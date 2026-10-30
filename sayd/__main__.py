"""تشغيل Sayd: python -m sayd --data data"""
import argparse
import json
import logging
import queue
import re
import sys
import threading
import time
import traceback
from dataclasses import replace
from pathlib import Path

from . import alerts, config, dashboard, deals, offers
from .db import DB, now, parse_ts, ts_minus
from .extra import OFFERS_FILTER, PAGE_SIZE, Extra, RawOffer, SiteChanged
from .net import Blocked, Disallowed, FetchError, Http
from .notify import Notifier

log = logging.getLogger("sayd")
OVERLAP = 5  # تداخل الصفحات


class Lane(threading.Thread):
    """مسار خلفي: يجلب صفحات المسح الكامل ثم صفحات خريطة الموقع بالتوازي مع فحص العروض.
    يجلب فقط (بعميل HTTP مستقل)؛ كل الكتابة في قاعدة البيانات تتم في الخيط الرئيسي (Runner.drain)."""

    def __init__(self, ex, full, rows, until, deadline):
        super().__init__(daemon=True)
        self.ex, self.full, self.rows, self.until, self.deadline = ex, full, rows, until, deadline
        self.out = queue.Queue()
        self.halt = threading.Event()

    def time_ok(self):
        return not self.halt.is_set() and time.time() < self.until and self.deadline - time.time() > 120

    def run(self):
        try:
            if self.full is not None:
                self._full(dict(self.full))
            self._sitemap()
        except Exception as e:  # لا يُسقط التشغيل؛ يُسجَّل كخطأ
            self.out.put(("error", "مسار خلفي", f"{type(e).__name__}: {e}"))
        finally:
            self.out.put(("end",))

    def _full(self, st):
        fails = suspicious = 0
        while self.time_ok():
            t = now()
            try:
                total, products, bad = self.ex.listing(start=st["start"])
            except (FetchError, Disallowed) as e:
                self.out.put(("fail", f"مسح كامل start={st['start']}", str(e)))
                fails += 1
                if fails >= 3:
                    return
                continue
            # محرك البحث أحياناً يرجع إجمالياً أصغر بكثير لطلب واحد: نعيد الطلب مرتين قبل قبوله
            odd = st["total"] and (total < 0.9 * st["total"] or (not products and st["start"] < total))
            if odd and suspicious < 2:
                suspicious += 1
                self.out.put(("error", "مسح كامل", f"استجابة غير متسقة عند {st['start']}: الإجمالي {total} بدل {st['total']}"))
                time.sleep(5)
                continue
            fails = suspicious = 0
            last = st["start"] + PAGE_SIZE >= total
            self.out.put(("list", t, st["start"], total, products, bad, last))
            st["total"] = total
            if last:
                return
            st["start"] += PAGE_SIZE - OVERLAP

    def _sitemap(self):
        blocked = 0
        for sku, url in self.rows:
            if not self.time_ok():
                return
            t = now()
            try:
                self.out.put(("page", t, sku, url, self.ex.product_page(url), None))
            except Blocked as e:
                self.out.put(("page", t, sku, url, None, e))
                blocked += 1
                if blocked >= 3:
                    return
            except (FetchError, Disallowed) as e:
                self.out.put(("page", t, sku, url, None, e))


