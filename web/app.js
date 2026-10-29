"use strict";
// صيد: يقرأ ملفات JSON الثابتة المولّدة بعد كل تحديث. للعرض فقط؛ لا يكتب أي بيانات ولا يطلب فحص المتجر.
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = (n) => Number(n).toLocaleString("en-US", { maximumFractionDigits: 2 });
const PAGE = 24, ENDED_PAGE = 60;
const HOUR = 3600000, GAP_MS = 12 * HOUR;  // أطول من 12 ساعة بلا رصد = انقطاع
const TZ = "Asia/Riyadh", LOCALE = "ar-SA-u-nu-latn-ca-gregory";
const RANGES = [["m1", "شهر", 30, "آخر شهر"], ["m3", "3 أشهر", 91, "آخر 3 أشهر"], ["m6", "6 أشهر", 182, "آخر 6 أشهر"], ["y1", "سنة", 365, "آخر سنة"], ["all", "الكل", 0, "منذ بدء الرصد"]];
const ICON = (id, cls = "icon") => `<svg class="${cls}" aria-hidden="true"><use href="#i-${id}"/></svg>`;
const NOIMG = `<div class="noimg" role="img" aria-label="لا توجد صورة">${ICON("img", "")}</div>`;

let deals = [], dealById = new Map(), groups = [], offers = [], cats = [], ended = [], updated = null, total = 0;
let state = { cat: "all", min: 0, max: Infinity, stock: false, sort: "new", limit: PAGE, endedLimit: ENDED_PAGE, tab: "products", q: "", raw: "", resLimit: PAGE };
let index = null, indexing = null, homeScroll = 0, current = null;
const shards = {};

