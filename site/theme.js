// المظهر قبل رسم الصفحة (لتجنّب الوميض): تلقائي حسب الجهاز ما لم يختر الزائر نهاري أو ليلي
try {
  const t = localStorage.getItem("theme");
  if (t === "light" || t === "dark") document.documentElement.dataset.theme = t;
} catch (e) { /* التخزين غير متاح: يبقى تلقائيًا */ }
