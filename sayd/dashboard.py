"""توليد اللوحة الثابتة (Cloudflare Pages) من قاعدة البيانات. لا تحتوي أي سر."""
import json
import shutil
from collections import defaultdict
from pathlib import Path

from .db import now, parse_ts, ts_minus

WEB = Path(__file__).resolve().parent.parent / "web"


def _dump(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def _r(v):
    return None if v is None else round(v, 2)


def history_stats(db, ts, days=30):
    """متوسط زمني موزون لآخر 30 يوماً + سجل كل منتج."""
    w = parse_ts(ts_minus(ts, days=days)).timestamp()
    end = parse_ts(ts).timestamp()
    hist = defaultdict(list)
    for sku, t, price, stock in db.conn.execute("SELECT sku, ts, price, in_stock FROM price_history ORDER BY sku, ts, id"):
        hist[sku].append((t, price, stock))
    avg = {}
    for sku, rows in hist.items():
        total = span = 0.0
        for i, (t, price, _) in enumerate(rows):
            if price is None:
                continue
            a = max(parse_ts(t).timestamp(), w)
            b = parse_ts(rows[i + 1][0]).timestamp() if i + 1 < len(rows) else end
            if b > a:
                total += price * (b - a)
                span += b - a
        if span:
            avg[sku] = total / span
    return hist, avg


def recent_drops(db, ts, days=14, limit=400):
    rows = db.conn.execute("""
        SELECT h.sku, h.ts, h.price, h.prev, p.name, p.url, p.category, p.min_price, p.in_stock FROM (
          SELECT sku, ts, price, LAG(price) OVER (PARTITION BY sku ORDER BY ts, id) AS prev FROM price_history
        ) h JOIN products p ON p.sku = h.sku
        WHERE h.ts >= ? AND h.prev IS NOT NULL AND h.price IS NOT NULL AND h.price < h.prev
        ORDER BY h.ts DESC LIMIT ?""", (ts_minus(ts, days=days), limit)).fetchall()
    return [{
        "sku": r["sku"], "ts": r["ts"], "name": r["name"], "url": r["url"], "cat": (r["category"] or "").split(" > ")[-1],
        "old": r["prev"], "new": r["price"], "pct": round((r["prev"] - r["price"]) / r["prev"] * 100, 1),
        "lowest": r["min_price"] is not None and r["price"] <= r["min_price"], "stock": bool(r["in_stock"]),
    } for r in rows]


def build(db, cfg, out):
    ts = now()
    out = Path(out)
    tmp = out.with_name(out.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(WEB, tmp)
    (tmp / "data" / "h").mkdir(parents=True)

    hist, avg = history_stats(db, ts, cfg["alerts"]["low_window_days"])

    # فهرس البحث المختصر: الأقسام في قائمة منفصلة، والرابط يُبنى من SKU (/ar-sa/p/<sku>)
    # items: [sku, name, category_index, price, was, stock, min, max, avg30]
    cats, items = {}, []
    for r in db.conn.execute("SELECT * FROM products ORDER BY sku"):
        ci = cats.setdefault(r["category"] or "", len(cats))
        items.append([r["sku"], r["name"], ci, _r(r["price"]), _r(r["was_price"]),
                      0 if r["delisted"] else r["in_stock"], _r(r["min_price"]), _r(r["max_price"]), _r(avg.get(r["sku"]))])
    _dump(tmp / "data" / "products.json", {"cats": list(cats), "items": items})

    shards = defaultdict(dict)
    for sku, rows in hist.items():
        shards[sku[-2:]][sku] = [[t[:16], _r(p), s] for t, p, s in rows]
    for k, v in shards.items():
        _dump(tmp / "data" / "h" / f"{k}.json", v)

    offers = []
    for o in db.conn.execute("SELECT * FROM offers WHERE active=1 AND source NOT IN ('bundle') ORDER BY last_seen DESC"):
        n = db.conn.execute("SELECT COUNT(*) FROM offer_products WHERE offer_key=?", (o["key"],)).fetchone()[0]
        offers.append({k: o[k] for k in ("key", "source", "provider_type", "provider", "code", "pct", "amount",
                                         "min_purchase", "max_discount", "expires_at", "text", "link", "first_seen", "last_seen")}
                      | {"products": n})
    # الأهم أولاً: ما له كود، ثم ما جهته معروفة، ثم الأوسع انتشاراً
    offers.sort(key=lambda o: (not o["code"], o["provider_type"] == "غير محدد", -o["products"]))

    watch = []
    for w in cfg["watch"]:
        item = {k: w.get(k) for k in ("sku", "category", "target_price", "drop_pct")}
        if w.get("sku"):
            p = db.product(str(w["sku"]))
            if p:
                item.update(name=p["name"], price=p["price"], stock=bool(p["in_stock"]) and not p["delisted"], url=p["url"])
        watch.append(item)

    full = db.get("full_scan")
    runs = [dict(r) for r in db.conn.execute(
        "SELECT started_at, finished_at, phases, products_seen, changes, alerts, errors, status FROM runs ORDER BY id DESC LIMIT 10")]
    summary = {
        "generated_at": ts,
        "baseline_done": bool(db.get("baseline_done", False)),
        "full_scan": full and {"start": full["start"], "total": full["total"]},
        "last_full_done_at": db.get("last_full_done_at"),
        "products": len(items),
        "in_stock": sum(1 for p in items if p[5]),
        "drops": recent_drops(db, ts),
        "offers": offers,
        "watch": watch,
        "runs": runs,
        "settings": {"min_drop_pct": cfg["alerts"]["min_drop_pct"], "window_days": cfg["alerts"]["low_window_days"]},
    }
    _dump(tmp / "data" / "summary.json", summary)

    shutil.rmtree(out, ignore_errors=True)
    tmp.rename(out)
