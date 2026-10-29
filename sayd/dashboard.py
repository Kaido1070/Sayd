"""توليد الموقع الثابت (Cloudflare Pages) من قاعدة البيانات.
الملفات العامة تحتوي فقط ما يحتاجه الزائر: العروض الحالية، الكوبونات، وسجل أسعار منتجات العروض.
لا أسرار ولا سجلات تشغيل ولا قائمة مراقبة."""
import hashlib
import json
import re
import shutil
from collections import Counter, defaultdict
from pathlib import Path

from . import categories
from . import deals as deals_mod
from .db import now, ts_minus

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
PREORDER_BADGE = "الحجز المسبق"  # شارة إكسترا لمنتجات الطلب المسبق
JUNK_BRANDS = {"القيمة الإفتراضية", "القيمة الافتراضية"}  # قيمة فارغة من المتجر وليست علامة تجارية


def template_hash():
    """بصمة قالب الموقع: إذا تغيّرت يُنشر الموقع فوراً دون انتظار موعد النشر."""
    h = hashlib.sha1()
    for f in sorted(WEB.rglob("*")) + [Path(__file__), ROOT / "sayd" / "deals.py", ROOT / "sayd" / "categories.py"]:
        if f.is_file():
            h.update(f.name.encode() + f.read_bytes())
    return h.hexdigest()[:16]


