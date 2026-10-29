"use strict";
// صيد: يقرأ ملفات JSON الثابتة المولّدة بعد كل تحديث. للعرض فقط؛ لا يكتب أي بيانات.
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = (n) => Number(n).toLocaleString("en-US", { maximumFractionDigits: 2 });
const PAGE = 24;
const ARROW = '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="M7 17 17 7M7 7h10v10"/></svg>';
const NOIMG = '<div class="noimg" role="img" aria-label="لا توجد صورة"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.3" aria-hidden="true"><rect x="3" y="4" width="18" height="16" rx="3"/><circle cx="9" cy="10" r="2"/><path d="m21 16-5-5-9 9"/></svg></div>';

let deals = [], groups = [], offers = [], cats = [], updated = null;
let state = { q: "", cat: "all", min: 0, max: Infinity, stock: false, sort: "new", limit: PAGE, tab: "products" };
const histCache = {};

// ---------- أدوات ----------
const norm = (s) => String(s || "").toLowerCase()
  .replace(/[ً-ٟـ]/g, "").replace(/[أإآٱ]/g, "ا").replace(/ى/g, "ي").replace(/ة/g, "ه").replace(/ؤ/g, "و").replace(/ئ/g, "ي")
  .replace(/[٠-٩]/g, (d) => "٠١٢٣٤٥٦٧٨٩".indexOf(d));

function plural(n, one, two, few, many) {
  return n === 1 ? one : n === 2 ? two : n >= 3 && n <= 10 ? `${n} ${few}` : `${n} ${many}`;
}
function span(ms) {
  const m = Math.max(1, Math.round(ms / 60000));
  if (m < 60) return plural(m, "دقيقة", "دقيقتين", "دقائق", "دقيقة");
  const h = Math.round(m / 60);
  if (h < 24) return plural(h, "ساعة", "ساعتين", "ساعات", "ساعة");
  const d = Math.round(h / 24);
  return plural(d, "يوم", "يومين", "أيام", "يومًا");
}
const ago = (ts) => "قبل " + span(Date.now() - Date.parse(ts));
const day = (ts) => new Date(ts).toLocaleDateString("ar-SA-u-nu-latn-ca-gregory", { day: "numeric", month: "long" });

function img(url, alt, w = 320) {
  if (!url) return NOIMG;
  const u = (x) => `${esc(url)}?w=${x}&amp;fmt=auto&amp;qlt=75`;
  return `<img src="${u(w)}" srcset="${u(240)} 240w, ${u(400)} 400w, ${u(640)} 640w" sizes="(max-width:650px) 46vw, (max-width:950px) 30vw, 270px" width="${w}" height="${w}" loading="lazy" decoding="async" alt="${esc(alt)}">`;
}
// الصورة التي تفشل تُستبدل ببديل ثابت
document.addEventListener("error", (e) => {
  const el = e.target;
  if (el.tagName === "IMG" && !el.dataset.failed) { el.dataset.failed = "1"; el.outerHTML = NOIMG; }
}, true);

let toastTimer;
function toast(msg) {
  $("toast").textContent = msg; $("toast").classList.add("show");
  clearTimeout(toastTimer); toastTimer = setTimeout(() => $("toast").classList.remove("show"), 3500);
}

// ---------- البيانات ----------
async function getJSON(path) {
  const r = await fetch(path, { cache: "no-cache" });
  if (!r.ok) throw new Error(path + " " + r.status);
  return r.json();
}

function buildGroups() {
  // نجمع فقط الخيارات التي يربطها المتجر نفسه (لون/سعة) — لا ندمج منتجات لتشابه أسمائها
  const byGroup = new Map();
  for (const d of deals) {
    d.key = norm([d.n, d.m, d.c, d.sl, d.gl, d.id].join(" "));
    const k = d.g ? "g:" + d.g : "p:" + d.id;
    if (!byGroup.has(k)) byGroup.set(k, []);
    byGroup.get(k).push(d);
  }
  groups = [...byGroup.values()].map((all) => ({ all: all.sort((a, b) => b.sv - a.sv) }));
}

