"""python -m unittest discover -s tests"""
import unittest
from datetime import datetime, timedelta, timezone

from sayd import categories, config, dashboard, deals
from sayd.db import DB
from sayd.extra import Product

RULES = config.load()["deals"]
T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)


def at(hours):
    return (T0 + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


class Base(unittest.TestCase):
    def setUp(self):
        self.db = DB(":memory:")

    def see(self, h, price, stock=True, sku="A", was=None, source="api"):
        p = Product(sku, "منتج " + sku, "قسم", ["1"], "https://www.extra.com/ar-sa/x/p/" + sku, price, was, stock, False, [])
        self.db.observe(p, at(h), 30, source=source)
        deals.update_states(self.db, RULES, at(h))

    def state(self, sku="A"):
        r = self.db.deal(sku)
        return r["status"] if r else None

    def periods(self, sku="A"):
        return [(p["price"], p["first_ts"], p["last_ts"], p["checks"]) for p in self.db.periods(sku)]


class Periods(Base):
    def test_same_price_extends_period_without_duplicates(self):
        for h in (0, 4, 8, 12):
            self.see(h, 1000)
        self.assertEqual(self.periods(), [(1000, at(0), at(12), 4)])
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0], 1)

    def test_change_records_both_prices_and_bounds(self):
        self.see(0, 1000); self.see(4, 1000); self.see(8, 800)
        self.assertEqual(self.periods(), [(1000, at(0), at(4), 2), (800, at(8), at(8), 1)])
        h = self.db.conn.execute("SELECT price, prev_price, ts FROM price_history ORDER BY id").fetchall()
        self.assertEqual([tuple(r) for r in h], [(1000, None, at(0)), (800, 1000, at(8))])

    def test_monitoring_gap_starts_new_period(self):
        self.see(0, 1000); self.see(30, 1000)   # انقطاع 30 ساعة > 12
        self.assertEqual(len(self.periods()), 2)

    def test_failed_or_zero_price_is_not_an_observation(self):
        self.see(0, 1000); self.see(4, None); self.see(8, 0)
        self.assertEqual(self.periods(), [(1000, at(0), at(0), 1)])

    def test_store_struck_price_kept_separately(self):
        self.see(0, 800, was=1500)
        p = self.db.periods("A")[0]
        self.assertEqual((p["price"], p["was_price"]), (800, 1500))