def _dump(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def _r(v):
    return None if v is None else round(v, 2)


def _deal(d):
    row = d["row"]
    specs = json.loads(row["specs"] or "[]")
    dept, cat = categories.place(row["category"], row["name"])
    return {
        "id": row["sku"], "n": row["name"], "m": row["model"] or "", "sp": specs[:2],
        "d": dept, "c": cat, "img": row["image"] or "", "u": row["url"],
        "p": _r(d["price"]), "o": _r(d["basis"]), "sv": _r(d["saving"]), "pc": round(d["pct"], 1),
        "st": 1 if row["in_stock"] else 0, "t": d["since"], "mp": 1 if row["marketplace"] else 0,
        "sl": row["seller"] or "", "lw": 1 if d["lowest"] else 0, "fs": d["first_seen"],
        "g": row["variant_group"] or "", "gl": row["variant_label"] or "",
    }


def shard(sku):
    """ملف بيانات المنتج: آخر 3 خانات من رمزه (≈1000 ملف صغير، كل صفحة منتج تحمّل ملفها فقط)."""
    return sku[-3:].lower()


def product_records(db, active_deals):
    """سجل كل منتج: البيانات، العرض الحالي إن وجد، وفترات السعر المرصودة كاملة."""
    deal_by = {d["row"]["sku"]: d for d in active_deals}
    ended = {r[0] for r in db.conn.execute("SELECT sku FROM deal_state WHERE status='ended'")}
    groups = defaultdict(list)
    for sku, g, label in db.conn.execute("SELECT sku, variant_group, variant_label FROM products WHERE variant_group IS NOT NULL"):
        groups[g].append([sku, label or ""])
    periods = defaultdict(list)
    for r in db.conn.execute("SELECT sku, price, was_price, in_stock, first_ts, last_ts, checks, source "
                             "FROM price_periods ORDER BY sku, first_ts, id"):
        # [أول رصد، آخر رصد، السعر، المتوفر، عدد مرات الرصد، السعر المشطوب المعلن]
        periods[r["sku"]].append([r["first_ts"][:16], r["last_ts"][:16], _r(r["price"]), r["in_stock"], r["checks"], _r(r["was_price"])])
    out = defaultdict(dict)
    for row in db.conn.execute("SELECT * FROM products"):
        sku = row["sku"]
        d = deal_by.get(sku)
        rec = {
            "n": row["name"], "m": row["model"] or "", "sp": json.loads(row["specs"] or "[]")[:3],
            "c": categories.place(row["category"], row["name"])[1], "img": row["image"] or "", "u": row["url"],
            "sl": row["seller"] or "", "mp": 1 if row["marketplace"] else 0,
            "p": _r(row["price"]), "st": 0 if row["delisted"] else (1 if row["in_stock"] else 0), "dl": row["delisted"] or 0,
            "fs": row["first_seen"], "ls": row["last_seen"], "pr": periods.get(sku, []),
        }
        if row["variant_group"] and len(groups[row["variant_group"]]) > 1:
            rec["gl"] = row["variant_label"] or ""
            rec["vo"] = [v for v in groups[row["variant_group"]] if v[0] != sku]
        if d:
            rec["deal"] = {"b": _r(d["basis"]), "p": _r(d["price"]), "sv": _r(d["saving"]), "pc": round(d["pct"], 1), "t": d["since"]}
        elif sku in ended:
            rec["ex"] = 1
        out[shard(sku)][sku] = rec
    return out


def preorder(db):
    """المتاح للطلب المسبق الآن: آخر قائمة جُلبت من إكسترا، أو المنتجات التي تحمل الشارة قبل أول جلب."""
    saved = db.get("preorder")
    if saved is not None:
        skus = saved["skus"]
    else:
        skus = [r[0] for r in db.conn.execute("SELECT sku FROM products WHERE badges LIKE ?", (f'%"{PREORDER_BADGE}"%',))]
    out = []
    for sku in skus:
        row = db.product(sku)
        if not row or row["delisted"]:
            continue
        out.append({"id": sku, "n": row["name"], "m": row["model"] or "", "img": row["image"] or "", "u": row["url"],
                    "p": _r(row["price"]), "st": 1 if row["in_stock"] else 0, "mp": 1 if row["marketplace"] else 0,
                    "sl": row["seller"] or "", "c": categories.place(row["category"], row["name"])[1]})
    return sorted(out, key=lambda x: (-x["st"], x["n"]))


def write_coverage(db, cfg, path):
    """تقرير داخلي لاكتمال الفهرسة وما تعذّر تحديثه (لا يُنشر في الموقع)."""
    ts = now()
    q = lambda sql, *a: db.conn.execute(sql, a).fetchone()[0]
    stale_since = ts_minus(ts, hours=cfg["scan"]["full_every_hours"] * 2 + 2)
    full_runs = db.conn.execute("SELECT started_at, finished_at, products_seen, note FROM runs "
                                "WHERE phases LIKE '%full%' ORDER BY id DESC LIMIT 1").fetchone()
    stale_by_dept = {r[0] or "غير معروف": r[1] for r in db.conn.execute(
        "SELECT dept, COUNT(*) FROM products WHERE delisted=0 AND last_seen<? GROUP BY dept ORDER BY 2 DESC", (stale_since,))}
    report = {
        "generated_at": ts,
        "history_started_at": q("SELECT MIN(first_ts) FROM price_periods"),
        "products_known": q("SELECT COUNT(*) FROM products"),
        "products_active": q("SELECT COUNT(*) FROM products WHERE delisted=0"),
        "products_delisted": q("SELECT COUNT(*) FROM products WHERE delisted=1"),
        "seen_last_24h": q("SELECT COUNT(*) FROM products WHERE last_seen>=?", ts_minus(ts, hours=24)),
        "stale_not_seen_since": stale_since,
        "stale_count": sum(stale_by_dept.values()),
        "stale_by_dept": stale_by_dept,
        "stale_sample": [r[0] for r in db.conn.execute(
            "SELECT sku FROM products WHERE delisted=0 AND last_seen<? ORDER BY last_seen LIMIT 50", (stale_since,))],
        "last_full_scan": dict(full_runs) if full_runs else None,
        "full_scan_in_progress": db.get("full_scan"),
        "search_api_total_last_seen": (db.get("full_scan") or {}).get("total"),
        "sitemap": {
            "urls": q("SELECT COUNT(*) FROM sitemap"),
            "checked": q("SELECT COUNT(*) FROM sitemap WHERE last_checked IS NOT NULL"),
            "dead_or_blocked": q("SELECT COUNT(*) FROM sitemap WHERE dead_until IS NOT NULL"),
            "not_in_db_unchecked": q("SELECT COUNT(*) FROM sitemap s LEFT JOIN products p ON p.sku=s.sku "
                                     "WHERE p.sku IS NULL AND s.last_checked IS NULL"),
        },
        "price_periods": q("SELECT COUNT(*) FROM price_periods"),
        "price_changes": q("SELECT COUNT(*) FROM price_history WHERE prev_price IS NOT NULL AND ABS(price-prev_price)>=0.005"),
        "deals": {s: n for s, n in db.conn.execute("SELECT status, COUNT(*) FROM deal_state GROUP BY status")},
        "errors_24h": {r[0]: r[1] for r in db.conn.execute(
            "SELECT substr(context,1,20), COUNT(*) FROM errors WHERE ts>=? GROUP BY 1 ORDER BY 2 DESC LIMIT 20", (ts_minus(ts, hours=24),))},
    }
    Path(path).write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return report


def _clean_offer_text(text):
    text = re.sub(r"\s*(اقرأ\s*)?الشروط والأحكام.*$", "", text or "").strip()
    return re.sub(r"\s+\.", ".", text)


def offers_payload(db, ts):
    """العروض والكوبونات الحقيقية النشطة فقط، بشروطها وتاريخ انتهائها."""
    def products(key, limit=8):
        rows = db.conn.execute(
            "SELECT p.sku, p.name, p.url, p.price, p.image FROM offer_products op JOIN products p ON p.sku=op.sku "
            "WHERE op.offer_key=? AND p.delisted=0 AND p.in_stock=1 ORDER BY p.price DESC LIMIT ?", (key, limit)).fetchall()
        total = db.conn.execute("SELECT COUNT(*) FROM offer_products op JOIN products p ON p.sku=op.sku "
                                "WHERE op.offer_key=? AND p.delisted=0 AND p.in_stock=1", (key,)).fetchone()[0]
        return total, [{"id": r[0], "n": r[1], "u": r[2], "p": _r(r[3]), "img": r[4] or ""} for r in rows]

    rows = db.conn.execute("SELECT * FROM offers WHERE active=1 AND (expires_at IS NULL OR expires_at>=?)", (ts,)).fetchall()
    bank_providers = {r["provider"] for r in rows if r["source"] == "bank" and r["provider"]}
    out = []
    for o in rows:
        src, text = o["source"], o["text"] or ""
        item = {"kind": "", "title": "", "desc": "", "code": o["code"] or "", "pct": o["pct"], "amount": o["amount"],
                "max": o["max_discount"], "min": o["min_purchase"], "exp": o["expires_at"], "who": o["provider"] or ""}
        if src == "bank":
            head, _, body = text.partition("•")
            item.update(kind="payment", title=(head.strip() or o["provider"] or "عرض دفع"), desc=_clean_offer_text(body or text))
        elif src == "installment":
            item.update(kind="installment", title=text.split(":")[0].strip(), desc=text.split(":", 1)[-1].strip())
        elif src == "badge" and o["code"]:
            item.update(kind="code", title=f"كود {o['code']}", desc="خصم على منتجات محددة عند إدخال الكود في السلة.")
        elif src == "badge" and "كاش باك" in text:
            item.update(kind="cashback", title="كاش باك", desc="استرداد مبلغ بعد الشراء، والمبلغ يختلف حسب المنتج.")
        else:
            continue  # شارات عامة (تخفيضات، عرض خاص) وبنرات وعروض الحزم لا تُنشر كعروض
        if src == "badge" and o["provider"] in bank_providers:
            continue
        n, sample = products(o["key"])
        if src in ("badge", "installment") and not n:
            continue
        item.update(count=n, products=sample if src == "badge" else [])
        out.append(item)
    order = {"payment": 0, "code": 1, "cashback": 2, "installment": 3}
    out.sort(key=lambda x: (order[x["kind"]], -(x["count"] or 0)))
    return out


def build(db, cfg, out):
    ts = now()
    out = Path(out)
    tmp = out.with_name(out.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(WEB, tmp)
    (tmp / "data" / "p").mkdir(parents=True)

    current = deals_mod.active(db)
    items = [_deal(d) for d in current]
    used = defaultdict(set)
    for i in items:
        used[i["d"]].add(i["c"])
    _dump(tmp / "data" / "deals.json", {
        "updated": ts,
        "products": db.conn.execute("SELECT COUNT(*) FROM products WHERE delisted=0").fetchone()[0],
        "cats": categories.order(db.get("menu"), used),  # [[القسم، [الفئات]]] بترتيب قائمة إكسترا
        "deals": sorted(items, key=lambda i: i["t"], reverse=True),
        "preorder": preorder(db),
    })
    _dump(tmp / "data" / "offers.json", {"updated": ts, "offers": offers_payload(db, ts)})
    # ملف فارغ مكان ملف قديم كان يحتوي سجل التشغيل؛ يستبدل النسخة المخزنة في ذاكرة Cloudflare
    _dump(tmp / "data" / "summary.json", {})

    # فهرس البحث لكل المنتجات (يُحمّل عند أول بحث فقط): [الرمز، الاسم، الموديل، السعر، الحالة، العلامة]
    _dump(tmp / "data" / "index.json", {"updated": ts, "items": [
        [r["sku"], r["name"], r["model"] or "", _r(r["price"]), -1 if r["delisted"] else (1 if r["in_stock"] else 0),
         "" if (r["brand"] or "") in JUNK_BRANDS else r["brand"] or ""]
        for r in db.conn.execute("SELECT sku, name, model, price, in_stock, delisted, brand FROM products ORDER BY sku")]})
    # صفحة كل منتج: ملف صغير مشترك بين ~33 منتجاً يُحمّل عند فتح الصفحة فقط
    for key, recs in product_records(db, current).items():
        _dump(tmp / "data" / "p" / f"{key}.json", recs)

    shutil.rmtree(out, ignore_errors=True)
    tmp.rename(out)
    return len(items)
