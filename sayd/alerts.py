"""شروط التنبيه، صياغة الرسائل، وتجميعها في ملخص عند الكثرة."""
import html
import json
import logging
import re

from .db import ts_minus

log = logging.getLogger("sayd.alerts")


def money(v):
    if v is None:
        return "—"
    return f"{v:,.0f}" if abs(v - round(v)) < 0.005 else f"{v:,.2f}"


def watch_rules(change, watch):
    out = []
    for w in watch:
        if w.get("sku") and str(w["sku"]) == change.sku:
            out.append(w)
        elif w.get("category"):
            c = str(w["category"]).strip()
            if c in change.category_codes or c in (change.category or ""):
                out.append(w)
    return out


def price_alert(ch, cfg, watch):
    """يرجع payload إذا استحق نزول السعر تنبيهاً."""
    a = cfg["alerts"]
    old, new = ch.old_price, ch.new_price
    if old is None or new is None or new >= old or new < a["min_price"]:
        return None
    drop = (old - new) / old * 100
    rules = watch_rules(ch, watch)
    custom = [float(w["drop_pct"]) for w in rules if w.get("drop_pct") is not None]
    threshold = min(custom) if custom else a["min_drop_pct"]
    reasons = []
    if drop >= threshold:
        reasons.append(f"نزول {drop:.1f}%" + (" (نسبة خاصة)" if custom else ""))
    if ch.window_min is not None and new < ch.window_min and drop >= a["low_min_drop_pct"]:
        reasons.append(f"أقل سعر خلال {a['low_window_days']} يوم")
    for w in rules:
        t = w.get("target_price")
        if t is not None and new <= float(t):
            reasons.append(f"وصل للسعر المستهدف ({money(float(t))})")
    if not reasons:
        return None
    disc = discount_pct(new, ch.was_price)
    return {
        "sku": ch.sku, "name": ch.name, "url": ch.url, "category": ch.category,
        "old": old, "new": new, "drop_pct": round(drop, 2), "was": ch.was_price, "discount": round(disc, 1),
        "strong": drop >= a["strong_drop_pct"] or disc >= a["strong_deal_pct"],
        "prev_min": ch.prev_min, "window_min": ch.window_min,
        "lowest_ever": ch.prev_min is None or new < ch.prev_min,
        "reasons": reasons, "watched": bool(rules),
    }


def discount_pct(price, was):
    """الخصم مقارنة بالسعر قبل الخصم المعروض في الموقع."""
    if not price or not was or was <= price:
        return 0.0
    return (was - price) / was * 100


def restock_alert(ch, watched_skus, cfg):
    """رجوع التوفر: لقائمة المراقبة، ولأي منتج عليه خصم قوي (مثل عرض نفدت كميته ثم رجع)."""
    if not (ch.old_stock is False and ch.new_stock) or ch.new_price is None:
        return None
    disc = discount_pct(ch.new_price, ch.was_price)
    strong = disc >= cfg["alerts"]["strong_deal_pct"] and ch.new_price >= cfg["alerts"]["strong_restock_min_price"]
    if ch.sku not in watched_skus and not strong:
        return None
    return {"sku": ch.sku, "name": ch.name, "url": ch.url, "price": ch.new_price, "was": ch.was_price,
            "discount": round(disc, 1), "strong": strong, "watched": ch.sku in watched_skus}


# ---------- الصياغة ----------
def _e(s):
    return html.escape(str(s or ""), quote=False)


def fmt_price(p, short=False):
    if short:
        return (f"{'🔥' if p.get('strong') else '🔻'} <a href=\"{_e(p['url'])}\">{_e(p['name'][:70])}</a>\n"
                f"   {money(p['old'])} ← <b>{money(p['new'])}</b> (−{p['drop_pct']:.1f}%)" + (" ⭐" if p["lowest_ever"] else ""))
    h = (("🔥 <b>عرض قوي</b>\n" if p.get("strong") else "")
         + f"🔻 <b>{_e(p['name'])}</b>\n"
         f"السعر القديم: {money(p['old'])} ريال\n"
         f"السعر الجديد: <b>{money(p['new'])} ريال</b> (−{p['drop_pct']:.1f}%)\n"
         f"أقل سعر سابق: {money(p['prev_min'])} ريال\n"
         f"أقل سعر منذ بدء التتبع: {'نعم ⭐' if p['lowest_ever'] else 'لا'}\n"
         f"السبب: {_e('، '.join(p['reasons']))}\n"
         + ("✔️ تم التحقق من صفحة المنتج\n" if p.get("verified") else "")
         + f"{_e(p['url'])}")
    return h


