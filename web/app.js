"use strict";
// صيد: يقرأ ملفات JSON الثابتة المولّدة بعد كل تحديث. للعرض فقط؛ لا يكتب أي بيانات ولا يطلب فحص المتجر.
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = (n) => Number(n).toLocaleString("en-US", { maximumFractionDigits: 2 });
const PAGE = 24, ENDED_PAGE = 60, FOUND = 8;
const HOUR = 3600000, GAP_MS = 12 * HOUR;  // أطول من 12 ساعة بلا رصد = انقطاع
const TZ = "Asia/Riyadh", LOCALE = "ar-SA-u-nu-latn-ca-gregory";
const RANGES = [["m1", "شهر", 30, "آخر شهر"], ["m3", "3 أشهر", 91, "آخر 3 أشهر"], ["m6", "6 أشهر", 182, "آخر 6 أشهر"], ["y1", "سنة", 365, "آخر سنة"], ["all", "الكل", 0, "منذ بدء الرصد"]];
const ARROW = '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="M7 17 17 7M7 7h10v10"/></svg>';
const NOIMG = '<div class="noimg" role="img" aria-label="لا توجد صورة"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.3" aria-hidden="true"><rect x="3" y="4" width="18" height="16" rx="3"/><circle cx="9" cy="10" r="2"/><path d="m21 16-5-5-9 9"/></svg></div>';

let deals = [], groups = [], offers = [], cats = [], ended = [], updated = null;
let state = { q: "", raw: "", cat: "all", min: 0, max: Infinity, stock: false, sort: "new", limit: PAGE, endedLimit: ENDED_PAGE, tab: "products" };
let index = null, indexing = null, homeScroll = 0, current = null;
const shards = {};

// ---------- أدوات ----------
const norm = (s) => String(s || "").toLowerCase()
  .replace(/[ً-ٟـ]/g, "").replace(/[أإآٱ]/g, "ا").replace(/ى/g, "ي").replace(/ة/g, "ه").replace(/ؤ/g, "و").replace(/ئ/g, "ي")
  .replace(/[٠-٩]/g, (d) => "٠١٢٣٤٥٦٧٨٩".indexOf(d));
const tokens = (q) => q.split(/\s+/).filter(Boolean);

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
    const [d, o] = await Promise.all([getJSON("/data/deals.json"), getJSON("/data/offers.json")]);
    deals = d.deals || []; cats = d.cats || []; updated = d.updated; offers = o.offers || []; ended = (d.ended || []).map((n) => ({ n, key: norm(n) }));
    buildGroups();
    $("deal-count").textContent = groups.length;
    $("offer-count").textContent = offers.length;
    if (updated) { $("updated").hidden = false; $("updated").lastElementChild.textContent = "آخر تحديث " + ago(updated); }
    renderCats(); render(); renderCoupons(); renderEnded();
  } catch (e) {
    $("grid").innerHTML = `<div class="empty"><h3>تعذّر تحميل التخفيضات</h3><p>تحقق من الاتصال وحاول مرة ثانية.</p><button class="retry" type="button" data-retry>إعادة المحاولة</button></div>`;
    $("coupons").innerHTML = "";
    $("result-count").textContent = "";
  }
}