// ---------- أدوات ----------
const norm = (s) => String(s || "").toLowerCase().replace(/[\u200b-\u200f\u202a-\u202e\u2066-\u2069]/g, " ")
  .replace(/[ً-ٟـ]/g, "").replace(/[أإآٱ]/g, "ا").replace(/ى/g, "ي").replace(/ة/g, "ه").replace(/ؤ/g, "و").replace(/ئ/g, "ي")
  .replace(/[٠-٩]/g, (d) => "٠١٢٣٤٥٦٧٨٩".indexOf(d)).replace(/[-_/\\.,،؛:;()«»"'+|*!؟?]+/g, " ").replace(/\s+/g, " ").trim();
const compact = (s) => norm(s).replace(/[^a-z0-9]/g, "");
const tokens = (q) => q.split(" ").filter(Boolean);

// أسماء المنتجات عربية والزائر قد يكتب بالإنجليزية أو بتهجئة أخرى: كل مجموعة كلمات متكافئة
const SYN = [
  ["samsung", "سامسونج", "سامسونغ"], ["apple", "ابل"], ["iphone", "ايفون"], ["ipad", "ايباد"], ["macbook", "ماك بوك", "ماكبوك"],
  ["airpods", "ايربودز", "اير بودز"], ["watch", "ساعه"], ["sony", "سوني"], ["playstation", "بلايستيشن", "بلاي ستيشن"],
  ["ps5", "بلايستيشن 5", "playstation 5"], ["ps4", "بلايستيشن 4"], ["lg", "ال جي"], ["huawei", "هواوي"], ["xiaomi", "شاومي"],
  ["redmi", "ريدمي"], ["honor", "هونر"], ["lenovo", "لينوفو"], ["hp", "اتش بي"], ["dell", "ديل"], ["asus", "اسوس"], ["acer", "ايسر"],
  ["philips", "فيليبس"], ["braun", "براون"], ["dyson", "دايسون"], ["nintendo", "نينتندو"], ["switch", "سويتش"],
  ["xbox", "اكس بوكس", "اكسبوكس"], ["tcl", "تي سي ال"], ["hisense", "هايسنس"], ["toshiba", "توشيبا"], ["sharp", "شارب"],
  ["jbl", "جي بي ال"], ["anker", "انكر"], ["canon", "كانون"], ["nikon", "نيكون"], ["tefal", "تيفال"], ["kenwood", "كينوود"],
  ["bosch", "بوش"], ["oppo", "اوبو"], ["realme", "ريلمي"], ["vivo", "فيفو"], ["nokia", "نوكيا"], ["microsoft", "مايكروسوفت"],
  ["logitech", "لوجيتك"], ["razer", "ريزر"], ["msi", "ام اس اي"], ["garmin", "جارمن"], ["beats", "بيتس"], ["bose", "بوز"],
  ["tv", "تلفزيون", "شاشه"], ["laptop", "لابتوب", "لاب توب"], ["mobile", "جوال", "موبايل", "هاتف"], ["tablet", "تابلت", "جهاز لوحي"],
  ["headphones", "سماعه", "سماعات"], ["inch", "بوصه", "انش"], ["gb", "جيجابايت", "جيجا"], ["tb", "تيرابايت", "تيرا"],
  ["airfryer", "قلايه هوائيه"], ["fridge", "ثلاجه"], ["washer", "غساله"], ["ac", "مكيف"], ["vacuum", "مكنسه"], ["coffee", "قهوه"],
].map((g) => g.map(norm));
const SYN_OF = new Map();
for (const g of SYN) for (const w of g) SYN_OF.set(w, g);

function plural(n, one, two, few, many) {
  return n === 1 ? one : n === 2 ? two : n >= 3 && n <= 10 ? `${n} ${few}` : `${n} ${many}`;
}
function span(ms) {
  const m = Math.max(1, Math.round(ms / 60000));
  if (m < 60) return plural(m, "دقيقة", "دقيقتين", "دقائق", "دقيقة");
  const h = Math.round(m / 60);
  if (h < 48) return plural(h, "ساعة", "ساعتين", "ساعات", "ساعة");
  const d = Math.round(h / 24);
  return plural(d, "يوم", "يومين", "أيام", "يومًا");
}
const ms = (t) => Date.parse(t.length === 16 ? t + ":00Z" : t);  // التخزين بتوقيت UTC، العرض بتوقيت السعودية
const ago = (ts) => "قبل " + span(Date.now() - ms(ts));
const thisYear = new Date().getFullYear();
function when(t, time = true) {
  const d = new Date(t), o = { timeZone: TZ, day: "numeric", month: "short" };
  if (d.getFullYear() !== thisYear) o.year = "numeric";
  if (time) Object.assign(o, { hour: "numeric", minute: "2-digit" });
  return d.toLocaleString(LOCALE, o);
}
const shortDay = (t) => new Date(t).toLocaleDateString(LOCALE, { timeZone: TZ, day: "numeric", month: "short" });

const imgUrl = (sku) => `https://media.extra.com/s/aurora/${encodeURIComponent(sku)}_800`;
function img(url, alt, w = 320) {
  if (!url) return NOIMG;
  const u = (x) => `${esc(url)}?w=${x}&amp;fmt=auto&amp;qlt=78`;
  return `<img src="${u(w)}" srcset="${u(240)} 240w, ${u(400)} 400w, ${u(640)} 640w" sizes="(max-width:650px) 46vw, (max-width:1000px) 30vw, 280px" width="${w}" height="${w}" loading="lazy" decoding="async" alt="${esc(alt)}">`;
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

// ---------- المظهر: تلقائي (حسب الجهاز) ← نهاري ← ليلي ----------
const THEMES = { auto: ["تلقائي", "auto"], light: ["نهاري", "sun"], dark: ["ليلي", "moon"] };
function getTheme() { try { return localStorage.getItem("theme") || "auto"; } catch { return "auto"; } }
function setTheme(t) {
  try { t === "auto" ? localStorage.removeItem("theme") : localStorage.setItem("theme", t); } catch { /* يبقى للجلسة فقط */ }
  if (t === "auto") delete document.documentElement.dataset.theme; else document.documentElement.dataset.theme = t;
  const [label, icon] = THEMES[t];
  $("theme").querySelector("use").setAttribute("href", "#i-" + icon);
  $("theme-label").textContent = label;
  $("theme").setAttribute("aria-label", "المظهر: " + label);
  if (current) drawHistory();
}
$("theme").addEventListener("click", () => {
  const order = ["auto", "light", "dark"];
  setTheme(order[(order.indexOf(getTheme()) + 1) % order.length]);
});
setTheme(getTheme());

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
    const k = d.g ? "g:" + d.g : "p:" + d.id;
    if (!byGroup.has(k)) byGroup.set(k, []);
    byGroup.get(k).push(d);
  }
  groups = [...byGroup.values()].map((all) => ({ all: all.sort((a, b) => b.sv - a.sv) }));
}

async function load() {
  renderSkeleton($("grid"));
  try {
    const [d, o] = await Promise.all([getJSON("/data/deals.json"), getJSON("/data/offers.json")]);
    deals = d.deals || []; cats = d.cats || []; updated = d.updated; total = d.products || 0; offers = o.offers || [];
    ended = d.ended || [];
    dealById = new Map(deals.map((x) => [x.id, x]));
    buildGroups();
    $("deal-count").textContent = groups.length;
    $("offer-count").textContent = offers.length;
    $("st-deals").textContent = money(groups.length);
    $("st-products").textContent = total ? money(total) : "—";
    if (updated) $("st-updated").textContent = "آخر تحديث " + ago(updated);
    $("stats").hidden = false;
    renderCats(); render(); renderCoupons(); renderEnded();
    if (state.q) renderResults();
  } catch (e) {
    $("grid").innerHTML = `<div class="empty"><h3>تعذّر تحميل البيانات</h3><p>تحقق من الاتصال وحاول مرة ثانية.</p><button class="btn3d primary" type="button" data-retry>إعادة المحاولة</button></div>`;
    $("result-count").textContent = "";
  }
}

// فهرس كل المنتجات المرصودة: يُحمّل عند أول استخدام للبحث فقط
function loadIndex() {
  if (!indexing) {
    indexing = getJSON("/data/index.json").then((d) => {
      index = d.items.map(([id, n, m, p, st, b]) => ({ id, n, m, p, st, b, nk: norm(n), key: norm(`${n} ${m} ${b || ""} ${id}`), mk: compact(m) }));
      return index;
    }).catch(() => { indexing = null; return null; });
  }
  return indexing;
}
function shard(id) {
  const k = id.slice(-3).toLowerCase();
  // ملف غير موجود = لا منتج مرصود بهذه الخانات
  if (!shards[k]) shards[k] = getJSON(`/data/p/${encodeURIComponent(k)}.json`).catch((e) => { delete shards[k]; if (/ 404$/.test(e.message)) return {}; throw e; });
  return shards[k];
}

// رمز المنتج من رابط إكسترا أو من الرمز نفسه
function skuFromInput(v) {
  const s = v.trim();
  const m = s.match(/extra\.com\/[^\s]*?\/p\/([A-Za-z0-9]+)/i) || s.match(/^\/?p\/([A-Za-z0-9]+)$/i);
  if (m) return { sku: m[1].toUpperCase(), url: true };
  if (/^(MP)?\d{6,}$/i.test(s)) return { sku: s.toUpperCase(), url: false };
  return null;
}

// ---------- البحث في كل المنتجات ----------
function search(q) {
  const ts = tokens(q), cq = compact(q);
  const alts = ts.map((t) => SYN_OF.get(t) || [t]);
  // الأرقام تطابق رقمًا كاملًا فقط: «65» لا تطابق «165 هرتز»
  const nums = ts.map((t) => /^\d+$/.test(t) ? new RegExp(`(^|[^0-9])${t}([^0-9]|$)`) : null);
  const out = [];
  for (const x of index) {
    const modelHit = cq.length >= 4 && x.mk && x.mk.includes(cq);
    let ok = modelHit, inName = 0;
    if (!ok) {
      ok = true;
      for (let i = 0; i < ts.length; i++) {
        const t = ts[i];
        if (nums[i]) { if (nums[i].test(x.key)) { if (nums[i].test(x.nk)) inName++; continue; } ok = false; break; }
        if (x.key.includes(t)) { if (x.nk.includes(t)) inName++; continue; }
        if (alts[i].some((a) => x.key.includes(a))) { inName++; continue; }
        if (t.length >= 3 && /[a-z0-9]/.test(t) && x.mk.includes(t.replace(/[^a-z0-9]/g, ""))) continue;
        ok = false; break;
      }
    }
    if (!ok) continue;
    let s = (modelHit ? (x.mk === cq ? 120 : 80) : 0) + inName * 6;
    if (x.nk.startsWith(ts[0])) s += 12;
    if (dealById.has(x.id)) s += 10;
    s += x.st === 1 ? 8 : x.st === 0 ? 3 : 0;
    s += Math.log10((x.p || 0) + 1) * 2;  // ميل خفيف للمنتج الأساسي على ملحقاته الرخيصة
    out.push([s, x]);
  }
  return out.sort((a, b) => b[0] - a[0] || a[1].n.length - b[1].n.length).map((r) => r[1]);
}

let lastHits = [];
async function renderResults() {
  const raw = state.raw.trim();
  const on = raw.length >= 2 || !!skuFromInput(raw);
  $("results").hidden = !on;
  $("home").hidden = on || !!current;
  $("product-view").hidden = on || !current;
  $("search-clear").hidden = !raw;
  if (!on) return;
  if (!index) {
    $("results-note").textContent = "جارٍ البحث في كل المنتجات…";
    renderSkeleton($("results-grid"), 4);
    $("results-more-zone").hidden = true;
    if (!(await loadIndex())) { $("results-grid").innerHTML = '<div class="empty"><h3>تعذّر تحميل البحث</h3><p>حاول مرة ثانية.</p></div>'; return; }
    if (state.raw.trim() !== raw) return;
  }
  const s = skuFromInput(raw);
  if (s) {
    lastHits = index.filter((x) => x.id === s.sku);
    if (!lastHits.length) {
      $("results-note").textContent = "";
      $("results-grid").innerHTML = `<div class="empty"><h3>لم نرصد المنتج ${esc(s.sku)} بعد</h3><p>يبدأ سجل سعره من أول مرة نرصده فيها.</p></div>`;
      $("results-more-zone").hidden = true;
      return;
    }
  } else lastHits = search(state.q);
  $("results-note").textContent = lastHits.length ? `${money(lastHits.length)} منتج` : "";
  $("results-grid").innerHTML = lastHits.length
    ? lastHits.slice(0, state.resLimit).map(resultCard).join("")
    : '<div class="empty"><h3>لا يوجد منتج مرصود بهذا الاسم</h3><p>جرّب اسمًا أقصر أو رقم الموديل أو الصق رابط المنتج من إكسترا.</p></div>';
  $("results-more-zone").hidden = lastHits.length <= state.resLimit;
}

function resultCard(x) {
  const d = dealById.get(x.id);
  const status = x.st === 1 ? "متوفر" : x.st === 0 ? "غير متوفر حاليًا" : "لم يعد معروضًا";
  return `<article class="card mini">
    <a class="pic" href="${plink(x.id)}" data-link tabindex="-1" aria-hidden="true"><img src="${imgUrl(x.id)}?w=320&amp;fmt=auto&amp;qlt=75" width="320" height="320" loading="lazy" decoding="async" alt="">${d ? `<span class="badge">−${Math.round(d.pc)}٪</span>` : ""}</a>
    <div class="body">
      <div class="meta">${x.b ? `<b>${esc(x.b)}</b>` : ""}<span>${esc(x.m)}</span></div>
      <a class="name" href="${plink(x.id)}" data-link>${esc(x.n)}</a>
      <div class="prices">${x.p ? `<span class="price">${money(x.p)} <span class="cur">ريال</span></span>` : ""}${d ? `<span class="old">${money(d.o)}</span>` : ""}</div>
      <div class="stock ${x.st === 1 ? "" : "out"}"><i></i>${status}</div>
      <div class="spacer"></div>
      <a class="btn3d ghost" href="${plink(x.id)}" data-link>سجل السعر</a>
    </div>
  </article>`;
}

// ---------- التخفيضات ----------
function matches(d) {
  if (state.cat !== "all" && d.c !== state.cat) return false;
  if (state.stock && !d.st) return false;
  return d.p >= state.min && d.p <= state.max;
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

const seller = (d) => d.mp ? (d.sl ? d.sl : "بائع آخر") : "إكسترا";
const plink = (id) => "/p/" + encodeURIComponent(id);

function card(g) {
  const d = g.rep, n = g.all.length;
  const spec = [d.m, ...(d.sp || []).map((s) => s.split(":").slice(1).join(":").trim())].filter(Boolean).join(" · ");
  return `<article class="card">
    <a class="pic" href="${plink(d.id)}" data-link tabindex="-1" aria-hidden="true">${img(d.img, "")}
      <span class="badge">−${Math.round(d.pc)}٪</span>
      ${d.lw && Date.now() - ms(d.fs) >= 7 * 24 * HOUR ? `<span class="flag">أقل سعر خلال ${span(Date.now() - ms(d.fs))} من الرصد</span>` : ""}
    </a>
    <div class="body">
      <div class="meta"><b>${esc(seller(d))}</b><span>${esc(d.c)}</span></div>
      <a class="name" href="${plink(d.id)}" data-link>${esc(d.n)}</a>
      <div class="spec" title="${esc(spec)}"><bdi>${esc(spec) || "&nbsp;"}</bdi></div>
      <div class="prices"><span class="price">${money(d.p)} <span class="cur">ريال</span></span><span class="old" aria-label="السعر السابق ${money(d.o)} ريال">${money(d.o)}</span></div>
      <span class="save">وفّر ${money(d.sv)} ريال</span>
      <div class="stock ${d.st ? "" : "out"}"><i></i>${d.st ? "متوفر" : "غير متوفر حاليًا"} · ${ago(d.t)}${n > 1 ? `<span class="opts">${n} خيارات</span>` : ""}</div>
      <div class="spacer"></div>
      <a class="btn3d primary" href="${esc(d.u)}" target="_blank" rel="noopener">افتح في إكسترا ${ICON("out")}</a>
    </div>
  </article>`;
}

function renderSkeleton(el, n = 8) {
  el.innerHTML = '<article class="card skeleton" aria-hidden="true"><div class="pic"></div><div class="body"><div class="sk"></div><div class="sk w60"></div><div class="sk w40"></div></div></article>'.repeat(n);
}

function activeFilters() {
  return (state.cat !== "all" ? 1 : 0) + (state.min > 0 || state.max < Infinity ? 1 : 0) + (state.stock ? 1 : 0);
}

function render() {
  const list = visible();
  const active = activeFilters();
  $("filter-count").hidden = !active; $("filter-count").textContent = active;
  $("result-count").innerHTML = `${list.length} منتج${active ? ' <button type="button" data-clear>إعادة الضبط</button>' : ""}`;
  if (!list.length) {
    $("grid").innerHTML = groups.length
      ? `<div class="empty"><h3>لا يوجد تخفيض يطابق اختيارك</h3><p>وسّع نطاق السعر أو اختر فئة أخرى.</p><button class="btn3d ghost" type="button" data-clear>إعادة الضبط</button></div>`
      : `<div class="empty"><h3>لا توجد تخفيضات مؤكدة حاليًا</h3><p>تظهر هنا فور تأكيدها.</p></div>`;
    $("more-zone").hidden = true;
  } else {
    $("grid").innerHTML = list.slice(0, state.limit).map(card).join("");
    $("more-zone").hidden = list.length <= state.limit;
    $("shown").textContent = `المعروض ${Math.min(state.limit, list.length)} من ${list.length}`;
  }
  document.querySelectorAll("#categories button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.cat === state.cat)));
}

function renderCats() {
  const counts = {};
  for (const g of groups) for (const c of new Set(g.all.map((d) => d.c))) counts[c] = (counts[c] || 0) + 1;
  const list = cats.filter((c) => counts[c]);
  $("categories").innerHTML = ['<button type="button" data-cat="all" aria-pressed="true">الكل</button>']
    .concat(list.map((c) => `<button type="button" data-cat="${esc(c)}" aria-pressed="false">${esc(c)}</button>`)).join("");
  $("categories").hidden = list.length < 2;
  $("category-filter").innerHTML = '<option value="all">كل الفئات</option>' + list.map((c) => `<option>${esc(c)}</option>`).join("");
}

// ---------- صفحة المنتج ----------
// القيمة بعد النقطتين قد تكون أرقامًا لاتينية (3840 x 2160) فتُعزل حتى لا ينقلب ترتيبها
function specChip(s) {
  const i = s.indexOf(":");
  return i < 0 ? `<li>${esc(s)}</li>` : `<li>${esc(s.slice(0, i))}: <bdi>${esc(s.slice(i + 1).trim())}</bdi></li>`;
}
async function showProduct(id) {
  const view = $("product-view");
  current = { id, r: null, range: "m1" };
  $("home").hidden = true; view.hidden = !!state.q; $("hero").classList.add("compact");
  view.innerHTML = `<a class="back" href="/" data-link>${ICON("back")}الرئيسية</a><div class="pd-wrap"><p class="found-note">جارٍ التحميل…</p></div>`;
  let r;
  try { r = (await shard(id))[id]; }
  catch { view.querySelector(".pd-wrap").innerHTML = '<div class="empty"><h3>تعذّر تحميل المنتج</h3><button class="btn3d primary" type="button" data-reload>إعادة المحاولة</button></div>'; return; }
  if (location.pathname !== plink(id)) return;
  if (!r) {
    document.title = "منتج غير مرصود | صيد";
    view.querySelector(".pd-wrap").innerHTML = `<div class="empty"><h3>لم نرصد هذا المنتج بعد</h3><p>يبدأ سجل سعره من أول مرة نرصده فيها.</p></div>`;
    return;
  }
  current.r = r;
  document.title = `${r.n} | صيد`;
  const d = r.deal && Math.abs(r.deal.p - r.p) < 0.005 ? r.deal : null;  // العرض يُعرض فقط إذا طابق آخر سعر مرصود
  const status = r.dl ? "لم يعد معروضًا في إكسترا" : r.st ? "متوفر للتوصيل" : "غير متوفر حاليًا";
  const variants = (r.vo || []).length ? `<div class="variants"><h4>خيارات أخرى للمنتج نفسه</h4>${
    [[id, r.gl || "هذا الخيار"], ...r.vo].map(([v, l]) => v === id
      ? `<span class="variant current">${esc(l)}</span>`
      : `<a class="variant" href="${plink(v)}" data-link>${esc(l || v)}</a>`).join("")}</div>` : "";
  view.querySelector(".pd-wrap").innerHTML = `
    <div class="pd">
      <div class="pd-art">${r.img ? img(r.img, r.n, 640) : NOIMG}</div>
      <div class="pd-info">
        <div class="meta"><b>${esc(seller(r))}</b><span>${esc(r.c)}</span></div>
        <h2 class="pd-name">${esc(r.n)}</h2>
        ${r.m ? `<div class="model">${esc(r.m)}</div>` : ""}
        ${(r.sp || []).length ? `<ul class="spec-chips">${r.sp.map(specChip).join("")}</ul>` : ""}
        <div class="price-panel">
          ${r.p ? `<div class="prices"><span class="price">${money(r.p)} <span class="cur">ريال</span></span>${d ? `<span class="old" aria-label="السعر السابق ${money(d.b)} ريال">${money(d.b)}</span>` : ""}</div>` : ""}
          ${d ? `<span class="save">وفّر ${money(d.sv)} ريال · نزول ${d.pc.toFixed(1)}٪ عن آخر سعر رُصد قبله</span>` : ""}
          <div class="stock ${r.st ? "" : "out"}"><i></i>${status}</div>
          <a class="btn3d primary" href="${esc(r.u)}" target="_blank" rel="noopener">افتح في إكسترا ${ICON("out")}</a>
        </div>
        ${variants}
        <section class="pd-chart" aria-labelledby="chart-title">
          <div class="chart-head"><h3 id="chart-title">سجل السعر</h3>
            <div class="ranges" role="group" aria-label="مدة الرسم">${RANGES.map(([k, l]) => `<button type="button" data-range="${k}" aria-pressed="${k === "m1"}">${l}</button>`).join("")}</div>
          </div>
          <div class="chart-wrap" id="chart"></div>
          <p class="chart-sum" id="chart-sum"></p>
          <p class="chart-note" id="chart-note"></p>
        </section>
      </div>
    </div>`;
  drawHistory();
}

// الرسم: كل فترة سعر خط أفقي من أول رصد إلى آخر رصد لها، بلا قيم مختلقة.
// الانتقال بين سعرين خط متقطع (التغيّر حدث في مكان ما بينهما)، والانقطاع الطويل فراغ.
function drawHistory() {
  if (!current || !current.r) return;
  const { r, range } = current, box = $("chart");
  if (!box) return;
  const all = (r.pr || []).map(([f, l, p, st, n]) => ({ f: ms(f), l: ms(l), p, st, n }));
  document.querySelectorAll("[data-range]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.range === range)));
  if (!all.length) {
    box.innerHTML = '<p class="found-note">لا يوجد سعر مرصود لهذا المنتج بعد.</p>';
    $("chart-sum").textContent = ""; $("chart-note").textContent = "";
    return;
  }
  const [, , days, label] = RANGES.find((x) => x[0] === range);
  const end = Math.max(...all.map((x) => x.l)), first = all[0].f;
  const start = days ? Math.max(first, end - days * 24 * HOUR) : first;
  const ps = all.filter((x) => x.l >= start);
  const shown = ps.map((x) => ({ ...x, f: Math.max(x.f, start) }));
  const lo = Math.min(...shown.map((x) => x.p)), hi = Math.max(...shown.map((x) => x.p));
  const last = all[all.length - 1];

  const W = Math.max(240, box.clientWidth || 320), H = W < 480 ? 176 : 210;
  const L = 8, R = 60, T = 14, B = 26;  // محور السعر على اليمين
  const pad = (hi - lo) * 0.2 || Math.max(hi * 0.04, 1);
  const y0 = lo - pad, y1 = hi + pad;
  const t0 = start, t1 = Math.max(end, start + HOUR);
  const x = (t) => L + ((t - t0) / (t1 - t0)) * (W - L - R);
  const y = (p) => T + (1 - (p - y0) / (y1 - y0)) * (H - T - B);
  const base = H - B;

  const yt = hi === lo ? [lo] : niceTicks(lo, hi);
  let g = yt.map((v) => `<line class="grid-l" x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}"/><text class="ax" x="${W - R + 8}" y="${y(v) + 4}">${money(v)}</text>`).join("");
  // تواريخ المحور؛ المدة القصيرة (أقل من يوم ونصف) تُكتب بالساعة
  const xt = dateTicks(t0, t1, W < 480 ? 3 : 5), short1 = t1 - t0 < 36 * HOUR;
  const xl = (t) => short1 ? new Date(t).toLocaleTimeString(LOCALE, { timeZone: TZ, hour: "numeric", minute: "2-digit" }) : shortDay(t);
  g += xt.map((t, i) => `<text class="ax" x="${x(t)}" y="${H - 7}" text-anchor="${i === 0 ? "start" : i === xt.length - 1 ? "end" : "middle"}">${xl(t)}</text>`).join("");

  let lines = "", trans = "", gaps = "", dots = "", area = "";
  shown.forEach((s, i) => {
    const xa = x(s.f), xb = x(s.l);
    if (xb - xa < 2) dots += `<circle class="pt" cx="${xa}" cy="${y(s.p)}" r="3.2"/>`;
    else { lines += `M${xa},${y(s.p)}H${xb}`; area += `M${xa},${y(s.p)}H${xb}V${base}H${xa}Z`; }
    const n = shown[i + 1];
    if (!n) return;
    if (n.f - s.l <= GAP_MS) trans += `M${xb},${y(s.p)}H${x(n.f)}V${y(n.p)}`;
    else gaps += `<rect class="gap-r" x="${xb}" y="${T}" width="${Math.max(1, x(n.f) - xb)}" height="${base - T}"/>`;
  });
  dots += `<circle class="now" cx="${x(last.l)}" cy="${y(last.p)}" r="4.5"/>`;
  const em = getComputedStyle(document.documentElement).getPropertyValue("--em-hi").trim() || "#17795f";
  box.innerHTML = `<svg class="chart" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="سجل السعر ${label}: من ${money(shown[0].p)} إلى ${money(last.p)} ريال">
    <defs><linearGradient id="g-area" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="${em}" stop-opacity=".2"/><stop offset="1" stop-color="${em}" stop-opacity="0"/></linearGradient></defs>
    ${g}${gaps}<path class="area" d="${area}"/><path class="trans" d="${trans}"/><path class="line" d="${lines}"/>${dots}
    <line class="cursor" id="cursor" y1="${T}" y2="${base}" visibility="hidden"/></svg><div class="tip" id="tip" hidden></div>`;

  const lowP = shown.reduce((a, b) => (b.p < a.p ? b : a));
  $("chart-sum").innerHTML = `أقل سعر ${label}: <b>${money(lowP.p)}</b> ريال · ${r.dl ? "آخر سعر" : "الحالي"}: <b>${money(last.p)}</b> ريال`;
  const shortRange = days && first > end - days * 24 * HOUR;
  $("chart-note").textContent = `${shortRange || !days ? "بدأ الرصد " + when(first, false) + " · " : ""}آخر تحقق ${when(end)} · الأوقات بتوقيت السعودية`;

  // التلميح عند المرور أو اللمس
  const svg = box.querySelector("svg"), tip = $("tip"), cur = $("cursor");
  const pick = (ev) => {
    const rect = svg.getBoundingClientRect();
    const px = Math.min(Math.max(ev.clientX - rect.left, L), W - R);
    const t = t0 + ((px - L) / (W - L - R)) * (t1 - t0);
    let i = shown.findIndex((s, k) => t <= s.l || (shown[k + 1] && t < shown[k + 1].f && t - s.l < shown[k + 1].f - t));
    if (i < 0) i = shown.length - 1;
    const s = shown[i], p = ps[i], prev = all[all.indexOf(p) - 1];
    const out = [`<b>${money(s.p)} ريال</b>${p.st ? "" : " · غير متوفر"}`];
    out.push(p === last ? `رُصد من ${when(p.f)} ومستمر حتى آخر تحقق ${when(p.l)}` : `رُصد من ${when(p.f)} إلى ${when(p.l)}`);
    out.push(p.n > 1 ? `المدة المرصودة: ${span(p.l - p.f || 60000)}` : "رُصد مرة واحدة");
    if (prev) {
      out.push(p.f - prev.l <= GAP_MS
        ? (Math.abs(prev.p - p.p) >= 0.005 ? `التغيّر من ${money(prev.p)} رُصد بين ${when(prev.l)} و${when(p.f)}` : "")
        : `انقطع الرصد قبلها من ${when(prev.l)} إلى ${when(p.f)}`);
    }
    tip.innerHTML = out.filter(Boolean).map((l) => `<div>${l}</div>`).join("");
    tip.hidden = false;
    cur.setAttribute("x1", px); cur.setAttribute("x2", px); cur.setAttribute("visibility", "visible");
    const tw = tip.offsetWidth, th = tip.offsetHeight;
    tip.style.left = Math.min(Math.max(px - tw / 2, 0), W - tw) + "px";
    // فوق الخط إن وُجدت مساحة، وإلا تحته
    const above = y(s.p) - th - 14;
    tip.style.top = (above >= 0 ? above : Math.min(y(s.p) + 14, H - th)) + "px";
  };
  const hide = () => { tip.hidden = true; cur.setAttribute("visibility", "hidden"); };
  svg.addEventListener("pointermove", pick);
  svg.addEventListener("pointerdown", pick);
  svg.addEventListener("pointerleave", (e) => { if (e.pointerType === "mouse") hide(); });
}

function niceTicks(lo, hi) {
  const raw = (hi - lo) / 3, mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw);
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(+v.toFixed(2));
  return out.length ? out : [lo, hi];
}
function dateTicks(t0, t1, n) {
  if (t1 - t0 < 36 * HOUR) return [t0, t1];
  return Array.from({ length: n }, (_, i) => t0 + ((t1 - t0) * i) / (n - 1));
}

// ---------- الكوبونات ----------
const KIND = { payment: "عرض دفع", code: "كود خصم", cashback: "كاش باك", installment: "تقسيط" };
function renderCoupons() {
  $("coupon-note").textContent = offers.length ? `${offers.length} عرض` : "";
  if (!offers.length) { $("coupons").innerHTML = '<div class="empty"><h3>لا توجد عروض أو كوبونات حاليًا</h3></div>'; return; }
  $("coupons").innerHTML = offers.map((o) => {
    const terms = [
      o.pct ? `خصم ${o.pct}٪` : "", o.amount && o.kind !== "installment" ? `خصم ${money(o.amount)} ريال` : "",
      o.max ? `بحد أقصى ${money(o.max)} ريال` : "", o.min ? `حد أدنى للشراء ${money(o.min)} ريال` : "",
      o.exp ? `ينتهي ${when(ms(o.exp), false)}` : "", o.count ? `على ${o.count} منتج متوفر` : "",
    ].filter(Boolean);
    const prods = (o.products || []).map((p) => `<a href="${plink(p.id)}" data-link title="${esc(p.n)}">${p.img ? `<img src="${esc(p.img)}?w=160&amp;fmt=auto&amp;qlt=70" width="82" height="82" loading="lazy" alt="">` : NOIMG}<span>${esc(p.n)}</span></a>`).join("");
    return `<article class="coupon">
      <div class="c-logo">${esc(o.who || KIND[o.kind])}</div>
      <div class="c-main">
        <span class="c-tag">${KIND[o.kind]}</span>
        <h3>${esc(o.title)}</h3>
        ${o.desc ? `<p>${esc(o.desc)}</p>` : ""}
        ${terms.length ? `<div class="terms">${terms.map((t) => `<span>${esc(t)}</span>`).join("")}</div>` : ""}
        ${o.code ? `<button class="btn3d gold small copy" type="button" data-copy="${esc(o.code)}">${esc(o.code)} · نسخ</button>` : ""}
        ${o.kind === "installment" ? '<div class="c-note">التقسيط ليس تخفيضًا في سعر المنتج.</div>' : ""}
        ${prods ? `<div class="c-products">${prods}</div>` : ""}
      </div>
    </article>`;
  }).join("");
}

// ---------- خرجت من العروض: أسماء فقط ----------
function renderEnded() {
  $("ended-note").textContent = ended.length ? `${ended.length} منتج · ابحث بالاسم لفتح سجل سعره` : "";
  $("ended").innerHTML = ended.length
    ? ended.slice(0, state.endedLimit).map((n) => `<li>${esc(n)}</li>`).join("")
    : '<li class="muted-row">لا توجد منتجات هنا حاليًا</li>';
  $("ended-more-zone").hidden = ended.length <= state.endedLimit;
}

// ---------- التنقل ----------
function route() {
  const m = location.pathname.match(/^\/p\/([^/]+)\/?$/);
  if (m) { showProduct(decodeURIComponent(m[1]).toUpperCase()); return; }
  current = null;
  document.title = "صيد";
  $("hero").classList.remove("compact");
  $("product-view").hidden = true; $("product-view").innerHTML = "";
  $("home").hidden = !!state.q;
  requestAnimationFrame(() => window.scrollTo(0, homeScroll));
}
function clearSearch() {
  $("search").value = ""; state.raw = ""; state.q = ""; state.resLimit = PAGE;
  $("results").hidden = true; $("search-clear").hidden = true;
}
function go(url) {
  if (!current && !state.q) homeScroll = window.scrollY;
  clearSearch();
  if (url !== location.pathname) history.pushState(null, "", url);
  if (url.startsWith("/p/")) window.scrollTo(0, 0);
  route();
}
window.addEventListener("popstate", () => { clearSearch(); route(); });

function switchTab(name) {
  state.tab = name;
  document.querySelectorAll("[data-tab]").forEach((b) => {
    const on = b.dataset.tab === name; b.setAttribute("aria-selected", String(on)); b.tabIndex = on ? 0 : -1;
  });
  for (const t of ["products", "coupons", "ended"]) $(t + "-panel").hidden = name !== t;
}
function clearFilters() {
  state = { ...state, cat: "all", min: 0, max: Infinity, stock: false, limit: PAGE };
  render();
}

let typing;
$("search").addEventListener("focus", loadIndex, { once: true });
$("search").addEventListener("input", (e) => {
  clearTimeout(typing);
  const v = e.target.value;
  const s = skuFromInput(v);
  if (s && s.url) { go(plink(s.sku)); e.target.blur(); return; }  // لصق رابط منتج يفتح صفحته مباشرة
  typing = setTimeout(() => {
    state.raw = v; state.q = norm(v); state.resLimit = PAGE;
    renderResults();
    if (!state.q) route();
  }, 140);
});
$("search-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const s = skuFromInput($("search").value);
  if (s) go(plink(s.sku));
  $("search").blur();
});
$("search-clear").addEventListener("click", () => { clearSearch(); route(); $("search").focus(); });
$("sort").addEventListener("change", (e) => { state.sort = e.target.value; state.limit = PAGE; render(); });
$("more").addEventListener("click", () => { state.limit += PAGE; render(); });
$("results-more").addEventListener("click", () => { state.resLimit += PAGE; renderResults(); });
$("ended-more").addEventListener("click", () => { state.endedLimit += ENDED_PAGE; renderEnded(); });
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
  const a = e.target.closest("a[data-link]");
  if (a && !e.ctrlKey && !e.metaKey && !e.shiftKey && e.button === 0) {
    e.preventDefault();
    go(a.getAttribute("href"));
    return;
  }
  const b = e.target.closest("button");
  if (!b) return;
  if (b.dataset.cat) { state.cat = b.dataset.cat; state.limit = PAGE; render(); }
  if (b.dataset.tab) switchTab(b.dataset.tab);
  if (b.dataset.close) $(b.dataset.close).close();
  if (b.dataset.range && current) { current.range = b.dataset.range; drawHistory(); }
  if (b.dataset.clear !== undefined) clearFilters();
  if (b.dataset.retry !== undefined) load();
  if (b.dataset.reload !== undefined) route();
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
document.querySelector(".seg").addEventListener("keydown", (e) => {
  const order = ["products", "coupons", "ended"], i = order.indexOf(state.tab);
  const next = { ArrowLeft: i + 1, ArrowRight: i - 1, Home: 0, End: order.length - 1 }[e.key];
  if (next === undefined) return;
  e.preventDefault();
  const name = order[(next + order.length) % order.length];
  switchTab(name); $(name + "-tab").focus();
});

// ميلان البطاقات ثلاثي الأبعاد مع لمعة تتبع المؤشر (أجهزة الفأرة فقط، ويُلغى مع تقليل الحركة)
if (matchMedia("(hover:hover) and (pointer:fine)").matches && !matchMedia("(prefers-reduced-motion:reduce)").matches) {
  let tilted = null;
  const reset = (c) => { c.style.removeProperty("--rx"); c.style.removeProperty("--ry"); };
  document.addEventListener("pointermove", (e) => {
    const c = e.target.closest && e.target.closest(".card:not(.skeleton)");
    if (tilted && tilted !== c) { reset(tilted); tilted = null; }
    if (!c) return;
    const r = c.getBoundingClientRect(), px = (e.clientX - r.left) / r.width, py = (e.clientY - r.top) / r.height;
    c.style.setProperty("--ry", ((px - 0.5) * 7).toFixed(2) + "deg");
    c.style.setProperty("--rx", ((0.5 - py) * 6).toFixed(2) + "deg");
    c.style.setProperty("--mx", (px * 100).toFixed(1) + "%");
    c.style.setProperty("--my", (py * 100).toFixed(1) + "%");
    tilted = c;
  }, { passive: true });
  document.addEventListener("pointerleave", () => { if (tilted) reset(tilted); });
}

// إعادة الرسم فقط إذا تغيّر العرض (شريط عنوان الجوال يغيّر الارتفاع أثناء التمرير)
let resizing;
window.addEventListener("resize", () => {
  clearTimeout(resizing);
  resizing = setTimeout(() => { const svg = document.querySelector("#chart svg"); if (svg && $("chart").clientWidth !== +svg.getAttribute("width")) drawHistory(); }, 150);
});
// تحديث المظهر التلقائي عند تغيّر وضع الجهاز
matchMedia("(prefers-color-scheme:dark)").addEventListener("change", () => { if (current) drawHistory(); });

load();
route();