async function load() {
  renderSkeleton();
  try {
    const [d, o] = await Promise.all([getJSON("data/deals.json"), getJSON("data/offers.json")]);
    deals = d.deals || []; cats = d.cats || []; updated = d.updated; offers = o.offers || [];
    buildGroups();
    $("deal-count").textContent = groups.length;
    $("offer-count").textContent = offers.length;
    if (updated) { $("updated").hidden = false; $("updated").lastElementChild.textContent = "آخر تحديث " + ago(updated); }
    renderCats(); render(); renderCoupons();
  } catch (e) {
    $("grid").innerHTML = `<div class="empty"><h3>تعذّر تحميل التخفيضات</h3><p>تحقق من الاتصال وحاول مرة ثانية.</p><button class="retry" type="button" data-retry>إعادة المحاولة</button></div>`;
    $("coupons").innerHTML = "";
    $("result-count").textContent = "";
  }
}

// ---------- التخفيضات ----------
function matches(d) {
  if (state.cat !== "all" && d.c !== state.cat) return false;
  if (state.stock && !d.st) return false;
  if (d.p < state.min || d.p > state.max) return false;
  if (state.q) return state.q.split(/\s+/).every((t) => d.key.includes(t));
  return true;
}
const SORTS = {
  new: (a, b) => b.t.localeCompare(a.t),
  saving: (a, b) => b.sv - a.sv,
  discount: (a, b) => b.pc - a.pc,
  price: (a, b) => a.p - b.p,
};
function visible() {
  const out = [];
  for (const g of groups) {
    const hit = g.all.filter(matches);
    if (hit.length) out.push({ rep: [...hit].sort(SORTS[state.sort])[0], all: g.all });
  }
  return out.sort((a, b) => SORTS[state.sort](a.rep, b.rep));
}

function card(g) {
  const d = g.rep, n = g.all.length;
  const seller = d.mp ? (d.sl ? "بائع: " + d.sl : "بائع آخر") : "إكسترا";
  const spec = [d.m, ...(d.sp || []).map((s) => s.split(":").slice(1).join(":").trim())].filter(Boolean).join(" · ");
  return `<article class="card">
    <div class="picture">
      <button class="image-button" type="button" data-detail="${esc(d.id)}" aria-label="تفاصيل ${esc(d.n)}">${img(d.img, d.n)}</button>
      <span class="badge">−${Math.round(d.pc)}٪</span>
      ${d.lw ? `<span class="lowest">أقل سعر خلال ${span(Date.now() - Date.parse(d.fs))} من الرصد</span>` : ""}
    </div>
    <div class="card-body">
      <div class="merchant"><b>${esc(seller)}</b><span>${esc(d.c)}</span></div>
      <button class="name-button" type="button" data-detail="${esc(d.id)}"><h2 class="product-name">${esc(d.n)}</h2></button>
      <div class="specline" title="${esc(spec)}">${esc(spec) || "&nbsp;"}</div>
      <div class="prices"><span class="price"><bdi>${money(d.p)}</bdi> <span class="currency">ريال</span></span><span class="old" aria-label="السعر السابق ${money(d.o)} ريال">${money(d.o)}</span></div>
      <div class="save">وفّر ${money(d.sv)} ريال • نزول ${d.pc.toFixed(1)}٪</div>
      <div class="stock ${d.st ? "" : "out"}"><i></i>${d.st ? "متوفر" : "غير متوفر حاليًا"} · ${ago(d.t)}${n > 1 ? `<span class="options">${n} خيارات</span>` : ""}</div>
      <div class="spacer"></div>
      <a class="offer-btn" href="${esc(d.u)}" target="_blank" rel="noopener">افتح في إكسترا ${ARROW}</a>
    </div>
  </article>`;
}

function renderSkeleton() {
  const sk = '<article class="card skeleton" aria-hidden="true"><div class="picture"></div><div class="card-body"><div class="sk"></div><div class="sk w60"></div><div class="sk w40"></div></div></article>';
  $("grid").innerHTML = sk.repeat(8);
  $("result-count").textContent = "جارٍ التحميل…";
}

function activeFilters() {
  return (state.cat !== "all" ? 1 : 0) + (state.min > 0 || state.max < Infinity ? 1 : 0) + (state.stock ? 1 : 0);
}

