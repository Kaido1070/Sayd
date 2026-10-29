"""توليد الموقع الثابت (Cloudflare Pages) من قاعدة البيانات.
الملفات العامة تحتوي فقط ما يحتاجه الزائر: العروض الحالية، الكوبونات، وسجل أسعار منتجات العروض.
لا أسرار ولا سجلات تشغيل ولا قائمة مراقبة."""
import hashlib
import json
import re
import shutil
from collections import Counter, defaultdict
from pathlib import Path

from . import deals as deals_mod
from .db import now

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"


def template_hash():
    """بصمة قالب الموقع: إذا تغيّرت يُنشر الموقع فوراً دون انتظار موعد النشر."""
    h = hashlib.sha1()
    for f in sorted(WEB.rglob("*")) + [Path(__file__), ROOT / "sayd" / "deals.py"]:
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
    return {
        "id": row["sku"], "n": row["name"], "m": row["model"] or "", "sp": specs[:2],
        "c": (row["dept"] or "").strip() or "أخرى", "img": row["image"] or "", "u": row["url"],
        "p": _r(d["price"]), "o": _r(d["prev"]), "sv": _r(d["saving"]), "pc": round(d["pct"], 1),
        "st": 1 if row["in_stock"] else 0, "t": d["since"], "mp": 1 if row["marketplace"] else 0,
        "sl": row["seller"] or "", "lw": 1 if d["lowest"] else 0, "fs": d["first_seen"],
        "g": row["variant_group"] or "", "gl": row["variant_label"] or "",
    }


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
    (tmp / "data" / "h").mkdir(parents=True)

    found = deals_mod.find_deals(db, cfg["deals"], ts)
    items = [_deal(d) for d in found]
    cats = Counter(i["c"] for i in items)
    _dump(tmp / "data" / "deals.json", {
        "updated": ts,
        "cats": [c for c, _ in cats.most_common()],
        "deals": sorted(items, key=lambda i: i["t"], reverse=True),
    })
    _dump(tmp / "data" / "offers.json", {"updated": ts, "offers": offers_payload(db, ts)})
    # ملف فارغ مكان ملف قديم كان يحتوي سجل التشغيل؛ يستبدل النسخة المخزنة في ذاكرة Cloudflare
    _dump(tmp / "data" / "summary.json", {})

    # سجل الأسعار الحقيقي لمنتجات العروض فقط
    shards = defaultdict(dict)
    for i in items:
        rows = db.conn.execute("SELECT ts, price, in_stock FROM price_history WHERE sku=? ORDER BY ts, id", (i["id"],)).fetchall()
        shards[i["id"][-2:]][i["id"]] = [[t[:16], _r(p), s] for t, p, s in rows if p and p > 0]
    for k, v in shards.items():
        _dump(tmp / "data" / "h" / f"{k}.json", v)

    shutil.rmtree(out, ignore_errors=True)
    tmp.rename(out)
    return len(items)
