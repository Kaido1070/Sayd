"""تحليل نصوص العروض: الكود، النسبة/القيمة، الحدود، تاريخ الانتهاء، والجهة المرتبطة."""
import hashlib
import re
from datetime import datetime, timezone

BANK, WALLET, INSTALLMENT, LOYALTY, UNKNOWN = "بنك", "محفظة", "تقسيط", "برنامج ولاء", "غير محدد"

# (نوع الجهة، الاسم، كلمات دالة) - الكلمات تُطابق بدون حساسية لحالة الأحرف
PROVIDERS = [
    (BANK, "الراجحي", ["الراجحي", "al rajhi", "alrajhi", "rajhi", "arb"]),
    (BANK, "الأهلي", ["البنك الأهلي", "الأهلي", "snb", "alahli", "al ahli", "ncb"]),
    (BANK, "الإنماء", ["الإنماء", "الانماء", "alinma"]),
    (BANK, "ساب", ["ساب", "sabb", "sab bank", "البنك السعودي الأول"]),
    (BANK, "بنك الرياض", ["بنك الرياض", "riyad bank", "riyadbank"]),
    (BANK, "البلاد", ["البلاد", "albilad", "bank albilad"]),
    (BANK, "الجزيرة", ["بنك الجزيرة", "aljazira", "al jazira"]),
    (BANK, "العربي الوطني", ["العربي الوطني", "البنك العربي", "anb"]),
    (BANK, "السعودي الفرنسي", ["الفرنسي", "bsf", "fransi"]),
    (BANK, "السعودي للاستثمار", ["للاستثمار", "saib"]),
    (BANK, "ميم", ["بنك ميم", "meem"]),
    (BANK, "D360", ["d360", "دال 360", "دال٣٦٠"]),
    (BANK, "بنك STC", ["stc bank", "بنك stc", "بنك اس تي سي"]),
    (BANK, "بطاقات مدى/فيزا/ماستركارد", ["فيزا", "visa", "ماستركارد", "mastercard", "مدى", "mada"]),
    (WALLET, "stc pay", ["stc pay", "stcpay", "اس تي سي باي", "إس تي سي باي"]),
    (WALLET, "urpay", ["urpay", "يو ار باي", "يور باي"]),
    (WALLET, "Apple Pay", ["apple pay", "applepay", "apple_pay", "أبل باي", "ابل باي"]),
    (WALLET, "Google Pay", ["google pay", "جوجل باي"]),
    (WALLET, "Barq", ["barq", "برق"]),
    (WALLET, "Flooss", ["flooss", "فلوس"]),
    (INSTALLMENT, "تابي", ["تابي", "tabby"]),
    (INSTALLMENT, "تمارا", ["تمارا", "tamara"]),
    (INSTALLMENT, "تسهيل", ["تسهيل", "tasheel"]),
    (INSTALLMENT, "بسيطة", ["بسيطة", "baseeta"]),
    (INSTALLMENT, "إمكان", ["إمكان", "امكان", "emkan"]),
    (INSTALLMENT, "مدفوع", ["مدفوع لاحقا", "madfu"]),
    (LOYALTY, "جود (اكسترا)", ["عضوية جود", "أعضاء جود", "عملاء جود", "برنامج جود", "jood"]),
    (LOYALTY, "قطاف", ["قطاف", "qitaf"]),
    (LOYALTY, "أكثر", ["برنامج أكثر", "akthr"]),
    (LOYALTY, "مكافأة", ["مكافأة", "mokafaa"]),
    (LOYALTY, "نقاط", ["نقاط", "points"]),
]

MONTHS = {
    "يناير": 1, "فبراير": 2, "مارس": 3, "أبريل": 4, "ابريل": 4, "إبريل": 4, "مايو": 5, "يونيو": 6,
    "يوليو": 7, "أغسطس": 8, "اغسطس": 8, "سبتمبر": 9, "أكتوبر": 10, "اكتوبر": 10, "نوفمبر": 11, "ديسمبر": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}

_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩٫٬", "0123456789.,")
N = r"(\d[\d,]*(?:\.\d+)?)"


def _n(s):
    return float(s.replace(",", "")) if s else None


def _first(patterns, text):
    for p in patterns:
        m = re.search(p, text, re.I)
        if m:
            return m
    return None


# كلمة كاملة مع سوابق عربية شائعة (و، ب، ل، بال...) حتى لا تطابق "ساب" داخل "حساب"
_PREFIX = r"(?<!\w)(?:و|ف|ب|ل|ك|بال|لل|وال|فال|وب|ول)?"
_PROVIDER_RX = [(kind, name, re.compile(_PREFIX + "(?:" + "|".join(re.escape(w) for w in words) + r")(?!\w)", re.I))
                for kind, name, words in PROVIDERS]