function render() {
  const list = visible();
  const active = activeFilters();
  $("filter-count").hidden = !active; $("filter-count").textContent = active;
  const reset = active || state.q ? ' <button type="button" data-clear>إعادة الضبط</button>' : "";
  $("result-count").innerHTML = `${list.length} منتج${reset}`;
  if (!list.length) {
    $("grid").innerHTML = groups.length
      ? `<div class="empty"><h3>ما لقينا تخفيض يطابق اختيارك</h3><p>جرّب كلمة أخرى أو وسّع نطاق السعر.</p><button class="text-btn" type="button" data-clear>مسح البحث والفلاتر</button></div>`
      : `<div class="empty"><h3>لا توجد تخفيضات مؤكدة حاليًا</h3><p>نتابع أسعار إكسترا باستمرار، وتظهر التخفيضات هنا فور تأكيدها.</p></div>`;
    $("more-zone").hidden = true;
  } else {
    $("grid").innerHTML = list.slice(0, state.limit).map(card).join("");
    $("more-zone").hidden = list.length <= state.limit;
    $("shown").textContent = `المعروض ${Math.min(state.limit, list.length)} من ${list.length}`;
  }
  document.querySelectorAll("#categories button").forEach((b) => {
    const on = b.dataset.cat === state.cat; b.classList.toggle("active", on); b.setAttribute("aria-pressed", String(on));
  });
}

function renderCats() {
  const counts = {};
  for (const g of groups) for (const c of new Set(g.all.map((d) => d.c))) counts[c] = (counts[c] || 0) + 1;
  const list = cats.filter((c) => counts[c]);
  $("categories").innerHTML = ['<button type="button" data-cat="all" class="active" aria-pressed="true">الكل</button>']
    .concat(list.map((c) => `<button type="button" data-cat="${esc(c)}" aria-pressed="false">${esc(c)}</button>`)).join("");
  $("categories").hidden = list.length < 2;
  $("category-filter").innerHTML = '<option value="all">كل الفئات</option>' + list.map((c) => `<option>${esc(c)}</option>`).join("");
}

// ---------- التفاصيل ----------
async function history(id) {
  const k = id.slice(-2);
  if (!histCache[k]) histCache[k] = getJSON(`data/h/${k}.json`).catch(() => ({}));
  return (await histCache[k])[id] || [];
}

function chart(points) {
  // نقاط حقيقية فقط: السعر ثابت حتى التغيير التالي، والخط يمتد حتى الآن
  const pts = points.map(([t, p]) => ({ t: Date.parse(t + ":00Z"), p }));
  const t0 = pts[0].t, t1 = Math.max(Date.now(), pts[pts.length - 1].t + 60000);
  const ps = pts.map((x) => x.p), lo = Math.min(...ps), hi = Math.max(...ps), pad = (hi - lo) * 0.15 || hi * 0.05 || 1;
  const W = 500, H = 130, x = (t) => 10 + ((t - t0) / (t1 - t0)) * (W - 20), y = (p) => 10 + (1 - (p - (lo - pad)) / (hi - lo + 2 * pad)) * (H - 20);
  let d = `M${x(pts[0].t)},${y(pts[0].p)}`;
  pts.slice(1).forEach((q) => { d += `H${x(q.t)}V${y(q.p)}`; });
  d += `H${x(t1)}`;
  const dots = pts.map((q) => `<circle cx="${x(q.t)}" cy="${y(q.p)}" r="4" fill="white" stroke="#08765e" stroke-width="2"><title>${day(q.t)}: ${money(q.p)} ريال</title></circle>`).join("");
  return `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="تغيّر السعر من ${money(pts[0].p)} إلى ${money(pts[pts.length - 1].p)} ريال">
    <path d="M10 ${y(hi)}H490M10 ${y(lo)}H490" stroke="#e5ece5" stroke-dasharray="4 4"/>
    <path d="${d}" fill="none" stroke="#08765e" stroke-width="3" stroke-linejoin="round"/>${dots}</svg>
    <div class="chart-labels"><span>الآن</span><span>${day(t0)}</span></div>`;
}