class DealStates(Base):
    def drop(self):
        for h in range(0, 48, 4):
            self.see(h, 1000)            # سعر ثابت يومين
        self.see(50, 800)                # نزول 20% (رصد واحد = غير مؤكد)

    def test_first_sight_is_not_a_drop(self):
        self.see(0, 500)
        self.assertIsNone(self.state())

    def test_unconfirmed_drop_waits_then_activates(self):
        self.drop()
        self.assertEqual(self.state(), "pending")
        self.see(54, 800)
        self.assertEqual(self.state(), "active")
        self.assertEqual(self.db.deal("A")["basis"], 1000)

    def test_page_check_confirms_immediately(self):
        self.drop()
        self.see(50.5, 800, source="pdp")
        self.assertEqual(self.state(), "active")

    def test_stable_price_and_failed_fetch_do_not_end_deal(self):
        self.drop(); self.see(54, 800)
        for h in range(58, 200, 4):
            self.see(h, None if h % 8 == 0 else 800)   # نصف الفحوص فشلت
        self.assertEqual(self.state(), "active")

    def test_deeper_drop_updates_same_card(self):
        self.drop(); self.see(54, 800); self.see(58, 700); self.see(62, 700)
        rows = self.db.conn.execute("SELECT COUNT(*), MAX(price), MAX(basis) FROM deal_state").fetchone()
        self.assertEqual(tuple(rows), (1, 700, 1000))

    def test_exit_and_return(self):
        self.drop(); self.see(54, 800)
        self.see(58, 980); self.see(62, 980)            # نزول 2% فقط = غير مؤهل
        self.assertEqual(self.state(), "ended")
        self.assertEqual(deals.ended(self.db), ["منتج A"])
        self.see(66, 980); self.see(70, 980)
        self.assertEqual(deals.ended(self.db), ["منتج A"])   # بدون تكرار
        self.see(74, 750); self.see(78, 750)            # عاد مؤهلاً
        self.assertEqual(self.state(), "active")
        self.assertEqual(deals.ended(self.db), [])
        self.assertEqual(len(self.db.periods("A")), 4)   # السجل لم يُحذف

    def test_black_friday_raise_then_discount_is_not_a_deal(self):
        for h in range(0, 120, 4):
            self.see(h, 1000)           # 1000 لخمسة أيام
        for h in range(120, 132, 4):
            self.see(h, 1300)           # رفع مؤقت 12 ساعة
        self.see(136, 1000); self.see(140, 1000)   # "تخفيض" للسعر الأصلي
        self.assertNotEqual(self.state(), "active")

    def test_long_standing_higher_price_counts_as_basis(self):
        for h in range(0, 24 * 40, 6):
            self.see(h, 1300)           # 1300 لأربعين يوماً
        self.see(24 * 40 + 2, 1000); self.see(24 * 40 + 6, 1000)
        self.assertEqual(self.state(), "active")
        self.assertEqual(self.db.deal("A")["basis"], 1300)

    def test_rules_require_both_pct_and_saving_or_big_saving(self):
        cases = [(300, 270, True), (250, 225, False), (2500, 2300, True), (2500, 2310, False)]
        for old, new, ok in cases:
            self.assertEqual(deals.qualifies(old, new, RULES), ok, (old, new))

    def test_removed_from_catalog_ends_deal(self):
        self.drop(); self.see(54, 800)
        self.db.conn.execute("UPDATE products SET delisted=1")
        deals.update_states(self.db, RULES, at(60))
        self.assertEqual(self.state(), "ended")

    def test_repeated_evaluation_does_not_duplicate(self):
        self.drop(); self.see(54, 800)
        for _ in range(3):
            deals.update_states(self.db, RULES, at(55))
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM deal_state").fetchone()[0], 1)


class Preorder(Base):
    def add(self, sku, badges, stock=True):
        p = Product(sku, "منتج " + sku, "قسم", ["1"], "https://www.extra.com/ar-sa/x/p/" + sku, 999, None, stock, False, badges)
        self.db.observe(p, at(0), 30)

    def ids(self):
        return [x["id"] for x in dashboard.preorder(self.db)]

    def test_badge_before_first_fetch(self):
        self.add("P1", [dashboard.PREORDER_BADGE]); self.add("P2", ["عرض خاص"])
        self.assertEqual(self.ids(), ["P1"])

    def test_fetched_list_replaces_and_drops_gone_products(self):
        self.add("P1", [dashboard.PREORDER_BADGE]); self.add("P2", [dashboard.PREORDER_BADGE], stock=False)
        self.db.set("preorder", {"skus": ["P1", "P2"], "at": at(1)})
        self.assertEqual(self.ids(), ["P1", "P2"])                 # المتاح للحجز أولاً
        self.db.set("preorder", {"skus": ["P2"], "at": at(2)})    # إكسترا أزال P1 من الطلب المسبق
        self.assertEqual(self.ids(), ["P2"])
        self.db.conn.execute("UPDATE products SET delisted=1 WHERE sku='P2'")
        self.assertEqual(self.ids(), [])


