"""كل ما يخص قراءة موقع اكسترا. لو تغيّر الموقع، التصليح هنا فقط.

المصادر:
- الأسعار: محرك البحث Unbxd الذي تستخدمه صفحات الأقسام (JSON، 100 منتج/طلب).
- نصوص العروض: JSON مضمّن في صفحة المنتج (ACC.config.productDetails).
- البنرات: محتوى Amplience المشار إليه في الصفحات.
"""
import hashlib
import html
import json
import logging
import re
from dataclasses import dataclass, field

from .net import FetchError

log = logging.getLogger("sayd.extra")

BASE = "https://www.extra.com"
LOCALE = "ar-sa"
DISCOVERY_PAGE = f"{BASE}/{LOCALE}/mobiles-tablets/c/2"
AMPLIENCE_JSON = "https://extra.cdn.content.amplience.net/content/id/{id}?depth=all&format=inlined&locale=ar-SA"
PAGE_SIZE = 100  # أقصى ما يقبله Unbxd

# قيم احتياطية إذا فشل استخراجها من الصفحة
DEFAULT_UNBXD = {
    "url": "https://search.unbxd.io/",
    "apiKey": "8fb45132f31d81ab46966cc135c24430",
    "siteKey": "ss-unbxd-auk-extra-saudi-ar-prod11541714990564",
}

FIELDS = ",".join([
    "productCode", "nameAr", "price", "wasPrice", "hdAvailableCities", "inStockFlag", "comingSoon",
    "isMarketPlaceItem", "urlAr", "allCategoriesAr", "familyId", "categories",
    "productStatusAr", "brandAr",
])

SORT_CODE = "productCode asc"           # ترتيب ثابت يسمح بالاستكمال
SORT_DISCOUNT = "priceValueDiscountPercentage desc"
# كل منتج عليه أي خصم أو علامة عرض أو شارة (كاش باك، هدية، عرض بنك...)
OFFERS_FILTER = 'priceValueDiscountPercentage:[0.01 TO 100] OR offersFacet_uFilter:* OR productStatusAr_uFilter:*'
SITEMAP_INDEX = f"{BASE}/{LOCALE}/sitemap.xml"


class SiteChanged(FetchError):
    """الاستجابة لا تشبه المتوقع (تغيّر الموقع أو الـ API)."""


@dataclass
class Product:
    sku: str
    name: str
    category: str
    category_codes: list
    url: str
    price: float | None
    was_price: float | None
    in_stock: bool
    marketplace: bool
    badges: list = field(default_factory=list)


@dataclass
class RawOffer:
    key: str
    source: str          # bank | bundle | installment | banner
    text: str
    code: str = ""
    payment_modes: list = field(default_factory=list)
    end_ms: int | None = None
    save_amount: float | None = None
    target_skus: list = field(default_factory=list)
    link: str = ""


@dataclass
class ProductPage:
    sku: str
    price: float | None
    was_price: float | None
    in_stock: bool | None
    offers: list
    product: Product | None = None   # منتج كامل لمن لا يظهر في محرك البحث