async function showDetail(id) {
  const d = deals.find((x) => x.id === id);
  if (!d) return;
  const g = groups.find((x) => x.all.includes(d));
  const seller = d.mp ? (d.sl ? "بائع: " + d.sl : "بائع آخر") : "إكسترا";
  const options = g && g.all.length > 1 ? `<div class="variants"><h4>خيارات أخرى عليها تخفيض</h4>${g.all.map((v) => `
      <div class="variant ${v.id === d.id ? "current" : ""}"><span class="v-label">${esc(v.gl || v.n)}<br><small class="${v.st ? "" : "old"}">${v.st ? "متوفر" : "غير متوفر"} · وفّر ${money(v.sv)}</small></span>
      <span class="v-price">${money(v.p)}</span>${v.id === d.id ? "" : `<button class="text-btn" type="button" data-detail="${esc(v.id)}">عرض</button>`}
      <a href="${esc(v.u)}" target="_blank" rel="noopener">إكسترا ↗</a></div>`).join("")}</div>` : "";
  $("detail-content").innerHTML = `
    <div class="detail-top">
      <div class="detail-art">${img(d.img, d.n, 640)}</div>
      <div class="detail-info">
        <div class="merchant"><b>${esc(seller)}</b><span>${esc(d.c)}</span></div>
        <h3>${esc(d.n)}</h3>
        ${d.m ? `<div class="model">${esc(d.m)}</div>` : ""}
        ${(d.sp || []).length ? `<ul class="spec-list">${d.sp.map((s) => `<li>${esc(s)}</li>`).join("")}</ul>` : ""}
        <div class="prices"><span class="price"><bdi>${money(d.p)}</bdi> <span class="currency">ريال</span></span><span class="old">${money(d.o)}</span></div>
        <div class="save">وفّر ${money(d.sv)} ريال • نزول ${d.pc.toFixed(1)}٪</div>
        <p class="detail-note">السعر السابق هو آخر سعر رصدناه قبل النزول. ${d.st ? "متوفر للتوصيل" : "غير متوفر حاليًا"} · نزل السعر ${ago(d.t)}.${d.lw ? ` هذا أقل سعر خلال ${span(Date.now() - Date.parse(d.fs))} من الرصد.` : ""}</p>
        <a class="offer-btn" href="${esc(d.u)}" target="_blank" rel="noopener">افتح في إكسترا ${ARROW}</a>
      </div>
    </div>
    ${options}
    <div class="history" id="history"><h4>سجل السعر</h4><small>جارٍ التحميل…</small></div>`;
  const dlg = $("detail");
  if (!dlg.open) dlg.showModal();
  dlg.scrollTop = 0;
  const h = await history(d.id);
  const box = $("history");
  if (!box) return;
  if (h.length < 2) { box.innerHTML = "<h4>سجل السعر</h4><small>لا يوجد سجل كافٍ بعد.</small>"; return; }
  const low = Math.min(...h.map((r) => r[1]));
  box.innerHTML = `<h4>سجل السعر</h4><small>رصد منذ ${day(h[0][0] + ":00Z")} (${span(Date.now() - Date.parse(h[0][0] + ":00Z"))}) • الأقل خلالها: ${money(low)} ريال</small>${chart(h)}`;
}

// ---------- العروض والكوبونات ----------
const KIND = { payment: "عرض دفع", code: "كود خصم", cashback: "كاش باك", installment: "تقسيط" };
function renderCoupons() {
  const q = state.q;
  const list = offers.filter((o) => !q || q.split(/\s+/).every((t) => norm([o.title, o.desc, o.code, o.who, KIND[o.kind]].join(" ")).includes(t)));
  $("coupon-note").textContent = offers.length ? `${list.length} عرض` : "";
  if (!list.length) {
    $("coupons").innerHTML = offers.length
      ? '<div class="empty"><h3>لا يوجد عرض يطابق البحث</h3><button class="text-btn" type="button" data-clear>مسح البحث</button></div>'
      : '<div class="empty"><h3>لا توجد عروض أو كوبونات حاليًا</h3></div>';
    return;
  }
  $("coupons").innerHTML = list.map((o) => {
    const terms = [
      o.pct ? `خصم ${o.pct}٪` : "", o.amount && o.kind !== "installment" ? `خصم ${money(o.amount)} ريال` : "",
      o.max ? `بحد أقصى ${money(o.max)} ريال` : "", o.min ? `حد أدنى للشراء ${money(o.min)} ريال` : "",
      o.exp ? `ينتهي ${day(o.exp)}` : "", o.count ? `ظهر على ${o.count} منتج متوفر` : "",
    ].filter(Boolean);
    const logo = o.who || KIND[o.kind];
    const prods = (o.products || []).map((p) => `<a href="${esc(p.u)}" target="_blank" rel="noopener" title="${esc(p.n)}">${p.img ? `<img src="${esc(p.img)}?w=160&amp;fmt=auto&amp;qlt=70" width="78" height="78" loading="lazy" alt="">` : NOIMG}<span>${esc(p.n)}</span></a>`).join("");
    return `<article class="coupon">
      <div class="coupon-logo">${esc(logo)}</div>
      <div class="coupon-main">
        <span class="coupon-tag">${KIND[o.kind]}</span>
        <h3>${esc(o.title)}</h3>
        ${o.desc ? `<p>${esc(o.desc)}</p>` : ""}
        ${terms.length ? `<div class="terms">${terms.map((t) => `<span>${esc(t)}</span>`).join("")}</div>` : ""}
        ${o.code ? `<button class="copy" type="button" data-copy="${esc(o.code)}">${esc(o.code)} ⧉ نسخ</button>` : ""}
        ${o.kind === "installment" ? '<div class="coupon-note">التقسيط ليس تخفيضًا في سعر المنتج.</div>' : ""}
        ${prods ? `<div class="coupon-products">${prods}</div>` : ""}
      </div>
    </article>`;
  }).join("");
}