class Categories(unittest.TestCase):
    def test_clear_labels_from_extra_tree(self):
        cases = {
            "الأجهزة المنزلية الصغيرة > محضرات القهوة > القهوة": "القهوة",
            "الأجهزة المنزلية الصغيرة > محضرات القهوة > محضرات الكابتشينو": "ماكينات القهوة",
            "الأجهزة المنزلية الكبيرة > ثلاجات > كبيرة": "الثلاجات",
            "الأجهزة المنزلية الكبيرة > غسالات الملابس > تعبئة امامية": "الغسالات",
            "إلكترونيات > تلفزيونات > ميني إل إي دي": "التلفزيونات",
            "مستلزمات > أثاث > مراتب": "المراتب",
            "مستلزمات > أمتعة > مفرد": "حقائب السفر",
            "الجوالات و الأجهزة اللوحية > إكسسوارات > الأغلفة": "إكسسوارات الجوال",
            "العاب الكترونيه > مستلزمات > وحدات تحكم": "إكسسوارات الألعاب",
            "": "أخرى", None: "أخرى",
        }
        for path, want in cases.items():
            self.assertEqual(categories.label(path), want, path)

    def test_name_corrects_misfiled_products(self):
        cases = [
            ("كيون ماكينة قهوة 0.6 لتر، 1450 واط", "مستلزمات > منزل > الإكسسوارات المنزلية", "ماكينات القهوة"),
            ("استوديو ميدنايت بلاك مكينة التقطير", "الأجهزة المنزلية الصغيرة > محضرات القهوة > القهوة", "ماكينات القهوة"),
            ("بكج حبوب قهوة مختصة للاسبريسو", "الأجهزة المنزلية الصغيرة > محضرات القهوة > القهوة", "القهوة"),
            ("مارك، دفاية زيتية، 14 ريشة", "الأجهزة المنزلية الصغيرة > أجهزة العناية المنزلية > معالجة الهواء", "الدفايات"),
            ("بيريلا منشر غسيل", "الأجهزة المنزلية الصغيرة > أجهزة العناية المنزلية > كوايات", "العناية بالملابس"),
            ("بالسون طباخة الأرز الكهربائية", "الأجهزة المنزلية الصغيرة > أجهزة تحضير الأطعمة والمشروبات > طباخات", "أجهزة المطبخ الصغيرة"),
            ("صينية مكتب صغيرة من فينكس", "قرطاسية > قرطاسية > ملفات وتخزين", "القرطاسية"),
            ("موقد غاز (رأس دافور) رحلات", "مستلزمات > أثاث خارجي > مستلزمات الرحلات", "الرحلات والحدائق"),
            ("انستانت بوت قدر ضغط فوري", "الأجهزة المنزلية الصغيرة > أجهزة تحضير الأطعمة والمشروبات > قدور الضغط", "أجهزة المطبخ الصغيرة"),
        ]
        for name, path, want in cases:
            self.assertEqual(categories.label(path, name), want, name)


    def test_place_in_extra_menu(self):
        cases = [
            ("ال جي، غسالة تعبئة امامية، 9 كجم", "الأجهزة المنزلية الكبيرة > غسالات الملابس > تعبئة امامية",
             ("الأجهزة الكبيرة", "غسالات الملابس")),
            ("بكج حبوب قهوة مختصة", "الأجهزة المنزلية الصغيرة > محضرات القهوة > القهوة", ("مستلزمات المنزل", "القهوة")),
            ("كيون ماكينة قهوة", "مستلزمات > منزل > الإكسسوارات المنزلية", ("الأجهزة الصغيرة", "محضرات القهوة")),
            ("تي سي ال، مكيف جداري", "الأجهزة المنزلية الكبيرة > مكيفات > مكيفات سبليت", ("المكيفات", "المكيفات")),
            ("سامسونج، تلفزيون 65 بوصة", "إلكترونيات > تلفزيونات > ميني إل إي دي", ("الشاشات وأجهزة الترفيه", "الشاشات")),
        ]
        for name, path, want in cases:
            self.assertEqual(categories.place(path, name), want, name)

    def test_order_follows_extra_menu_despite_spelling(self):
        menu = [["الأجهزة الكبيرة", ["غسالات الملابس", "الثلاجات"]],
                ["مستلزمات المنزل", ["أمتعه السفر", "خلاطات المغاسل و السباحه", "لأدوات الكهربائية و اليدوية"]]]
        used = {"أخرى": {"أخرى"}, "مستلزمات المنزل": {"الأدوات الكهربائية واليدوية", "القهوة", "أمتعة السفر",
                                                    "خلاطات المغاسل والسباحة"},
                "الأجهزة الكبيرة": {"الثلاجات", "غسالات الملابس"}}
        self.assertEqual(categories.order(menu, used), [
            ["الأجهزة الكبيرة", ["غسالات الملابس", "الثلاجات"]],
            ["مستلزمات المنزل", ["أمتعة السفر", "خلاطات المغاسل والسباحة", "الأدوات الكهربائية واليدوية", "القهوة"]],
            ["أخرى", ["أخرى"]]])


if __name__ == "__main__":
    unittest.main()
