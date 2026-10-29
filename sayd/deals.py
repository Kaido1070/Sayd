"""منطق التخفيض المشترك (الموقع والتنبيهات): المقارنة دائماً مع آخر سعر صالح رصده النظام لنفس المنتج،
وليس مع السعر المشطوب الذي يعلنه المتجر."""
from collections import defaultdict

from .db import parse_ts, ts_minus


def drop_pct(old, new):
    """نسبة النزول من السعر السابق المرصود."""
    if not old or not new or old <= 0 or new >= old:
        return 0.0
    return (old - new) / old * 100


def qualifies(old, new, rules):
    """العرض المؤهل للعرض الافتراضي: (نزول ≥ النسبة و توفير ≥ الحد الأدنى) أو توفير كبير مهما كانت النسبة."""
    if not old or not new or new <= 0 or new >= old:
        return False
    saving = old - new
    return (drop_pct(old, new) >= rules["min_drop_pct"] and saving >= rules["min_saving"]) or saving >= rules["big_saving"]


def current_drop(history):
    """history: [(ts, price, in_stock)] مرتبة زمنياً. يرجع (السعر السابق، الحالي، وقت النزول، أقل سعر سابق، أول رصد)
    أو None. السعر المفقود أو الصفري لا يُحسب، وأول رصد للمنتج ليس نزولاً."""
    valid = [(t, p) for t, p, _ in history if p and p > 0]
    if len(valid) < 2:
        return None
    cur = valid[-1][1]
    start = len(valid) - 1
    while start > 0 and abs(valid[start - 1][1] - cur) < 0.005:
        start -= 1
    if start == 0:
        return None
    prev = valid[start - 1][1]
    if cur >= prev:
        return None
    return prev, cur, valid[start][0], min(p for _, p in valid[:start]), valid[0][0]


def find_deals(db, rules, now_ts, confirmed_only=True):
    """العروض الحالية المؤكدة. مؤكد = أعيد رصد السعر بعد النزول، أو طابقته صفحة المنتج."""
    hist = defaultdict(list)
    for sku, t, p, s in db.conn.execute(
            "SELECT sku, ts, price, in_stock FROM price_history WHERE sku IN "
            "(SELECT sku FROM price_history GROUP BY sku HAVING COUNT(*) > 1) ORDER BY sku, ts, id"):
        hist[sku].append((t, p, s))
    oldest = ts_minus(now_ts, days=rules["max_age_days"])
    out = []
    for sku, rows in hist.items():
        d = current_drop(rows)
        if not d:
            continue
        prev, cur, since, prev_min, first = d
        if since < oldest or not qualifies(prev, cur, rules):
            continue
        row = db.product(sku)
        if not row or row["delisted"] or row["price"] is None or abs(row["price"] - cur) > 0.005:
            continue
        page_ok = row["pdp_at"] and row["pdp_at"] >= since and row["pdp_price"] is not None \
            and abs(row["pdp_price"] - cur) <= 0.5
        if confirmed_only and not (page_ok or (row["last_seen"] and row["last_seen"] > since)):
            continue  # لم يتأكد بعد (رُصد مرة واحدة فقط)
        out.append({
            "row": row, "prev": prev, "price": cur, "since": since, "first_seen": first,
            "saving": prev - cur, "pct": drop_pct(prev, cur),
            # أقل سعر: فقط إذا كان أقل من كل الأسعار السابقة المرصودة، مع مدة الرصد الفعلية
            "lowest": cur < prev_min, "page_ok": bool(page_ok),
            "tracked_hours": (parse_ts(now_ts) - parse_ts(first)).total_seconds() / 3600,
        })
    return out
