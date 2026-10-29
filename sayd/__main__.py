"""تشغيل Sayd: python -m sayd --data data"""
import argparse
import logging
import re
import sys
import time
import traceback
from pathlib import Path

from . import alerts, config, dashboard, offers
from .db import DB, now, parse_ts, ts_minus
from .extra import PAGE_SIZE, SORT_DISCOUNT, Extra, RawOffer, SiteChanged
from .net import Blocked, Disallowed, FetchError, Http
from .notify import Notifier

log = logging.getLogger("sayd")


class Runner:
    def __init__(self, cfg, db, ex, notifier, deadline, force_full=False, force_hot=False):
        self.cfg, self.db, self.ex, self.notifier = cfg, db, ex, notifier
        self.deadline = deadline
        self.force_full, self.force_hot = force_full, force_hot
        self.stats = {"seen": 0, "changes": 0, "alerts": 0, "errors": 0}
        self.phases = []
        self.pages_ok = self.pages_failed = 0
        self.parse_bad = 0
        self.pdp_ok = self.pdp_changed = 0
        self.last_error = ""
        self.watch = cfg["watch"]
        self.badge_skus = {}

    # ---------- مساعدات ----------
    def left(self):
        return self.deadline - time.time()

    def error(self, context, e):
        self.stats["errors"] += 1
        self.last_error = f"{context}: {e}"
        log.warning("%s: %s", context, e)
        self.db.log_error(context, e, now())

    @property
    def baseline_done(self):
        return bool(self.db.get("baseline_done", False))

    def due(self, key, seconds, slack):
        last = self.db.get(key)
        return last is None or (parse_ts(now()) - parse_ts(last)).total_seconds() >= seconds - slack

    def listing(self, context, **kw):
        try:
            total, products, bad = self.ex.listing(**kw)
        except (FetchError, Disallowed) as e:
            self.pages_failed += 1
            self.error(context, e)
            return None, []
        self.pages_ok += 1
        self.parse_bad += bad
        return total, products

    def process(self, products):
        """يسجل المنتجات ويضع التنبيهات في الطابور (بعد المسح الأول فقط)."""
        ts = now()
        alerting = self.baseline_done
        watched = self.watched_skus
        wdays = self.cfg["alerts"]["low_window_days"]
        for p in products:
            self.stats["seen"] += 1
            for b in p.badges:
                # "كاش باك 30 ريال" و"كاش باك 50 ريال" عرض واحد (الأكواد مثل W9150 تبقى كما هي)
                key = re.sub(r"\s*\d[\d,.]*\s*ريال", "", b).strip()
                self.badge_skus.setdefault(key or b, set()).add(p.sku)
            ch = self.db.observe(p, ts, wdays)
            if not ch or ch.is_new:
                continue
            self.stats["changes"] += 1
            if not alerting:
                continue
            pa = alerts.price_alert(ch, self.cfg, self.watch)
            if pa and self.db.queue_alert("price", ch.sku, ch.new_price, pa, ts):
                self.stats["alerts"] += 1
            ra = alerts.restock_alert(ch, watched)
            if ra and self.db.queue_alert("restock", ch.sku, ch.history_id, ra, ts):
                self.stats["alerts"] += 1
        self.db.commit()

    # ---------- المراحل ----------
    def full_scan(self):
        st = self.db.get("full_scan") or {"start": 0, "started_at": now(), "pages_ok": 0, "total": None}
        self.phases.append(f"full@{st['start']}")
        log.info("مسح كامل من الموضع %d", st["start"])
        fails = 0
        while self.left() > 90:
            total, products = self.listing(f"full start={st['start']}", start=st["start"])
            if total is None:
                fails += 1
                if fails >= 3:
                    break
                continue
            fails = 0
            self.process(products)
            st.update(start=st["start"] + PAGE_SIZE, total=total, pages_ok=st["pages_ok"] + 1)
            self.db.set("full_scan", st)
            self.db.commit()
            if st["start"] >= total:
                self._full_done(st)
                return True
        log.info("توقف المسح الكامل عند %d/%s وسيكمل لاحقاً", st["start"], st["total"])
        return False

    def _full_done(self, st):
        ts = now()
        if st["total"] and st["pages_ok"] * PAGE_SIZE >= 0.9 * st["total"]:
            n = self.db.delist_unseen(st["started_at"], ts)
            if n:
                log.info("%d منتج لم يعد ظاهراً في الكتالوج", n)
        self.db.set("full_scan", None)
        self.db.set("last_full_done_at", ts)
        if not self.baseline_done:
            self.db.set("baseline_done", True)
            self.db.set("baseline_done_at", ts)
            log.info("اكتمل المسح الأول (تأسيس الأسعار) — التنبيهات تبدأ من التشغيل القادم")
        self.db.commit()

    def hot_scan(self):
        sc = self.cfg["scan"]
        self.phases.append("hot")
        for cat in sc["hot_categories"]:
            start = 0
            while self.left() > 120 and start < 60 * PAGE_SIZE:
                total, products = self.listing(f"hot {cat}", start=start, category=cat, sort=SORT_DISCOUNT)
                if total is None:
                    break
                self.process(products)
                start += PAGE_SIZE
                if start >= total:
                    break
        for i in range(sc["hot_top_discount_pages"]):
            if self.left() < 120:
                break
            total, products = self.listing("hot top-discount", start=i * PAGE_SIZE, sort=SORT_DISCOUNT)
            if total is None:
                break
            self.process(products)
        self.watch_scan()
        self.db.set("last_hot_at", now())
        self.db.commit()

    def watch_scan(self):
        skus = sorted(self.watched_skus)
        for i in range(0, len(skus), 50):
            if self.left() < 120:
                break
            _, products = self.listing("watch", skus=skus[i:i + 50])
            self.process(products)

    def pdp_scan(self):
        sc = self.cfg["scan"]
        ts = now()
        explicit = [str(w["sku"]) for w in self.watch if w.get("sku")]
        skus = self.db.products_for_pdp(sc["pdp_per_run"], ts_minus(ts, hours=sc["pdp_recheck_hours"]), explicit)
        self.phases.append(f"pdp:{len(skus)}")
        alerting = self.baseline_done and bool(self.db.get("offers_baseline_done", False))
        blocked = 0
        for sku in skus:
            if self.left() < 120:
                break
            row = self.db.product(sku)
            try:
                page = self.ex.product_page(row["url"])
            except Blocked as e:
                blocked += 1
                self.error(f"pdp {sku}", e)
                if blocked >= 3:
                    log.warning("توقف فتح صفحات المنتجات لهذا التشغيل (حظر متكرر)")
                    break
                continue
            except SiteChanged as e:
                self.pdp_changed += 1
                self.error(f"pdp {sku}", e)
                continue
            except (FetchError, Disallowed) as e:
                self.error(f"pdp {sku}", e)
                continue
            self.pdp_ok += 1
            ts = now()
            self.db.mark_pdp(sku, ts)
            for raw in page.offers:
                self.save_offer(raw, [sku], ts, alerting)
            self.db.commit()
        if self.pdp_ok:
            self.db.set("offers_baseline_done", True)

    def banner_scan(self):
        self.phases.append("banners")
        ts = now()
        alerting = self.baseline_done and bool(self.db.get("banners_baseline_done", False))
        try:
            raws = self.ex.banners(self.cfg["scan"]["banner_pages"])
        except FetchError as e:
            self.error("banners", e)
            return
        for raw in raws:
            self.save_offer(raw, [], ts, alerting, banner=True)
        self.db.set("banners_baseline_done", True)
        self.db.commit()

    def badge_offers(self):
        """شارات المنتجات (مثل "عرض الراجحي") تُحفظ كعروض وتُنبّه عند ظهور شارة جديدة."""
        ts = now()
        alerting = self.baseline_done and bool(self.db.get("badges_baseline_done", False))
        for badge, skus in self.badge_skus.items():
            raw = RawOffer(key="badge:" + badge, source="badge", text=badge)
            self.save_offer(raw, sorted(skus), ts, alerting, banner=True)
        if self.badge_skus and self.baseline_done:
            self.db.set("badges_baseline_done", True)
        self.db.commit()

    def save_offer(self, raw, skus, ts, alerting, banner=False):
        o = offers.analyze(raw)
        if banner and not offers.is_promo_banner(o):
            return
        status = self.db.upsert_offer(o, ts)
        if skus:
            self.db.link_offer(o["key"], skus, ts)
        if not (status and alerting):
            return
        if o["source"] == "bundle" and not self.cfg["alerts"]["alert_bundle_offers"]:
            return
        o["status"] = status
        o["scope"] = self.offer_scope(o["key"])
        if self.db.queue_alert("offer", o["key"], o["content_hash"], o, ts):
            self.stats["alerts"] += 1

    def offer_scope(self, key):
        rows = self.db.conn.execute(
            "SELECT p.category FROM offer_products op JOIN products p ON p.sku=op.sku WHERE op.offer_key=?", (key,)).fetchall()
        if not rows:
            return ""
        tops = {}
        for r in rows:
            top = (r[0] or "").split(" > ")[0] or "غير مصنف"
            tops[top] = tops.get(top, 0) + 1
        cats = "، ".join(k for k, _ in sorted(tops.items(), key=lambda x: -x[1])[:3])
        return f"{cats} ({len(rows)} منتج)"

    @property
    def watched_skus(self):
        if not hasattr(self, "_watched"):
            self._watched = self.db.watch_skus(self.watch)
        return self._watched

    def health_check(self):
        """تنبيه واحد يومياً إذا فشل الجمع بالكامل."""
        problem = None
        if self.pages_ok == 0 and self.pages_failed > 0:
            problem = f"تعذّر جلب أي صفحة أسعار من اكسترا ({self.pages_failed} محاولة). آخر خطأ: {self.last_error}"
        elif self.pages_ok > 0 and self.stats["seen"] == 0 and self.parse_bad > 0:
            problem = "صفحات الأسعار تصل لكن تحليل المنتجات فشل بالكامل — غالباً تغيّر شكل بيانات الموقع."
        elif self.pdp_ok == 0 and self.pdp_changed >= 5:
            problem = "صفحات المنتجات لم تعد تحتوي بيانات العروض بالشكل المعروف — راجع extra.py."
        if problem:
            log.error(problem)
            ts = now()
            self.db.queue_alert("system", "collect", ts[:10], {"message": problem}, ts)

    # ---------- التشغيل ----------
    def run(self):
        sc = self.cfg["scan"]
        in_progress = self.db.get("full_scan") is not None
        full_due = self.force_full or in_progress or not self.baseline_done or \
            self.due("last_full_done_at", sc["full_every_hours"] * 3600, 20 * 60)
        hot_due = self.baseline_done and (self.force_hot or self.due("last_hot_at", sc["hot_every_minutes"] * 60, 10 * 60))
        pending = self.db.pending_alerts(ts_minus(now(), days=3)) if self.notifier.configured else []
        if not (full_due or hot_due or pending):
            log.info("لا يوجد عمل مستحق في هذا التشغيل")
            return False

        if full_due or hot_due:
            found = self.ex.discover()
            if found and found != self.db.get("unbxd"):
                self.db.set("unbxd", found)
        if hot_due:
            self.hot_scan()
            self.pdp_scan()
            self.banner_scan()
        if full_due:
            self.full_scan()
        self.badge_offers()
        ts = now()
        self.db.expire_offers(ts, ts_minus(ts, days=4))
        self.health_check()
        self.db.commit()
        return True