// فهرس كل المنتجات المرصودة: يُحمّل عند أول استخدام للبحث فقط
function loadIndex() {
  if (!indexing) {
    indexing = getJSON("/data/index.json").then((d) => {
      index = d.items.map(([id, n, m, p, st]) => ({ id, n, m, p, st, key: norm(`${n} ${m} ${id}`), mk: norm(m) }));
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

// ---------- التخفيضات ----------
function matches(d) {
  if (state.cat !== "all" && d.c !== state.cat) return false;
  if (state.stock && !d.st) return false;
  if (d.p < state.min || d.p > state.max) return false;
  if (state.q) return tokens(state.q).every((t) => d.key.includes(t));
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

const seller = (d) => d.mp ? (d.sl ? "بائع: " + d.sl : "بائع آخر") : "إكسترا";
const plink = (id) => "/p/" + encodeURIComponent(id);

function card(g) {
  const d = g.rep, n = g.all.length;
  const spec = [d.m, ...(d.sp || []).map((s) => s.split(":").slice(1).join(":").trim())].filter(Boolean).join(" · ");
  return `<article class="card">
    <div class="picture">
      <a class="image-button" href="${plink(d.id)}" data-link aria-label="${esc(d.n)}" tabindex="-1">${img(d.img, d.n)}</a>
      <span class="badge">−${Math.round(d.pc)}٪</span>
      ${d.lw ? `<span class="lowest">أقل سعر خلال ${span(Date.now() - ms(d.fs))} من الرصد</span>` : ""}
    </div>
    <div class="card-body">
      <div class="merchant"><b>${esc(seller(d))}</b><span>${esc(d.c)}</span></div>
      <a class="name-button" href="${plink(d.id)}" data-link><h2 class="product-name">${esc(d.n)}</h2></a>
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

// ---------- البحث في كل المنتجات ----------
const RANK = { 1: 0, 0: 1, "-1": 2 };
async function renderFound() {
  const box = $("search-results"), raw = state.raw.trim();
  if (!raw || (raw.length < 2 && !skuFromInput(raw))) { box.hidden = true; box.innerHTML = ""; return; }
  box.hidden = false;
  if (!index) {
    box.innerHTML = '<p class="found-note">جارٍ البحث في كل المنتجات…</p>';
    if (!(await loadIndex())) { box.innerHTML = '<p class="found-note">تعذّر تحميل البحث. حاول مرة ثانية.</p>'; return; }
    if (state.raw.trim() !== raw) return;
  }
  const s = skuFromInput(raw);
  let hits;
  if (s) {
    hits = index.filter((x) => x.id === s.sku);
    if (!hits.length) {
      box.innerHTML = `<p class="found-note">لم نرصد المنتج ${esc(s.sku)} بعد. يبدأ سجل سعره من أول مرة نرصده فيها.</p>`;
      return;
    }
  } else {
    const ts = tokens(state.q);
    hits = index.filter((x) => ts.every((t) => x.key.includes(t)));
    const exact = (x) => (x.mk && x.mk === state.q ? 0 : 1);
    hits.sort((a, b) => exact(a) - exact(b) || RANK[a.st] - RANK[b.st] || a.n.length - b.n.length);
  }
  const more = hits.length > FOUND ? `<p class="found-note">و${hits.length - FOUND} منتج آخر. أضف كلمة أو رقم الموديل لتضييق البحث.</p>` : "";
  box.innerHTML = hits.length
    ? `<div class="found-head">كل منتجات إكسترا المرصودة <span>${hits.length}</span></div><ul class="found">${hits.slice(0, FOUND).map((x) => `
      <li><a href="${plink(x.id)}" data-link>
        <img class="thumb" src="${imgUrl(x.id)}?w=96&amp;fmt=auto&amp;qlt=70" width="48" height="48" loading="lazy" alt="">
        <span class="f-name">${esc(x.n)}${x.m ? `<small>${esc(x.m)}</small>` : ""}</span>
        <span class="f-price">${x.p ? `${money(x.p)} <small>ريال</small>` : ""}<small class="${x.st === 1 ? "" : "out"}">${x.st === 1 ? "متوفر" : x.st === 0 ? "غير متوفر" : "لم يعد معروضًا"}</small></span>
      </a></li>`).join("")}</ul>${more}`
    : '<p class="found-note">لا يوجد منتج مرصود بهذا الاسم.</p>';
}

// ---------- صفحة المنتج ----------
async function showProduct(id) {
  const view = $("product-view");
  $("home").hidden = true; view.hidden = false;
  view.innerHTML = `<a class="back" href="/" data-link>→ التخفيضات</a><div class="pd"><p class="found-note">جارٍ التحميل…</p></div>`;
  let r;
  try { r = (await shard(id))[id]; }
  catch { view.querySelector(".pd").innerHTML = '<div class="empty"><h3>تعذّر تحميل المنتج</h3><button class="retry" type="button" data-reload>إعادة المحاولة</button></div>'; return; }
  if (location.pathname !== plink(id)) return;
  if (!r) {
    document.title = "منتج غير مرصود | صيد";
    view.querySelector(".pd").innerHTML = `<div class="empty"><h3>لم نرصد هذا المنتج بعد</h3><p>يبدأ سجل سعره من أول مرة نرصده فيها.</p></div>`;
    return;
  }
  current = { id, r, range: "m1" };
  document.title = `${r.n} | صيد`;
  const d = r.deal && Math.abs(r.deal.p - r.p) < 0.005 ? r.deal : null;  // العرض يُعرض فقط إذا طابق آخر سعر مرصود
  const spec = (r.sp || []).length ? `<ul class="spec-list">${r.sp.map((s) => `<li>${esc(s)}</li>`).join("")}</ul>` : "";
  const status = r.dl ? "لم يعد معروضًا في إكسترا" : r.st ? "متوفر للتوصيل" : "غير متوفر حاليًا";
  const variants = (r.vo || []).length ? `<div class="variants"><h4>خيارات أخرى للمنتج نفسه</h4>${
    [[id, r.gl || "هذا الخيار"], ...r.vo].map(([v, l]) => v === id
      ? `<span class="variant current">${esc(l)}</span>`
      : `<a class="variant" href="${plink(v)}" data-link>${esc(l || v)}</a>`).join("")}</div>` : "";
  view.querySelector(".pd").innerHTML = `
    <div class="pd-top">
      <div class="pd-art">${r.img ? img(r.img, r.n, 640) : NOIMG}</div>
      <div class="pd-info">
        <div class="merchant"><b>${esc(seller(r))}</b><span>${esc(r.c)}</span></div>
        <h2 class="pd-name">${esc(r.n)}</h2>
        ${r.m ? `<div class="model">${esc(r.m)}</div>` : ""}
        ${spec}
        ${r.p ? `<div class="prices"><span class="price"><bdi>${money(r.p)}</bdi> <span class="currency">ريال</span></span>${d ? `<span class="old" aria-label="السعر السابق ${money(d.b)} ريال">${money(d.b)}</span>` : ""}</div>` : ""}
        ${d ? `<div class="save">وفّر ${money(d.sv)} ريال • نزول ${d.pc.toFixed(1)}٪ عن آخر سعر رصدناه قبل النزول</div>` : ""}
        <div class="stock ${r.st ? "" : "out"}"><i></i>${status}</div>
        ${variants}
        <a class="offer-btn" href="${esc(r.u)}" target="_blank" rel="noopener">افتح في إكسترا ${ARROW}</a>
      </div>
    </div>
    <section class="pd-chart" aria-labelledby="chart-title">
      <div class="chart-head"><h3 id="chart-title">سجل السعر</h3>
        <div class="ranges" role="group" aria-label="مدة الرسم">${RANGES.map(([k, l]) => `<button type="button" data-range="${k}" aria-pressed="${k === "m1"}">${l}</button>`).join("")}</div>
      </div>
      <div class="chart-wrap" id="chart"></div>
      <p class="chart-sum" id="chart-sum"></p>
      <p class="chart-note" id="chart-note"></p>
    </section>`;
  drawHistory();
}

// الرسم: كل فترة سعر خط أفقي من أول رصد إلى آخر رصد لها، بلا قيم مختلقة.
// الانتقال بين سعرين خط متقطع (التغيّر حدث في مكان ما بينهما)، والانقطاع الطويل فراغ.
function drawHistory() {
  if (!current) return;
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

  const W = Math.max(240, box.clientWidth || 320), H = W < 480 ? 168 : 196;
  const L = 8, R = 58, T = 12, B = 26;  // محور السعر على اليمين
  const pad = (hi - lo) * 0.18 || Math.max(hi * 0.04, 1);
  const y0 = lo - pad, y1 = hi + pad;
  const t0 = start, t1 = Math.max(end, start + HOUR);
  const x = (t) => L + ((t - t0) / (t1 - t0)) * (W - L - R);
  const y = (p) => T + (1 - (p - y0) / (y1 - y0)) * (H - T - B);

  // خطوط السعر الأفقية
  const yt = hi === lo ? [lo] : niceTicks(lo, hi);
  let g = yt.map((v) => `<line class="grid-l" x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}"/><text class="ax" x="${W - R + 6}" y="${y(v) + 4}">${money(v)}</text>`).join("");
  // تواريخ المحور؛ المدة القصيرة (أقل من يوم ونصف) تُكتب بالساعة
  const xt = dateTicks(t0, t1, W < 480 ? 3 : 5), short1 = t1 - t0 < 36 * HOUR;
  const xl = (t) => short1 ? new Date(t).toLocaleTimeString(LOCALE, { timeZone: TZ, hour: "numeric", minute: "2-digit" }) : shortDay(t);
  g += xt.map((t, i) => `<text class="ax" x="${x(t)}" y="${H - 7}" text-anchor="${i === 0 ? "start" : i === xt.length - 1 ? "end" : "middle"}">${xl(t)}</text>`).join("");

  let lines = "", trans = "", gaps = "", dots = "";
  shown.forEach((s, i) => {
    const xa = x(s.f), xb = x(s.l);
    if (xb - xa < 2) dots += `<circle class="pt" cx="${xa}" cy="${y(s.p)}" r="3"/>`;
    else lines += `M${xa},${y(s.p)}H${xb}`;
    const n = shown[i + 1];
    if (!n) return;
    if (n.f - s.l <= GAP_MS) trans += `M${xb},${y(s.p)}H${x(n.f)}V${y(n.p)}`;
    else gaps += `<rect class="gap-r" x="${xb}" y="${T}" width="${Math.max(1, x(n.f) - xb)}" height="${H - T - B}"/>`;
  });
  dots += `<circle class="now" cx="${x(last.l)}" cy="${y(last.p)}" r="4"/>`;
  box.innerHTML = `<svg class="chart" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="سجل السعر ${label}: من ${money(shown[0].p)} إلى ${money(last.p)} ريال">
    ${g}${gaps}<path class="trans" d="${trans}"/><path class="line" d="${lines}"/>${dots}
    <line class="cursor" id="cursor" y1="${T}" y2="${H - B}" visibility="hidden"/></svg><div class="tip" id="tip" hidden></div>`;

  const lowP = shown.reduce((a, b) => (b.p < a.p ? b : a));
  $("chart-sum").innerHTML = `أقل سعر ${label}: <b>${money(lowP.p)}</b> ريال · ${r.dl ? "آخر سعر" : "الحالي"}: <b>${money(last.p)}</b> ريال`;
  const short = days && first > end - days * 24 * HOUR;
  $("chart-note").textContent = `${short || !days ? "بدأ الرصد " + when(first, false) + " · " : ""}آخر تحقق ${when(end)} · الأوقات بتوقيت السعودية`;

  // التلميح عند المرور أو اللمس
  const svg = box.querySelector("svg"), tip = $("tip"), cur = $("cursor");
  const pick = (ev) => {
    const rect = svg.getBoundingClientRect();
    const px = Math.min(Math.max(ev.clientX - rect.left, L), W - R);
    const t = t0 + ((px - L) / (W - L - R)) * (t1 - t0);
    let i = shown.findIndex((s, k) => t <= s.l || (shown[k + 1] && t < shown[k + 1].f && t - s.l < shown[k + 1].f - t));
    if (i < 0) i = shown.length - 1;
    const s = shown[i], p = ps[i], prev = all[all.indexOf(p) - 1];
    const lines = [`<b>${money(s.p)} ريال</b>${p.st ? "" : " · غير متوفر"}`];
    lines.push(p === last ? `رُصد من ${when(p.f)} ومستمر حتى آخر تحقق ${when(p.l)}` : `رُصد من ${when(p.f)} إلى ${when(p.l)}`);
    lines.push(p.n > 1 ? `المدة المرصودة: ${span(p.l - p.f || 60000)}` : "رُصد مرة واحدة");
    if (prev) {
      lines.push(p.f - prev.l <= GAP_MS
        ? (Math.abs(prev.p - p.p) >= 0.005 ? `التغيّر من ${money(prev.p)} رُصد بين ${when(prev.l)} و${when(p.f)}` : "")
        : `انقطع الرصد قبلها من ${when(prev.l)} إلى ${when(p.f)}`);
    }
    tip.innerHTML = lines.filter(Boolean).map((l) => `<div>${l}</div>`).join("");
    tip.hidden = false;
    cur.setAttribute("x1", px); cur.setAttribute("x2", px); cur.setAttribute("visibility", "visible");
    const tw = tip.offsetWidth;
    tip.style.left = Math.min(Math.max(px - tw / 2, 0), W - tw) + "px";
    tip.style.top = Math.max(y(s.p) - tip.offsetHeight - 12, 0) + "px";
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

// ---------- العروض والكوبونات ----------
const KIND = { payment: "عرض دفع", code: "كود خصم", cashback: "كاش باك", installment: "تقسيط" };
function renderCoupons() {
  const q = state.q;
  const list = offers.filter((o) => !q || tokens(q).every((t) => norm([o.title, o.desc, o.code, o.who, KIND[o.kind]].join(" ")).includes(t)));
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
      o.exp ? `ينتهي ${when(ms(o.exp), false)}` : "", o.count ? `ظهر على ${o.count} منتج متوفر` : "",
    ].filter(Boolean);
    const logo = o.who || KIND[o.kind];
    const prods = (o.products || []).map((p) => `<a href="${plink(p.id)}" data-link title="${esc(p.n)}">${p.img ? `<img src="${esc(p.img)}?w=160&amp;fmt=auto&amp;qlt=70" width="78" height="78" loading="lazy" alt="">` : NOIMG}<span>${esc(p.n)}</span></a>`).join("");
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

// ---------- خرجت من العروض: أسماء فقط ----------
function renderEnded() {
  const list = ended.filter((e) => !state.q || tokens(state.q).every((t) => e.key.includes(t)));
  $("ended-note").textContent = ended.length ? `${list.length} منتج · ابحث بالاسم لفتح سجل سعره` : "";
  $("ended").innerHTML = list.length
    ? list.slice(0, state.endedLimit).map((e) => `<li>${esc(e.n)}</li>`).join("")
    : `<li class="muted-row">${ended.length ? "لا يوجد اسم يطابق البحث" : "لا توجد منتجات هنا حاليًا"}</li>`;
  $("ended-more-zone").hidden = list.length <= state.endedLimit;
}

// ---------- التنقل ----------
function route() {
  const m = location.pathname.match(/^\/p\/([^/]+)\/?$/);
  if (m) { showProduct(decodeURIComponent(m[1]).toUpperCase()); return; }
  current = null;
  document.title = "صيد | تخفيضات إكسترا";
  $("product-view").hidden = true; $("product-view").innerHTML = "";
  $("home").hidden = false;
  requestAnimationFrame(() => window.scrollTo(0, homeScroll));
}
function go(url) {
  if (!current) homeScroll = window.scrollY;
  if (url !== location.pathname) history.pushState(null, "", url);
  $("search-results").hidden = true;
  if (url.startsWith("/p/")) window.scrollTo(0, 0);
  route();
}
window.addEventListener("popstate", route);

function switchTab(name) {
  state.tab = name;
  document.querySelectorAll("[data-tab]").forEach((b) => {
    const on = b.dataset.tab === name; b.setAttribute("aria-selected", String(on)); b.tabIndex = on ? 0 : -1;
  });
  for (const t of ["products", "coupons", "ended"]) $(t + "-panel").hidden = name !== t;
}
function clearAll() {
  state = { ...state, q: "", raw: "", cat: "all", min: 0, max: Infinity, stock: false, limit: PAGE, endedLimit: ENDED_PAGE };
  $("search").value = ""; render(); renderCoupons(); renderEnded(); renderFound();
}

let typing;
$("search").addEventListener("focus", loadIndex, { once: true });
$("search").addEventListener("input", (e) => {
  clearTimeout(typing);
  const v = e.target.value;
  const s = skuFromInput(v);
  if (s && s.url) { go(plink(s.sku)); return; }  // لصق رابط منتج يفتح صفحته مباشرة
  typing = setTimeout(() => {
    state.raw = v; state.q = norm(v.trim()); state.limit = PAGE; state.endedLimit = ENDED_PAGE;
    render(); renderCoupons(); renderEnded(); renderFound();
  }, 150);
});
$("search").addEventListener("keydown", (e) => {
  if (e.key !== "Enter") return;
  const s = skuFromInput(e.target.value);
  if (s) { e.preventDefault(); go(plink(s.sku)); }
  else e.target.blur();
});
$("sort").addEventListener("change", (e) => { state.sort = e.target.value; state.limit = PAGE; render(); });
$("more").addEventListener("click", () => { state.limit += PAGE; render(); });
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
    if (a.closest("#search-results")) { $("search").value = ""; state.raw = ""; }
    go(a.getAttribute("href"));
    return;
  }
  const b = e.target.closest("button");
  if (!b) return;
  if (b.dataset.cat) { state.cat = b.dataset.cat; state.limit = PAGE; render(); }
  if (b.dataset.tab) switchTab(b.dataset.tab);
  if (b.dataset.close) $(b.dataset.close).close();
  if (b.dataset.range && current) { current.range = b.dataset.range; drawHistory(); }
  if (b.dataset.clear !== undefined) clearAll();
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
document.querySelector(".tabs").addEventListener("keydown", (e) => {
  const order = ["products", "coupons", "ended"], i = order.indexOf(state.tab);
  const next = { ArrowLeft: i + 1, ArrowRight: i - 1, Home: 0, End: order.length - 1 }[e.key];
  if (next === undefined) return;
  e.preventDefault();
  const name = order[(next + order.length) % order.length];
  switchTab(name); $(name + "-tab").focus();
});
let resizing;
// إعادة الرسم فقط إذا تغيّر العرض (شريط عنوان الجوال يغيّر الارتفاع أثناء التمرير)
window.addEventListener("resize", () => {
  clearTimeout(resizing);
  resizing = setTimeout(() => { const svg = document.querySelector("#chart svg"); if (svg && $("chart").clientWidth !== +svg.getAttribute("width")) drawHistory(); }, 150);
});

load();
route();