def fmt_offer(o, short=False):
    status = "🏷️ عرض جديد" if o["status"] == "new" else "✏️ تغيّر عرض"
    who = o["provider_type"] + (f" – {o['provider']}" if o["provider"] else "")
    terms = []
    if o.get("pct"):
        terms.append(f"خصم {o['pct']:g}%")
    if o.get("amount"):
        terms.append(f"خصم {money(o['amount'])} ريال")
    if o.get("max_discount"):
        terms.append(f"بحد أقصى {money(o['max_discount'])}")
    if o.get("min_purchase"):
        terms.append(f"حد أدنى للشراء {money(o['min_purchase'])}")
    if o.get("expires_at"):
        terms.append(f"ينتهي {o['expires_at'][:10]}")
    code = f"الكود: <code>{_e(o['code'])}</code>" if o.get("code") else "بدون كود"
    if short:
        title = o["text"] if len(o["text"]) <= 70 else o["text"][:70] + "…"
        return f"{status}: {_e(title)}\n   {_e(who)} | {code}" + (f" | {_e('، '.join(terms))}" if terms else "")
    lines = [f"{status}: {_e(who)}", code]
    if terms:
        lines.append("الشروط: " + _e("، ".join(terms)))
    if o.get("scope"):
        lines.append("يشمل: " + _e(o["scope"]))
    text = o["text"] if len(o["text"]) <= 300 else o["text"][:300] + "…"
    lines.append(f"النص: {_e(text)}")
    if o.get("link"):
        lines.append(_e(o["link"]))
    return "\n".join(lines)


def fmt_restock(r, short=False):
    disc = f" (خصم {r['discount']:.0f}% من {money(r['was'])})" if r.get("discount") else ""
    s = ("🔥 " if r.get("strong") else "") + f"✅ رجع متوفر: <b>{_e(r['name'])}</b> — {money(r['price'])} ريال{disc}"
    return s if short else s + f"\n{_e(r['url'])}"


def fmt_system(s, short=False):
    return f"⚠️ <b>Sayd: مشكلة في الجمع</b>\n{_e(s['message'])}"


def subject(kind, p):
    """عنوان مختصر للإيميل وإشعار ntfy."""
    if kind == "price":
        return f"Sayd {'🔥' if p.get('strong') else '🔻'} {p['drop_pct']:.0f}% — {p['name'][:60]}"
    if kind == "restock":
        return f"Sayd {'🔥 ' if p.get('strong') else ''}✅ رجع متوفر — {p['name'][:60]}"
    if kind == "offer":
        return f"Sayd 🏷️ {'عرض جديد' if p['status'] == 'new' else 'تغيّر عرض'} — {p['text'][:50]}"
    return "Sayd"


FORMATTERS = {"price": fmt_price, "offer": fmt_offer, "restock": fmt_restock, "system": fmt_system}


def plain(h):
    return html.unescape(re.sub(r"<[^>]+>", "", h))


def _chunks(header, lines, limit=3800):
    out, cur = [], header
    for ln in lines:
        if len(cur) + len(ln) + 1 > limit:
            out.append(cur)
            cur = header + "(تابع)\n"
        cur += ln + "\n"
    out.append(cur)
    return out


def dispatch(db, notifier, cfg, ts, dashboard_url=""):
    """يرسل التنبيهات المعلقة (آخر 3 أيام) ويعلّمها كمرسلة."""
    pending = db.pending_alerts(ts_minus(ts, days=3))
    if not pending:
        return 0
    if not notifier.configured:
        log.warning("لا توجد قناة تنبيه مضبوطة (TELEGRAM_* أو NTFY_TOPIC أو SMTP_*)؛ %d تنبيه معلق", len(pending))
        return 0
    items = [(r["id"], r["kind"], json.loads(r["payload"])) for r in pending]
    a = cfg["alerts"]
    sent = 0

    # تنبيهات النظام دائماً منفردة
    normal = [i for i in items if i[1] != "system"]
    for i, kind, p in (i for i in items if i[1] == "system"):
        h = fmt_system(p)
        ch = notifier.send(h, plain(h), "Sayd ⚠️")
        if ch:
            db.mark_sent([i], ts, ch)
            sent += 1

    # كل تنبيه في رسالة مستقلة (الأكبر نزولاً أولاً)، وما زاد عن max_individual يُجمع في ملخص
    prices = sorted((x for x in normal if x[1] == "price"), key=lambda x: -x[2]["drop_pct"])
    restocks = [x for x in normal if x[1] == "restock"]
    offers = [x for x in normal if x[1] == "offer"]
    ordered = prices + restocks + offers
    single, rest = ordered[:a["max_individual"]], ordered[a["max_individual"]:]
    for i, kind, p in single:
        h = FORMATTERS[kind](p)
        ch = notifier.send(h, plain(h), subject(kind, p), p.get("url") or p.get("link"))
        if ch:
            db.mark_sent([i], ts, ch)
            sent += 1
        db.commit()
    if not rest:
        return sent

    lines = []
    for title, kind in (("نزول أسعار", "price"), ("رجوع توفر", "restock"), ("عروض", "offer")):
        group = [p for _, k, p in rest if k == kind]
        if group:
            lines.append(f"\n<b>{title} ({len(group)})</b>")
            lines += [FORMATTERS[kind](p, short=True) for p in group]
    header = f"📋 <b>تكملة تنبيهات Sayd</b> — {len(rest)} تنبيه إضافي\n"
    channels = [notifier.send(c, plain(c), "Sayd ملخص", dashboard_url or None) for c in _chunks(header, lines)]
    if all(channels):
        db.mark_sent([x[0] for x in rest], ts, channels[0])
        sent += len(rest)
    db.commit()
    return sent