// ---------- التفاعل ----------
function switchTab(name) {
  state.tab = name;
  document.querySelectorAll("[data-tab]").forEach((b) => {
    const on = b.dataset.tab === name; b.setAttribute("aria-selected", String(on)); b.tabIndex = on ? 0 : -1;
  });
  $("products-panel").hidden = name !== "products";
  $("coupons-panel").hidden = name !== "coupons";
  $("search").placeholder = name === "products" ? "وش المنتج اللي تدور عليه؟" : "ابحث في العروض والكوبونات";
}
function clearAll() {
  state = { ...state, q: "", cat: "all", min: 0, max: Infinity, stock: false, limit: PAGE };
  $("search").value = ""; render(); renderCoupons();
}

let typing;
$("search").addEventListener("input", (e) => {
  clearTimeout(typing);
  typing = setTimeout(() => { state.q = norm(e.target.value.trim()); state.limit = PAGE; render(); renderCoupons(); }, 120);
});
$("sort").addEventListener("change", (e) => { state.sort = e.target.value; state.limit = PAGE; render(); });
$("more").addEventListener("click", () => { state.limit += PAGE; render(); });
$("filter-open").addEventListener("click", () => {
  $("category-filter").value = state.cat; $("min-price").value = state.min || ""; $("max-price").value = state.max === Infinity ? "" : state.max;
  $("stock-only").checked = state.stock; $("budget-error").hidden = true; $("filters").showModal();
});
$("filter-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const min = Math.max(0, Number($("min-price").value) || 0), max = $("max-price").value === "" ? Infinity : Number($("max-price").value);
  if (max < min) { $("budget-error").hidden = false; return; }
  state = { ...state, cat: $("category-filter").value, min, max, stock: $("stock-only").checked, limit: PAGE };
  $("filters").close(); render();
});
$("reset").addEventListener("click", () => {
  $("category-filter").value = "all"; $("min-price").value = ""; $("max-price").value = ""; $("stock-only").checked = false; $("budget-error").hidden = true;
});
document.addEventListener("click", async (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  if (b.dataset.cat) { state.cat = b.dataset.cat; state.limit = PAGE; render(); }
  if (b.dataset.tab) switchTab(b.dataset.tab);
  if (b.dataset.close) $(b.dataset.close).close();
  if (b.dataset.detail) showDetail(b.dataset.detail);
  if (b.dataset.clear !== undefined) clearAll();
  if (b.dataset.retry !== undefined) load();
  if (b.dataset.copy) {
    try { await navigator.clipboard.writeText(b.dataset.copy); toast("تم نسخ الكود " + b.dataset.copy); }
    catch { toast("الكود: " + b.dataset.copy); }
  }
});
document.querySelectorAll("dialog").forEach((d) => d.addEventListener("click", (e) => {
  if (e.target !== d) return;
  const r = d.getBoundingClientRect();
  if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) d.close();
}));
document.querySelector(".tabs").addEventListener("keydown", (e) => {
  if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) return;
  e.preventDefault();
  const name = state.tab === "products" ? "coupons" : "products";
  switchTab(name); $(name + "-tab").focus();
});

load();