class Runner:
    def __init__(self, cfg, db, ex, notifier, deadline, force_full=False, force_hot=False):
        self.cfg, self.db, self.ex, self.notifier = cfg, db, ex, notifier
        self.deadline = deadline
        self.started = time.time()
        self.force_full, self.force_hot = force_full, force_hot
        self.stats = {"seen": 0, "changes": 0, "alerts": 0, "errors": 0}
        self.phases = []
        self.pages_ok = self.pages_failed = 0
        self.parse_bad = 0
        self.pdp_ok = self.pdp_changed = 0
        self.last_error = ""
        self.watch = cfg["watch"]
        self.badge_skus = {}
        self.to_verify = []
        self.blocked = 0
        self.lane = None
        self.lane_end = self._draining = False
        self.lane_ex = None  # للاختبارات: عميل بديل للمسار الخلفي

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
        self.drain()
        try:
            total, products, bad = self.ex.listing(**kw)
        except (FetchError, Disallowed) as e:
            self.pages_failed += 1
            self.error(context, e)
            return None, []
        self.pages_ok += 1
        self.parse_bad += bad
        return total, products

    def process(self, products, source="api"):
        """يسجل المنتجات؛ نزول السعر يُجمع للتحقق منه قبل التنبيه (بعد المسح الأول فقط).
        source="pdp": السعر من صفحة المنتج نفسها فهو مؤكد ولا يحتاج تحقق إضافي."""
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
            ch = self.db.observe(p, ts, wdays, source=source)
            if not ch or ch.is_new:
                continue
            self.stats["changes"] += 1
            if not alerting:
                continue
            pa = alerts.price_alert(ch, self.cfg, self.watch)
            if pa and source == "pdp":
                pa["verified"] = True
                if self.db.queue_alert("price", ch.sku, pa["new"], pa, ts):
                    self.stats["alerts"] += 1
            elif pa:
                self.to_verify.append((ch, pa))
            if pa:
                continue  # تنبيه النزول يكفي؛ لا يتكرر معه «رجع متوفر»
            ra = alerts.restock_alert(ch, watched, self.cfg, self.basis_before(ch.sku) if ch.new_stock and not ch.old_stock else None)
            # لا يتكرر تنبيه رجوع التوفر لنفس المنتج بنفس السعر
            if ra and self.db.queue_alert("restock", ch.sku, ch.new_price, ra, ts):
                self.stats["alerts"] += 1
        self.db.commit()

    def strong_scan(self):
        """خصومات قوية جديدة لتيليجرام: خصم ≥ 50% (خصم إكسترا المعلن أو النزول المرصود) والسعر الأساسي (قبل الخصم) > 100 ومتوفر.
        «جديد» = لم يُسجَّل من قبل، أو نزل سعره 5% عن آخر ما سُجّل. أول تشغيل (أو بعد تغيير الشرط) يسجّل الموجود دون إرسال.
        يُرسل فقط بعد التأكد من صفحة المنتج (السعر والخصم والتوفر)."""
        a = self.cfg["alerts"]
        pct, floor = a["strong_min_pct"], a["strong_min_price"]
        ts = now()
        rows = {r["sku"]: r for r in self.db.conn.execute(
            "SELECT * FROM products WHERE delisted=0 AND in_stock=1 AND was_price>? AND was_price>price "
            "AND (was_price-price)/was_price*100>=?", (floor, pct))}
        seen_drop = {}
        for d in deals.active(self.db):  # نزول رصدناه نحن ولو بلا سعر مشطوب
            r = d["row"]
            if r["in_stock"] and d["basis"] > floor and d["pct"] >= pct:
                rows.setdefault(r["sku"], r)
                seen_drop[r["sku"]] = d["basis"]
        seen = self.db.get("strong_seen")
        rule = f"{pct}/{floor}/before"  # تغيّر الشرط = ما صار يطابقه الآن ليس «جديداً»
        first = seen is None or self.db.get("strong_rule") != rule
        seen = seen or {}
        forget = ts_minus(ts, days=a["strong_forget_days"])
        seen = {k: v for k, v in seen.items() if v["t"] >= forget}
        new = []
        for sku, r in rows.items():
            old = seen.get(sku)
            if first or (old and r["price"] > old["p"] * 0.95):
                seen[sku] = {"p": min(r["price"], old["p"]) if old else r["price"], "t": ts}  # معروف: لا يُرسل
            else:
                new.append(r)
        self.phases.append(f"strong:{len(rows)}/{len(new)}")
        sent = 0
        for r in sorted(new, key=lambda r: -(r["was_price"] or 0) + r["price"])[:a["strong_verify_max"]]:
            if self.left() < 60 or self.blocked >= 3:
                break
            page = self.fetch_page(r["sku"], r["url"])
            if not page or page.price is None:
                continue
            self.handle_page(r["sku"], page)
            before = max(x for x in (page.was_price, seen_drop.get(r["sku"]), 0) if x is not None)
            got = (before - page.price) / before * 100 if before > page.price else 0
            if page.in_stock is False or before <= floor or got < pct:
                continue  # الصفحة لا تؤكده الآن؛ يُعاد فحصه في التشغيل القادم
            payload = {"sku": r["sku"], "name": r["name"], "url": r["url"], "price": page.price, "before": before,
                       "pct": round(got, 1), "seen_drop": seen_drop.get(r["sku"]),
                       "history": (self.cfg["site"].get("dashboard_url") or "").rstrip("/") + "/p/" + r["sku"]}
            if self.db.queue_alert("strong", r["sku"], page.price, payload, ts):
                self.stats["alerts"] += 1
                sent += 1
            seen[r["sku"]] = {"p": page.price, "t": ts}
        self.db.set("strong_seen", seen)
        self.db.set("strong_rule", rule)
        self.db.commit()
        if first:
            log.info("خصومات قوية: سُجّل %d منتج موجود دون إرسال (التنبيه للجديد فقط)", len(seen))

    def basis_before(self, sku):
        """السعر المرصود قبل آخر نزول (نفس أساس العروض في الموقع)، أو None إن لم يوجد نزول."""
        ref = deals.reference(deals.price_runs(self.db.periods(sku)), self.cfg["deals"])
        return ref[0] if ref else None

    # ---------- المراحل ----------
    def paged_scan(self, label, where=None, state_key=None):
        """يمر على كل صفحات الاستعلام. الصفحات متداخلة بـ OVERLAP منتجات حتى لا يضيع منتج
        إذا انحذف منتج قبله أثناء المسح. state_key يحفظ نقطة التوقف للاستكمال في التشغيل التالي."""
        st = (self.db.get(state_key) if state_key else None) or {"start": 0, "started_at": now(), "total": None}
        log.info("%s: من الموضع %d", label, st["start"])
        seen, fails, suspicious = set(), 0, 0
        while self.left() > 90:
            total, products = self.listing(f"{label} start={st['start']}", start=st["start"], where=where)
            if total is None:
                fails += 1
                if fails >= 3:
                    break
                continue
            # محرك البحث أحياناً يرجع إجمالياً أصغر بكثير لطلب واحد: نعيد الطلب مرتين قبل قبوله
            odd = st["total"] and (total < 0.9 * st["total"] or (not products and st["start"] < total))
            if odd and suspicious < 2:
                suspicious += 1
                self.error(label, f"استجابة غير متسقة عند {st['start']}: الإجمالي {total} بدل {st['total']}")
                time.sleep(5)
                continue
            fails = suspicious = 0
            fresh = [p for p in products if p.sku not in seen]
            seen.update(p.sku for p in products)
            self.process(fresh)
            st["total"] = total
            if st["start"] + PAGE_SIZE >= total:
                if state_key:
                    self.db.set(state_key, None)
                return st, True
            st["start"] += PAGE_SIZE - OVERLAP
            if state_key:
                self.db.set(state_key, st)
                self.db.commit()
        log.info("%s: توقف عند %d/%s وسيكمل لاحقاً", label, st["start"], st["total"])
        return st, False

    def full_scan(self):
        """المسح الأول (تأسيس الأسعار) في الخيط الرئيسي. بعده يجري المسح الكامل في المسار الخلفي."""
        st = self.db.get("full_scan")
        self.phases.append(f"full@{st['start'] if st else 0}")
        st, done = self.paged_scan("مسح كامل", state_key="full_scan")
        if done:
            self.full_done(st)

    def full_done(self, st):
        seen = self.db.conn.execute("SELECT COUNT(*) FROM products WHERE last_seen>=?", (st["started_at"],)).fetchone()[0]
        if seen < st["total"]:
            self.error("full", f"اكتمل المسح لكن ظهر {seen} من {st['total']} منتج")
        self.sitemap_queue(st["started_at"])
        ts = now()
        if st["total"] and seen >= 0.9 * st["total"]:
            n = self.db.delist_unseen(st["started_at"], ts)
            if n:
                log.info("%d منتج لم يعد ظاهراً في الكتالوج", n)
        self.db.set("last_full_done_at", ts)
        if not self.baseline_done:
            self.db.set("baseline_done", True)
            self.db.set("baseline_done_at", ts)
            log.info("اكتمل المسح الأول (تأسيس الأسعار) — التنبيهات تبدأ من التشغيل القادم")
        self.db.commit()

    def sitemap_queue(self, since):
        """بعد كل مسح كامل: قائمة المنتجات التي لها صفحات حية لكن محرك البحث لم يرجعها.
        تُفحص من صفحاتها في المسار الخلفي على دفعات عبر التشغيلات."""
        sc = self.cfg["scan"]
        if self.due("last_sitemap_at", sc["sitemap_every_hours"] * 3600, 30 * 60):
            try:
                mapping = self.ex.sitemap_products()
            except FetchError as e:
                self.error("sitemap", e)
                mapping = None
            if mapping:
                self.db.replace_sitemap(mapping)
                self.db.set("last_sitemap_at", now())
                self.db.commit()
            self.menu_refresh()
        self.db.set("sitemap_queue", self.db.sitemap_unseen(since, now(), sc["sitemap_mp_batch"]))
        self.sm_replaced = True
        self.db.commit()

    # ---------- المسار الخلفي (المسح الكامل وخريطة الموقع) ----------
    def start_lane(self, until, full):
        """يبدأ المسح الكامل (إن كان مستحقاً أو جارياً) وصفحات خريطة الموقع بالتوازي مع فحص العروض."""
        st = None
        if full:
            st = self.db.get("full_scan") or {"start": 0, "started_at": now(), "total": None}
            self.db.set("full_scan", st)
            self.db.commit()
            self.phases.append(f"full@{st['start']}")
        self.full_st, self.full_seen = st, set()
        self.sm_rows = self.db.get("sitemap_queue") or []
        self.sm_done, self.sm_replaced = 0, False
        if st is None and not self.sm_rows:
            return
        sc = self.cfg["scan"]
        if self.lane_ex is None:
            self.lane_http = Http(self.cfg["site"]["user_agent"], sc["delay_min"], sc["delay_max"], self.deadline)
            ex = Extra(self.lane_http, self.cfg["site"]["include_marketplace"], self.db.get("unbxd"))
        else:
            self.lane_http, ex = None, self.lane_ex
        self.lane = Lane(ex, st, self.sm_rows, until, self.deadline)
        self.lane.start()

    def finish_lane(self):
        if not self.lane:
            return
        self.drain(wait=True)
        self.lane.join()
        if self.lane_http:
            self.lane_http.close()
        if not self.sm_replaced:  # الباقي يكمل في التشغيل التالي
            self.db.set("sitemap_queue", self.sm_rows[self.sm_done:] or None)
        if self.sm_rows:
            self.phases.append(f"sitemap:{self.sm_done}/{len(self.sm_rows)}")
        self.lane = None
        self.db.commit()

    def fresh_only(self, products, t):
        """ما جلبه المسار الخلفي قبل رصد أحدث له في فحص العروض لا يُسجَّل (حتى لا يرجع سعر قديم)."""
        if not products:
            return products
        skus = [p.sku for p in products]
        newer = {r[0] for r in self.db.conn.execute(
            f"SELECT sku FROM products WHERE last_seen>=? AND sku IN ({','.join('?' * len(skus))})", (t, *skus))}
        return [p for p in products if p.sku not in newer]

    def drain(self, wait=False):
        """يسجل ما جلبه المسار الخلفي. wait=True: ينتظر حتى ينتهي المسار."""
        lane = self.lane
        if not lane or self.lane_end or self._draining:
            return
        self._draining = True  # process() داخل التسجيل لا يعيد الدخول هنا
        try:
            while True:
                try:
                    item = lane.out.get(timeout=1) if wait else lane.out.get_nowait()
                except queue.Empty:
                    if wait:
                        continue  # المسار يضع «end» دائماً عند انتهائه
                    return
                kind = item[0]
                if kind == "end":
                    self.lane_end = True
                    return
                if kind == "error":
                    self.error(item[1], item[2])
                elif kind == "fail":
                    self.pages_failed += 1
                    self.error(item[1], item[2])
                elif kind == "list":
                    self._lane_list(*item[1:])
                elif kind == "page":
                    self._lane_page(*item[1:])
        finally:
            self._draining = False

    def _lane_list(self, t, start, total, products, bad, last):
        st = self.full_st
        self.pages_ok += 1
        self.parse_bad += bad
        fresh = [p for p in products if p.sku not in self.full_seen]
        self.full_seen.update(p.sku for p in products)
        self.process(self.fresh_only(fresh, t))
        st["total"] = total
        if last:
            self.db.set("full_scan", None)
            self.db.commit()
            self.full_done(st)
        else:
            st["start"] = start + PAGE_SIZE - OVERLAP
            self.db.set("full_scan", st)
            self.db.commit()

    def _lane_page(self, t, sku, url, page, err):
        self.sm_done += 1
        if isinstance(err, Blocked):
            self.error(f"sitemap {sku}", err)
        elif isinstance(err, Disallowed):  # robots.txt يمنعها (صفحات خدمات الضمان مثلاً)
            self.db.mark_sitemap(sku, t, dead_days=30)
        elif err is not None:
            if getattr(err, "status", None) in (404, 410):  # صفحة محذوفة: لا تُعاد قبل مدة
                self.db.mark_sitemap(sku, t, dead_days=7 if not sku.startswith("MP") else 30)
            else:
                self.error(f"sitemap {sku}", err)
        else:
            self.db.mark_sitemap(sku, t)
            if page.product and page.product.sku == sku and self.fresh_only([page.product], t):
                known = self.db.product(sku) is not None
                self.process([self.page_product(sku, page) if known else page.product], source="pdp")
            self.handle_page(sku, page, observe=False)
        self.db.commit()

    def offers_scan(self):
        """كل منتج عليه خصم أو علامة عرض أو شارة — وليس فقط الأعلى خصماً."""
        self.phases.append("offers")
        st, done = self.paged_scan("مسح العروض", where=OFFERS_FILTER)
        if done:  # وقت البداية، حتى لا يؤخر طول المسح موعد المسح التالي
            self.db.set("last_hot_at", st.get("started_at") or now())
        self.db.commit()

    def watch_scan(self):
        skus = sorted(self.watched_skus)
        for i in range(0, len(skus), 50):
            if self.left() < 120:
                break
            _, products = self.listing("watch", skus=skus[i:i + 50])
            self.process(products)

    def menu_refresh(self):
        """ترتيب أقسام إكسترا وفئاتها (مرة يومياً مع خريطة الموقع). يُحفظ فقط إذا بدا سليماً."""
        try:
            menu = self.ex.menu()
        except (FetchError, Disallowed) as e:
            self.error("menu", e)
            return
        if len(menu) >= 5:
            self.db.set("menu", menu)
        else:
            self.error("menu", "قائمة إكسترا غير مفهومة")

    def preorder_scan(self):
        """منتجات «الحجز المسبق» الحالية في إكسترا (طلب واحد عادة). القائمة تُستبدل كاملة فقط
        إذا نجح جلبها كله؛ عند الفشل تبقى السابقة كما هي."""
        skus, start = set(), 0
        while True:
            total, products = self.listing("preorder", start=start, where=f'productStatusAr_uFilter:"{dashboard.PREORDER_BADGE}"')
            if total is None:
                return
            self.process(products)
            skus |= {p.sku for p in products}
            start += PAGE_SIZE
            if start >= total or not products:
                break
        self.db.set("preorder", {"skus": sorted(skus), "at": now()})

    def page_product(self, sku, page):
        """سعر وتوفر الصفحة مع بيانات المنتج المحفوظة (الاسم والقسم من محرك البحث أدق)."""
        row = self.db.product(sku)
        p = page.product
        if not row or not p or page.price is None:
            return p
        return replace(p, name=row["name"], category=row["category"], url=row["url"] or p.url,
                       category_codes=json.loads(row["category_codes"] or "[]"),
                       badges=json.loads(row["badges"] or "[]"), marketplace=bool(row["marketplace"]))

    def handle_page(self, sku, page, observe=True):
        """يحفظ العروض المستخرجة من صفحة منتج، ويسجل سعرها كمشاهدة (ما لم يُطلب غير ذلك)."""
        if observe and page.product and page.price is not None and page.product.sku == sku:
            self.process([self.page_product(sku, page)], source="pdp")
        self.db.set_page_info(sku, page.variants, page.seller)
        self.pdp_ok += 1
        ts = now()
        self.db.mark_pdp(sku, ts)
        alerting = self.baseline_done and bool(self.db.get("offers_baseline_done", False))
        for raw in page.offers:
            self.save_offer(raw, [sku], ts, alerting)

    def fetch_page(self, sku, url):
        self.drain()
        try:
            return self.ex.product_page(url)
        except Blocked as e:
            self.blocked += 1
            self.error(f"pdp {sku}", e)
        except SiteChanged as e:
            self.pdp_changed += 1
            self.error(f"pdp {sku}", e)
        except (FetchError, Disallowed) as e:
            self.error(f"pdp {sku}", e)
        return None

    def pdp_scan(self):
        sc = self.cfg["scan"]
        ts = now()
        explicit = [str(w["sku"]) for w in self.watch if w.get("sku")]
        skus = self.db.products_for_pdp(sc["pdp_per_run"], ts_minus(ts, hours=sc["pdp_recheck_hours"]), explicit)
        skus += self.db.products_for_price_check(sc["price_check_per_run"], exclude=skus)
        self.phases.append(f"pdp:{len(skus)}")
        for sku in skus:
            if self.left() < 120 or self.blocked >= 3:
                break
            page = self.fetch_page(sku, self.db.product(sku)["url"])
            if page:
                self.handle_page(sku, page)
                self.db.commit()
        if self.pdp_ok:
            self.db.set("offers_baseline_done", True)

    def verify_prices(self):
        """قبل إرسال تنبيه النزول: يفتح صفحة المنتج ويتأكد أن السعر المعروض للمشتري هو نفسه."""
        items = sorted(self.to_verify, key=lambda x: -x[1]["drop_pct"])
        self.to_verify = []
        limit = self.cfg["alerts"]["verify_max"]
        for n, (ch, pa) in enumerate(items):
            page = None
            if n < limit and self.left() > 60 and self.blocked < 3:
                page = self.fetch_page(ch.sku, ch.url)
            if page and page.price is not None:
                self.handle_page(ch.sku, page, observe=False)
                if page.product and page.product.sku == ch.sku:
                    self.db.observe(self.page_product(ch.sku, page), now(), self.cfg["alerts"]["low_window_days"], source="pdp")
                if page.in_stock is False:
                    log.info("%s نافد في صفحته؛ لا تنبيه الآن", ch.sku)
                    continue
                if abs(page.price - ch.new_price) > 0.5:
                    log.info("سعر %s في الصفحة %s يختلف عن البحث %s", ch.sku, page.price, ch.new_price)
                    if page.price >= (ch.old_price or 0):
                        continue  # النزول غير مؤكد؛ المسح القادم سيلتقطه إن كان حقيقياً
                    ch.new_price = page.price
                    pa = alerts.price_alert(ch, self.cfg, self.watch)
                    if not pa:
                        continue
                pa["verified"] = True
            if self.db.queue_alert("price", ch.sku, pa["new"], pa, now()):
                self.stats["alerts"] += 1
        self.db.commit()

    def enrich_deals(self):
        """حالة العروض: مراجعة، ثم فتح صفحات ما ينتظر التأكيد (عرض جديد أو سعر تغيّر لعرض قائم)
        وصفحات العروض الحالية الناقصة (خيارات/بائع/صورة)، ثم مراجعة ثانية بعد التأكيد."""
        rules = self.cfg["deals"]
        deals.update_states(self.db, rules, now())
        todo = deals.pending(self.db)
        todo += [d["row"]["sku"] for d in sorted(deals.active(self.db), key=lambda d: -d["saving"])
                 if (not d["page_ok"] or not d["row"]["image"]) and d["row"]["sku"] not in todo]
        self.phases.append(f"deals:{len(todo)}")
        for sku in todo[:rules["enrich_per_run"]]:
            if self.left() < 60 or self.blocked >= 3:
                break
            page = self.fetch_page(sku, self.db.product(sku)["url"])
            if page:
                self.handle_page(sku, page)
                self.db.commit()
        deals.update_states(self.db, rules, now())
        self.db.commit()

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
        site_changed = self.db.get("site_hash") != dashboard.template_hash()
        if not (full_due or hot_due or pending or site_changed):
            log.info("لا يوجد عمل مستحق في هذا التشغيل")
            return False
        if not (full_due or hot_due or pending):
            return True  # فقط إعادة نشر الموقع بقالبه الجديد

        if full_due or hot_due:
            found = self.ex.discover()
            if found and found != self.db.get("unbxd"):
                self.db.set("unbxd", found)
        if not self.baseline_done:
            self.full_scan()           # المسح الأول (تأسيس الأسعار) دفعة واحدة
        else:
            # المسح الكامل وصفحات خريطة الموقع في مسار خلفي بالتوازي، حتى نهاية run_budget_minutes،
            # فلا يتأخر فحص العروض ولا ينتهي التشغيل بعد موعد التالي
            self.start_lane(self.started + sc["run_budget_minutes"] * 60, full_due)
            if hot_due:
                self.offers_scan()
        if self.baseline_done and (full_due or hot_due):
            self.watch_scan()
            self.preorder_scan()
            self.pdp_scan()
            self.banner_scan()
            self.verify_prices()
            self.enrich_deals()
            self.strong_scan()
        self.finish_lane()
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
    ap.add_argument("--test-notify", action="store_true", help="أرسل رسالة تجربة لكل القنوات المضبوطة ثم اخرج")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stdout)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # يمنع طباعة الروابط (رابط تيليجرام يحتوي التوكن)

    cfg = config.load(args.config)
    if args.test_notify:
        n = Notifier(cfg["secrets"])
        if not n.configured:
            log.error("لا توجد قناة مضبوطة: أضف TELEGRAM_BOT_TOKEN و TELEGRAM_CHAT_ID (أو SMTP_* أو NTFY_TOPIC) في GitHub Secrets")
            return 1
        h = "✅ <b>Sayd</b>: تم ربط التنبيهات بنجاح.\nستصلك هنا تنبيهات نزول الأسعار والعروض الجديدة."
        ch = n.send(h, alerts.plain(h), "Sayd ✅ تجربة التنبيهات", cfg["site"].get("dashboard_url"))
        n.close()
        log.info("نتيجة التجربة: %s", ch or "فشل الإرسال في كل القنوات")
        return 0 if ch else 1
    data = Path(args.data)
    data.mkdir(parents=True, exist_ok=True)
    db = DB(data / "sayd.db")
    db.gap_hours = cfg["scan"].get("gap_hours", 12)
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
        if runner.lane:
            runner.lane.halt.set()
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
    # البيانات تُبنى كل تشغيل وتُدفع لفرع live (يقرؤه الموقع مباشرة، بلا بناء في Cloudflare)؛
    # الموقع نفسه يُنشر كل publish_every_minutes، أو فوراً إذا تغيّر قالبه
    site_hash = dashboard.template_hash()
    publish_site = runner.due("last_site_at", sc["publish_every_minutes"] * 60, 10 * 60) or db.get("site_hash") != site_hash
    built = False
    last_site = db.get("last_site_at")  # الفهرس المنشور في Cloudflare من هذا الوقت
    since = max(x for x in (ts_minus(now(), days=2), last_site and ts_minus(last_site, minutes=15)) if x)
    try:
        dashboard.build(db, cfg, data / "site", since=since)
        built = True
        if publish_site:
            db.set("last_site_at", now())
            db.set("site_hash", site_hash)
        db.commit()
    except Exception:
        publish_site = False
        log.error("فشل توليد اللوحة:\n%s", traceback.format_exc())
    try:  # تقرير تغطية داخلي (يُحفظ مع قاعدة البيانات، لا يُنشر في الموقع)
        dashboard.write_coverage(db, cfg, data / "coverage.json")
    except Exception:
        log.error("فشل تقرير التغطية:\n%s", traceback.format_exc())
    ok = db.integrity_ok()
    db.close()
    if not ok:
        log.error("فحص سلامة قاعدة البيانات فشل — لن تُنشر")
        return 2
    (data / ".changed").write_text(now())
    if built:
        (data / ".live").write_text(now())
    if publish_site:
        (data / ".publish_site").write_text(now())
    log.info("انتهى: %s", runner.stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