def provider_of(text, payment_modes=()):
    hay = " ".join([text, *payment_modes]).replace("_", " ")
    for kind, name, rx in _PROVIDER_RX:
        if rx.search(hay):
            return kind, name
    return UNKNOWN, ""


def parse_expiry(text, end_ms=None):
    if end_ms:
        return datetime.fromtimestamp(end_ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    t = text.lower()
    m = re.search(r"(\d{1,2})\s+(" + "|".join(MONTHS) + r")\s*,?\s*(\d{4})", t)
    if m:
        d, mo, y = int(m.group(1)), MONTHS[m.group(2)], int(m.group(3))
    else:
        m = re.search(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})", t) or None
        if m:
            d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        else:
            m = re.search(r"(\d{4})-(\d{2})-(\d{2})", t)
            if not m:
                return None
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        # نهاية اليوم بتوقيت السعودية (UTC+3)
        return datetime(y, mo, d, 20, 59, 59, tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None


def analyze(raw):
    """raw: extra.RawOffer -> dict جاهز للحفظ."""
    text = (raw.text or "").translate(_DIGITS)
    code = raw.code or ""
    if not code:
        m = _first([r"(?:كود|الكود|رمز|كوبون|code|coupon)\s*(?:الخصم|الكوبون)?\s*[:：]?\s*[\"'«]?([A-Z0-9][A-Z0-9_-]{2,24})\b"], text)
        code = m.group(1) if m else ""
    pct = _first([N + r"\s*[%٪]", r"(?:خصم|وفر|وفّر)\s*" + N + r"\s*(?:بالمئة|بالمائة|في المئة)"], text)
    amount = _first([r"(?:خصم|وفّر|وفر|توفير|استرداد|كاش ?باك)\s*(?:إضافي|اضافي)?\s*(?:بقيمة|بمبلغ)?\s*" + N + r"\s*(?:ريال|ر\.س|sar)"], text)
    max_disc = _first([r"(?:بحد|بحدٍ|حد)\s*(?:أقصى|اقصى)\s*(?:للخصم)?\s*(?:يصل إلى|قدره)?\s*" + N,
                       r"(?:up to|max(?:imum)?)\s*(?:sar)?\s*" + N], text)
    min_buy = _first([r"(?:الحد الأدنى للشراء|حد أدنى للشراء|بحد أدنى|الحد الأدنى)\s*(?:للطلب|للسلة)?\s*(?:هو)?\s*" + N,
                      r"(?:عند|على)\s*(?:الشراء|التسوق|المشتريات)\s*(?:التي)?\s*(?:بقيمة|بمبلغ|تزيد عن|فوق|أكثر من)\s*" + N,
                      r"(?:للمشتريات|لمشتريات)\s*(?:بقيمة)?\s*(?:فوق|أكثر من|تزيد عن|من)\s*" + N,
                      r"min(?:imum)?\s*(?:spend|purchase|order)?\s*(?:of)?\s*(?:sar)?\s*" + N], text)
    if raw.source == "installment":
        kind, name = INSTALLMENT, provider_of(text, raw.payment_modes)[1]
    else:
        kind, name = provider_of(text, raw.payment_modes)
    o = {
        "key": raw.key,
        "source": raw.source,
        "provider_type": kind,
        "provider": name,
        "code": code,
        "pct": _n(pct.group(1)) if pct else None,
        "amount": _n(amount.group(1)) if amount else raw.save_amount,
        "min_purchase": _n(min_buy.group(1)) if min_buy else None,
        "max_discount": _n(max_disc.group(1)) if max_disc else None,
        "expires_at": parse_expiry(text, raw.end_ms),
        "text": raw.text,
        "link": getattr(raw, "link", "") or "",
    }
    sig = "|".join(str(o[k]) for k in ("code", "pct", "amount", "min_purchase", "max_discount", "expires_at", "text"))
    o["content_hash"] = hashlib.sha1(sig.encode()).hexdigest()[:16]
    return o


def is_promo_banner(o):
    """البنرات كثيرة؛ نهتم فقط بما يرتبط بجهة دفع أو فيه كلمات عرض."""
    if o["provider_type"] != UNKNOWN or o["code"] or o["pct"]:
        return True
    return bool(re.search(r"خصم|عرض|كود|كاش|صفق|تخفيض|توفير|cashback|offer|promo|sale|deal", o["text"], re.I))