def main(argv=None):
    ap = argparse.ArgumentParser(prog="sayd")
    ap.add_argument("--data", default="data", help="مجلد قاعدة البيانات واللوحة")
    ap.add_argument("--config", default=None)
    ap.add_argument("--max-minutes", type=float, default=None)
    ap.add_argument("--force-full", action="store_true")
    ap.add_argument("--force-hot", action="store_true")
    ap.add_argument("--no-send", action="store_true", help="لا ترسل تنبيهات (تبقى معلقة)")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stdout)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # يمنع طباعة الروابط (رابط تيليجرام يحتوي التوكن)

    cfg = config.load(args.config)
    data = Path(args.data)
    data.mkdir(parents=True, exist_ok=True)
    db = DB(data / "sayd.db")
    minutes = args.max_minutes or cfg["scan"]["max_run_minutes"]
    deadline = time.time() + minutes * 60
    sc = cfg["scan"]
    http = Http(cfg["site"]["user_agent"], sc["delay_min"], sc["delay_max"], deadline)
    ex = Extra(http, cfg["site"]["include_marketplace"], db.get("unbxd"))
    notifier = Notifier(cfg["secrets"])
    runner = Runner(cfg, db, ex, notifier, deadline, args.force_full, args.force_hot)
    run_id = db.start_run(now())
    db.commit()
    status, worked = "ok", True
    try:
        worked = runner.run()
    except Exception as e:  # خطأ غير متوقع: سجّله ونبّه، والبيانات المحفوظة حتى الآن تبقى
        status = "crash"
        log.error("خطأ غير متوقع:\n%s", traceback.format_exc())
        db.conn.rollback()
        ts = now()
        db.queue_alert("system", "crash", ts[:10], {"message": f"خطأ غير متوقع في Sayd: {type(e).__name__}: {str(e)[:300]}"}, ts)
    finally:
        http.close()

    if not worked:
        db.conn.execute("DELETE FROM runs WHERE id=?", (run_id,))
        db.close()
        notifier.close()
        return 0

    if not args.no_send:
        try:
            alerts.dispatch(db, notifier, cfg, now(), cfg["site"].get("dashboard_url", ""))
        except Exception:
            log.error("فشل إرسال التنبيهات:\n%s", traceback.format_exc())
    notifier.close()
    db.finish_run(run_id, now(), phases=runner.phases, products_seen=runner.stats["seen"],
                  changes=runner.stats["changes"], alerts=runner.stats["alerts"],
                  errors=runner.stats["errors"], status=status, note=runner.last_error[:300])
    db.commit()
    try:
        dashboard.build(db, cfg, data / "site")
    except Exception:
        log.error("فشل توليد اللوحة:\n%s", traceback.format_exc())
    ok = db.integrity_ok()
    db.close()
    if not ok:
        log.error("فحص سلامة قاعدة البيانات فشل — لن تُنشر")
        return 2
    (data / ".changed").write_text(now())
    log.info("انتهى: %s", runner.stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
