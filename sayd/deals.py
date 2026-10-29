"""منطق التخفيض المشترك (الموقع والتنبيهات).

المقارنة دائماً مع أسعار رصدها النظام لنفس المنتج (نفس الرمز = نفس المتغير والبائع)،
وليس مع السعر المشطوب أو نسبة الخصم التي يعلنها المتجر.

سعر المقارنة (الأساس) = الأقل بين:
  - آخر سعر صالح قبل النزول، و
  - أقل سعر ثبت 24 ساعة أو أكثر خلال الثلاثين يوماً التي سبقت النزول.
فإذا رُفع السعر قبل "التخفيض" بفترة قصيرة (حيلة الجمعة السوداء) لا يُحسب الرفع أساساً.

حالة العرض محفوظة (deal_state): ثبات السعر أو فشل الجلب لا ينهي العرض؛ يُراجع فقط عند تغيّر
السعر المؤكد أو خروج المنتج من الكتالوج. لكل رمز منتج بطاقة واحدة فقط.
"""
from .db import parse_ts


def drop_pct(old, new):
    """نسبة النزول من السعر السابق المرصود."""
    if not old or not new or old <= 0 or new >= old:
        return 0.0
    return (old - new) / old * 100


def qualifies(old, new, rules):
    """العرض المؤهل: (نزول ≥ النسبة و توفير ≥ الحد الأدنى) أو توفير كبير مهما كانت النسبة."""
    if not old or not new or new <= 0 or new >= old:
        return False
    saving = old - new
    return (drop_pct(old, new) >= rules["min_drop_pct"] and saving >= rules["min_saving"]) or saving >= rules["big_saving"]


def _hours(a, b):
    return (parse_ts(b) - parse_ts(a)).total_seconds() / 3600


def price_runs(periods):
    """دمج الفترات المتتالية بنفس السعر (حتى لو تغيّر التوفر أو انقطع الرصد): [{price, first, last}]."""
    runs = []
    for p in periods:
        if runs and abs(runs[-1]["price"] - p["price"]) < 0.005:
            runs[-1]["last"] = max(runs[-1]["last"], p["last_ts"])
        else:
            runs.append({"price": p["price"], "first": p["first_ts"], "last": p["last_ts"]})
    return runs


def reference(runs, rules):
    """(الأساس، آخر سعر قبل النزول، وقت رصد السعر الحالي) أو None إذا لا يوجد سعر سابق."""
    if len(runs) < 2:
        return None
    cur, prev = runs[-1], runs[-2]
    window_days = rules.get("reference_days", 30)
    sustained = [r["price"] for r in runs[:-1]
                 if _hours(r["last"], cur["first"]) <= window_days * 24 and _hours(r["first"], r["last"]) >= rules.get("sustain_hours", 24)]
    return min([prev["price"]] + sustained), prev["price"], cur["first"]


def _confirmed(row, periods, price):
    """السعر الحالي مؤكد إذا رُصد مرتين على الأقل، أو طابقته صفحة المنتج بعد بدء رصده."""
    last = periods[-1]
    if abs(last["price"] - price) >= 0.005:
        return False
    if last["checks"] >= 2:
        return True
    return bool(row["pdp_at"] and row["pdp_price"] is not None and abs(row["pdp_price"] - price) <= 0.5
                and row["pdp_at"] >= last["first_ts"])


def update_states(db, rules, ts):
    """يراجع العروض: الجديدة، المتغيرة، والمعلقة بانتظار التأكيد. يرجع عدد التغييرات."""
    since = db.get("deals_eval_at") or ""
    skus = {r[0] for r in db.conn.execute("SELECT DISTINCT sku FROM price_periods WHERE first_ts>=?", (since,))}
    skus |= {r[0] for r in db.conn.execute("SELECT sku FROM deal_state WHERE status IN ('active','pending')")}
    if not since:  # أول مراجعة: كل منتج له أكثر من سعر
        skus |= {r[0] for r in db.conn.execute("SELECT sku FROM price_periods GROUP BY sku HAVING COUNT(DISTINCT price)>1")}
    changed = 0
    for sku in skus:
        row = db.product(sku)
        periods = db.periods(sku)
        if not row or not periods:
            continue
        state = db.deal(sku)
        status = state["status"] if state else None
        runs = price_runs(periods)
        cur = runs[-1]["price"]
        if row["delisted"]:
            if status == "active":
                db.set_deal(sku, status="ended", ended_at=ts, reason="removed")
                changed += 1
            elif status == "pending":
                db.drop_deal(sku)
            continue
        confirmed = _confirmed(row, periods, cur)
        if status == "active":
            if abs(cur - state["price"]) < 0.005 or not confirmed:
                continue  # السعر لم يتغير أو التغيير لم يتأكد بعد: العرض باقٍ كما هو
            if qualifies(state["basis"], cur, rules):
                db.set_deal(sku, price=cur, updated_at=ts)  # نفس البطاقة بسعر جديد
            else:
                db.set_deal(sku, status="ended", price=cur, updated_at=ts, ended_at=ts,
                            reason="price_up" if cur > state["price"] else "not_qualified")
            changed += 1
            continue
        ref = reference(runs, rules)
        if ref and qualifies(ref[0], cur, rules):
            basis, _, started = ref
            if confirmed:
                db.set_deal(sku, status="active", basis=basis, price=cur, started_at=started,
                            updated_at=ts, ended_at=None, reason=None)
                changed += 1
            elif status != "pending":
                db.set_deal(sku, status="pending", basis=basis, price=cur, started_at=started, updated_at=ts)
        elif status == "pending":
            db.drop_deal(sku)
    db.set("deals_eval_at", ts)
    return changed


def active(db):
    """العروض الحالية مع بيانات العرض: الأساس، السعر المؤكد، التوفير، وأقل سعر منذ بدء الرصد."""
    out = []
    for st in db.conn.execute("SELECT * FROM deal_state WHERE status='active'").fetchall():
        row = db.product(st["sku"])
        if not row:
            continue
        periods = db.periods(st["sku"])
        earlier = [p["price"] for p in periods if p["first_ts"] < st["started_at"]]
        out.append({
            "row": row, "basis": st["basis"], "price": st["price"], "since": st["started_at"],
            "first_seen": periods[0]["first_ts"] if periods else row["first_seen"],
            "saving": st["basis"] - st["price"], "pct": drop_pct(st["basis"], st["price"]),
            "lowest": bool(earlier) and st["price"] < min(earlier),
            "page_ok": bool(row["pdp_at"] and row["pdp_at"] >= st["started_at"]),
        })
    return out


def pending(db):
    """عروض محتملة تنتظر التأكيد، وعروض حالية تغيّر سعرها ولم يتأكد التغيير."""
    rows = db.conn.execute("SELECT d.sku FROM deal_state d JOIN products p ON p.sku=d.sku WHERE d.status='pending' "
                           "OR (d.status='active' AND ABS(p.price - d.price) >= 0.005)").fetchall()
    return [r[0] for r in rows]


def ended(db):
    """«خرجت من العروض»: اسم واحد لكل منتج، الأحدث أولاً."""
    return [r[0] for r in db.conn.execute("SELECT p.name FROM deal_state d JOIN products p ON p.sku=d.sku "
                                          "WHERE d.status='ended' ORDER BY d.ended_at DESC")]