def _num(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def _text(fragment):
    t = re.sub(r"<[^>]+>", " ", fragment or "")
    t = html.unescape(t).replace("\xa0", " ")
    t = t.replace("€", "ريال")  # الموقع يعرض رمز اليورو خطأً في نصوص العروض
    return re.sub(r"\s+", " ", t).strip()


def _promo_id(code, text):
    """رمز العرض بصيغة <العرض>_<القاعدة>_<المنتج>: نأخذ الجزء الأول ليكون العرض واحداً لكل المنتجات."""
    code = str(code or "")
    return code.split("_")[0] if code else hashlib.sha1(text.encode()).hexdigest()[:12]


def _json_after(page, marker):
    i = page.find(marker)
    if i < 0:
        return None
    s = page.find("{", i)
    try:
        obj, _ = json.JSONDecoder().raw_decode(page[s:])
    except ValueError:
        return None
    return obj


class Extra:
    def __init__(self, http, include_marketplace=True, unbxd=None):
        self.http = http
        self.include_marketplace = include_marketplace
        self.unbxd = unbxd or dict(DEFAULT_UNBXD)

    # ---------- إعداد Unbxd ----------
    def discover(self):
        """يقرأ مفاتيح Unbxd الحالية من صفحة قسم (تتغير نادراً)."""
        try:
            page = self.http.get(DISCOVERY_PAGE).text
        except FetchError as e:
            log.warning("تعذّر قراءة صفحة القسم لاكتشاف الإعداد: %s", e)
            return None
        cfg = _json_after(page, "unbxdSearchConfig=")
        if not cfg or not cfg.get("apiKey") or not cfg.get("siteKey"):
            return None
        self.unbxd = {"url": cfg.get("url") or DEFAULT_UNBXD["url"], "apiKey": cfg["apiKey"], "siteKey": cfg["siteKey"]}
        return self.unbxd

    def _endpoint(self, kind):
        u = self.unbxd
        return f"{u['url'].rstrip('/')}/{u['apiKey']}/{u['siteKey']}/{kind}"

    # ---------- قوائم المنتجات ----------
    def listing(self, start=0, *, category=None, skus=None, where=None, sort=SORT_CODE):
        """صفحة من 100 منتج. يرجع (العدد الكلي، المنتجات، عدد المنتجات التي فشل تحليلها)."""
        params = {"rows": PAGE_SIZE, "start": start, "fields": FIELDS, "sort": sort}
        filters = ['type:"PRODUCT"']
        if where:
            filters.append(where)
        if skus:
            filters.append(" OR ".join(f'productCode:"{s}"' for s in skus))
        if category:
            params.update({"p": f'categories_uFilter:"{category}"', "pagetype": "boolean"})
            kind = "category"
        else:
            params["q"] = "*"
            kind = "search"
        params["filter"] = filters
        r = self.http.get(self._endpoint(kind), params=params, headers={"Origin": BASE, "Referer": BASE + "/"})
        try:
            data = r.json()
            resp = data["response"]
            total = int(resp.get("numberOfProducts", 0))
            raw = resp["products"]
        except (ValueError, KeyError, TypeError):
            raise SiteChanged("شكل استجابة Unbxd تغيّر") from None
        # Unbxd يحذف الحقل إذا كانت القائمة فارغة؛ غيابه من الصفحة كلها يعني أن الموقع تغيّر
        has_hd = any("hdAvailableCities" in p for p in raw)
        products, bad = [], 0
        for p in raw:
            try:
                prod = self._parse(p, has_hd)
            except (KeyError, TypeError, ValueError) as e:
                bad += 1
                log.warning("فشل تحليل منتج %s: %s", p.get("productCode"), e)
                continue
            if prod.marketplace and not self.include_marketplace:
                continue
            products.append(prod)
        return total, products, bad

    def _parse(self, p, has_hd=True):
        sku = str(p["productCode"]).strip()
        name = (p.get("nameAr") or "").strip()
        if not sku or not name:
            raise ValueError("SKU أو الاسم مفقود")
        # "متوفر" = يمكن توصيله لمدينة واحدة على الأقل (المتوفر للاستلام من المعرض فقط لا يُحسب)
        if has_hd:
            stock = bool(p.get("hdAvailableCities"))
        else:
            stock = str(p.get("inStockFlag")).lower() == "true"
        if str(p.get("comingSoon")).lower() == "true":
            stock = False
        cats = [c.strip() for c in p.get("allCategoriesAr") or [] if c and c.strip()]
        url = p.get("urlAr") or ""
        if url and not url.startswith("http"):
            url = f"{BASE}/{LOCALE}{url}"
        return Product(
            sku=sku,
            name=name,
            category=" > ".join(cats),
            category_codes=[str(c) for c in p.get("categories") or []],
            url=url,
            price=_num(p.get("price")),
            was_price=_num(p.get("wasPrice")),
            in_stock=stock,
            marketplace=str(p.get("isMarketPlaceItem")).lower() == "true",
            badges=[_text(b) for b in p.get("productStatusAr") or [] if b and b.strip()],
        )

    # ---------- صفحة المنتج ----------
    def product_page(self, url):
        page = self.http.get(url).text
        d = _json_after(page, "ACC.config.productDetails")
        if not d or "code" not in d:
            raise SiteChanged("بيانات صفحة المنتج غير موجودة")
        sku = str(d["code"])
        disc = _num((d.get("discountPrice") or {}).get("value"))
        base = _num((d.get("price") or {}).get("value"))
        stock_code = ((d.get("stock") or {}).get("stockLevelStatus") or {}).get("code")
        if d.get("stockAvailableForHD") is not None:   # "متوفر" = قابل للتوصيل، مثل قوائم البحث
            stock = bool(d["stockAvailableForHD"]) and stock_code != "outOfStock"
        else:
            stock = None if stock_code is None else stock_code != "outOfStock"
        price = disc or base
        cats = [c.get("name", "").strip() for c in d.get("categories") or []
                if re.match(r"^\d+(-\d+)*$", str(c.get("code", ""))) and c.get("name")]
        product = None
        if d.get("name"):
            product = Product(
                sku=sku, name=_text(d["name"]), category=" > ".join(cats),
                category_codes=[str(c.get("code")) for c in d.get("categories") or [] if c.get("code")],
                url=BASE + "/" + LOCALE + d["url"] if str(d.get("url", "")).startswith("/") else url,
                price=price, was_price=base if disc and base and base > disc else None,
                in_stock=bool(stock), marketplace=bool(d.get("marketPlaceProduct")) or sku.startswith("MP"),
            )
        offers = []
        for b in d.get("bankPromotionDetails") or []:
            text = _text(b.get("bankHtmlMessage"))
            if not text:
                continue
            codes = re.findall(r"<strong[^>]*>\s*([A-Za-z0-9_-]{3,30})\s*</strong>", b.get("bankHtmlMessage") or "")
            modes = [m for m in b.get("paymentModes") or [] if m]
            offers.append(RawOffer(
                key="bank:" + _promo_id(b.get("ruleCode"), text),
                source="bank",
                text=text,
                code=codes[0] if codes else "",
                payment_modes=modes,
            ))
        for p in d.get("potentialPromotions") or []:
            if p.get("excludeFromStorefrontDisplay"):
                continue
            save = _num((p.get("saveAmount") or {}).get("value"))
            msg = _text(p.get("description") or p.get("title") or "")
            if not msg:
                kind = "خصم إضافي بشروط الدفع" if p.get("cardTypeBankPromo") else "اشترِ هذا المنتج ووفّر على منتج آخر"
                msg = f"{kind}{f' بقيمة {save:g} ريال' if save else ''}"
            offers.append(RawOffer(
                key="promo:" + _promo_id(p.get("code"), msg),
                source="bank" if p.get("cardTypeBankPromo") or p.get("applePayPromo") else "bundle",
                text=msg,
                end_ms=p.get("endDate"),
                save_amount=save,
                target_skus=[str(s) for s in p.get("targetProducts") or []][:50],
                payment_modes=["APPLE_PAY"] if p.get("applePayPromo") else [],
            ))
        # عروض التقسيط: تُحفظ منفصلة ولا علاقة لها بالسعر
        for t in d.get("tasheelPriceValuesList") or []:
            months = t.get("tasheelInstallmentMonths")
            if months:
                offers.append(RawOffer(key=f"inst:tasheel:{months}", source="installment",
                                       text=f"تسهيل: تقسيط حتى {months} شهر", payment_modes=["TASHEEL"]))
        tabby = d.get("tabbyDetails") or {}
        if tabby.get("installments"):
            offers.append(RawOffer(key=f"inst:tabby:{tabby['installments']}", source="installment",
                                   text=f"تابي: تقسيم على {tabby['installments']} دفعات", payment_modes=["TABBY"]))
        coupon = d.get("couponData")
        if isinstance(coupon, dict) and coupon:
            text = _text(json.dumps(coupon, ensure_ascii=False))
            offers.append(RawOffer(key="coupon:" + str(coupon.get("code") or coupon.get("couponId") or text[:60]),
                                   source="bank", text=text, code=str(coupon.get("code") or "")))
        return ProductPage(
            sku=sku,
            price=price,
            was_price=base if disc and base and base > disc else None,
            in_stock=stock,
            offers=offers,
            product=product,
        )

    # ---------- خريطة الموقع ----------
    def sitemap_products(self):
        """كل روابط المنتجات في خريطة الموقع: {sku: url}. تُستخدم لاكتشاف ما لا يظهر في محرك البحث."""
        index = self.http.get(SITEMAP_INDEX).text
        files = [u for u in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", index) if "/Product-" in u]
        if not files:
            raise SiteChanged("خريطة الموقع لا تحتوي ملفات منتجات")
        out = {}
        for f in files:
            try:
                xml = self.http.get(f).text
            except FetchError as e:
                log.warning("تعذّر قراءة %s: %s", f.rsplit("/", 1)[-1], e)
                continue
            for url, sku in re.findall(r"<loc>\s*([^<\s]+/p/([0-9A-Za-z]+))\s*</loc>", xml):
                out[sku] = url
        return out

    # ---------- البنرات ----------
    def banners(self, paths):
        """أسماء وروابط البنرات (النص الفعلي غالباً داخل صورة، فنعتمد على الاسم والرابط)."""
        ids = set()
        for path in paths:
            try:
                page = self.http.get(BASE + path).text
            except FetchError as e:
                log.warning("تعذّر فتح %s: %s", path, e)
                continue
            ids.update(re.findall(r'"amplienceId"\s*:\s*"([0-9a-f-]{36})"', page))
        out = []
        for cid in sorted(ids):
            try:
                data = self.http.get(AMPLIENCE_JSON.format(id=cid)).json()
            except (FetchError, ValueError) as e:
                log.warning("تعذّر جلب محتوى البنر %s: %s", cid, e)
                continue
            for node in _walk(data.get("content") or {}):
                meta = node.get("_meta") or {}
                links = [l.get("url") for l in node.get("links") or [] if isinstance(l, dict) and l.get("url")]
                if meta.get("deliveryId") and (links or "Banner" in (meta.get("schema") or "")):
                    name = meta.get("name") or ""
                    text = " ".join(x for x in (name, node.get("title"), node.get("subtitle"), node.get("description")) if isinstance(x, str) and x)
                    link = links[0] if links else ""
                    out.append(RawOffer(key="banner:" + meta["deliveryId"], source="banner", text=_text(text),
                                        link=BASE + link if link.startswith("/") else link))
        return out


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)
