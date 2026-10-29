"use strict";
// لوحة Sayd: تقرأ ملفات JSON المولّدة بعد كل تشغيل
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (v) => (v == null ? "—" : Number(v).toLocaleString("ar-SA-u-nu-latn", { maximumFractionDigits: 2 }));
const EXTRA = "https://www.extra.com";
let summary = null, products = null, productsLoading = null;
const shards = {};

function ago(ts) {
  if (!ts) return "—";
  const m = Math.round((Date.now() - Date.parse(ts)) / 60000);
  if (m < 60) return `قبل ${m} دقيقة`;
  if (m < 1440) return `قبل ${Math.round(m / 60)} ساعة`;
  return `قبل ${Math.round(m / 1440)} يوم`;
}
const day = (ts) => (ts ? new Date(ts).toLocaleDateString("ar-SA-u-nu-latn-ca-gregory", { day: "numeric", month: "short", year: "numeric" }) : "—");

async function getJSON(path) {
  const r = await fetch(path, { cache: "no-cache" });
  if (!r.ok) throw new Error(path + " " + r.status);
  return r.json();
}

// ---------- التبويبات ----------
document.querySelectorAll(".tabs button").forEach((b) =>
  b.addEventListener("click", () => {
    document.querySelectorAll(".tabs button").forEach((x) => x.setAttribute("aria-selected", x === b));
    document.querySelectorAll(".tab").forEach((t) => (t.hidden = t.id !== "tab-" + b.dataset.tab));
    if (b.dataset.tab === "search") { loadProducts(); $("#q").focus(); }
    try { localStorage.setItem("sayd-tab", b.dataset.tab); } catch (e) {}
  })
);

// ---------- آخر النزولات ----------
function renderDrops() {
  const onlyStock = $("#drops-stock").checked, onlyLow = $("#drops-lowest").checked, min = +$("#drops-min").value;
  const list = summary.drops.filter((d) => (!onlyStock || d.stock) && (!onlyLow || d.lowest) && d.pct >= min);
  $("#drops").innerHTML = list.length
    ? list.map((d) => `
      <li class="item click" data-sku="${esc(d.sku)}">
        <div class="name">${esc(d.name)}</div>
        <div class="row">
          <span class="old">${fmt(d.old)}</span><span class="price">${fmt(d.new)} ريال</span>
          <span class="tag drop">−${d.pct}%</span>
          ${d.lowest ? '<span class="tag star">أقل سعر تاريخي</span>' : ""}
          ${d.stock ? "" : '<span class="tag out">غير متوفر</span>'}
          <span class="muted">${esc(d.cat)} · ${ago(d.ts)}</span>
        </div>
      </li>`).join("")
    : `<li class="muted">${summary.baseline_done ? "لا توجد نزولات مطابقة خلال آخر 14 يوم." : "المسح الأول جارٍ لتأسيس الأسعار؛ النزولات تظهر بعد اكتماله."}</li>`;
}

// ---------- العروض ----------
function renderOffers() {
  const type = $("#offers-type").value, onlyCode = $("#offers-code").checked;
  const list = summary.offers.filter((o) => (!type || o.provider_type === type) && (!onlyCode || o.code));
  $("#offers").innerHTML = list.length
    ? list.map((o) => {
        const terms = [
          o.pct ? `خصم ${o.pct}%` : "", o.amount ? `خصم ${fmt(o.amount)} ريال` : "",
          o.max_discount ? `بحد أقصى ${fmt(o.max_discount)}` : "", o.min_purchase ? `حد أدنى ${fmt(o.min_purchase)}` : "",
          o.expires_at ? `ينتهي ${day(o.expires_at)}` : "",
        ].filter(Boolean).join(" · ");
        return `<li class="item">
          <div class="row" style="margin-top:0">
            <span class="tag">${esc(o.provider_type)}${o.provider ? " – " + esc(o.provider) : ""}</span>
            ${o.code ? `<code>${esc(o.code)}</code>` : ""}
            ${o.products ? `<span class="muted">${o.products} منتج</span>` : ""}
            <span class="muted">ظهر ${ago(o.first_seen)}</span>
          </div>
          ${terms ? `<div class="row">${esc(terms)}</div>` : ""}
          <div class="offer-text">${esc(o.text)}</div>
          ${o.link ? `<div class="row"><a href="${esc(o.link)}" target="_blank" rel="noopener">الرابط ↗</a></div>` : ""}
        </li>`;
      }).join("")
    : '<li class="muted">لا توجد عروض نشطة.</li>';
}

// ---------- البحث ----------
const norm = (s) => String(s || "").toLowerCase()
  .replace(/[ً-ْـ]/g, "").replace(/[أإآ]/g, "ا").replace(/ى/g, "ي").replace(/ة/g, "ه")
  .replace(/[٠-٩]/g, (d) => "٠١٢٣٤٥٦٧٨٩".indexOf(d));

function loadProducts() {
  if (!productsLoading) {
    $("#search-note").textContent = "جارٍ تحميل الكتالوج…";
    productsLoading = getJSON("data/products.json").then((d) => {
      products = d.items.map((r) => ({ sku: r[0], name: r[1], cat: d.cats[r[2]], price: r[3], was: r[4], stock: r[5], min: r[6], max: r[7], avg: r[8], key: norm(r[1] + " " + r[0]) }));
      $("#search-note").textContent = `${products.length.toLocaleString("en")} منتج`;
      search();
    });
  }
  return productsLoading;
}

function search() {
  if (!products) return;
  const q = norm($("#q").value.trim()), terms = q.split(/\s+/).filter(Boolean), onlyStock = $("#search-stock").checked;
  if (!terms.length) { $("#results").innerHTML = ""; return; }
  let res = products.filter((p) => terms.every((t) => p.key.includes(t)) && (!onlyStock || p.stock));
  const sort = $("#sort").value;
  const disc = (p) => (p.was && p.price ? (p.was - p.price) / p.was : 0);
  if (sort === "disc") res.sort((a, b) => disc(b) - disc(a));
  else if (sort === "price") res.sort((a, b) => (a.price ?? 1e12) - (b.price ?? 1e12));
  else if (sort === "low") res.sort((a, b) => (a.price / (a.min || a.price) || 9) - (b.price / (b.min || b.price) || 9));
  $("#search-note").textContent = `${res.length} نتيجة` + (res.length > 60 ? " (أول 60)" : "");
  $("#results").innerHTML = res.slice(0, 60).map(productItem).join("");
}

function productItem(p) {
  const atLow = p.price != null && p.min != null && p.price <= p.min;
  return `<li class="item click" data-sku="${esc(p.sku)}">
    <div class="name">${esc(p.name)}</div>
    <div class="row">
      ${p.was && p.was > p.price ? `<span class="old">${fmt(p.was)}</span>` : ""}
      <span class="price">${fmt(p.price)} ريال</span>
      ${atLow ? '<span class="tag star">عند أقل سعر</span>' : ""}
      ${p.stock ? "" : '<span class="tag out">غير متوفر</span>'}
      <span class="muted">${esc((p.cat || "").split(" > ").pop())} · ${esc(p.sku)}</span>
    </div>
  </li>`;
}

// ---------- المراقبة والتشغيل ----------
function renderWatch() {
  $("#watch").innerHTML = summary.watch.length
    ? summary.watch.map((w) => `<li class="item ${w.sku ? "click" : ""}" ${w.sku ? `data-sku="${esc(w.sku)}"` : ""}>
        <div class="name">${esc(w.name || (w.sku ? "SKU " + w.sku : "قسم: " + w.category))}</div>
        <div class="row">
          ${w.price != null ? `<span class="price">${fmt(w.price)} ريال</span>` : ""}
          ${w.target_price != null ? `<span class="tag">المستهدف ${fmt(w.target_price)}</span>` : ""}
          ${w.drop_pct != null ? `<span class="tag">نسبة خاصة ${w.drop_pct}%</span>` : ""}
          ${w.sku && w.stock === false ? '<span class="tag out">غير متوفر</span>' : ""}
        </div></li>`).join("")
    : '<li class="muted">قائمة المراقبة فارغة.</li>';
}

function renderRuns() {
  const s = summary;
  const prog = s.full_scan ? `مسح كامل جارٍ: ${fmt(s.full_scan.start)} من ${fmt(s.full_scan.total)}` : `آخر مسح كامل: ${ago(s.last_full_done_at)}`;
  $("#runs").innerHTML = `
    <div class="stats">
      <div class="stat"><b>${fmt(s.products)}</b><span>منتج مسجل</span></div>
      <div class="stat"><b>${fmt(s.in_stock)}</b><span>متوفر للتوصيل</span></div>
      <div class="stat"><b>${s.offers.length}</b><span>عرض نشط</span></div>
      <div class="stat"><b>${s.settings.min_drop_pct}%</b><span>حد التنبيه العام</span></div>
    </div>
    <p class="muted">${esc(prog)} · ${s.baseline_done ? "التنبيهات مفعلة" : "المسح الأول جارٍ (بدون تنبيهات)"}</p>
    <div class="scroll"><table class="runs"><tr><th>البداية</th><th>المراحل</th><th>منتجات</th><th>تغيّرات</th><th>تنبيهات</th><th>أخطاء</th><th>الحالة</th></tr>
    ${s.runs.map((r) => `<tr><td>${ago(r.started_at)}</td><td>${esc(JSON.parse(r.phases || "[]").join(" "))}</td><td>${fmt(r.products_seen)}</td><td>${fmt(r.changes)}</td><td>${fmt(r.alerts)}</td><td>${fmt(r.errors)}</td><td>${esc(r.status)}</td></tr>`).join("")}
    </table></div>`;
}

// ---------- تفاصيل المنتج والرسم ----------
async function openProduct(sku) {
  await loadProducts();
  const p = products.find((x) => x.sku === sku);
  if (!p) return;
  const key = sku.slice(-2);
  shards[key] = shards[key] || getJSON(`data/h/${key}.json`).catch(() => ({}));
  const hist = (await shards[key])[sku] || [];
  $("#d-name").textContent = p.name;
  $("#d-cat").textContent = `${p.cat} · ${p.sku}`;
  $("#d-link").href = `${EXTRA}/ar-sa/p/${encodeURIComponent(p.sku)}`;
  $("#d-stats").innerHTML = [
    ["السعر الحالي", p.price], ["قبل الخصم", p.was], ["أقل سعر", p.min], ["أعلى سعر", p.max], ["متوسط 30 يوم", p.avg],
  ].map(([l, v]) => `<div class="stat"><b>${fmt(v)}</b><span>${l}</span></div>`).join("");
  $("#d-table").innerHTML = "<tr><th>التاريخ</th><th>السعر</th><th>التوفر</th></tr>" +
    hist.slice().reverse().map((h) => `<tr><td>${day(h[0] + ":00Z")}</td><td>${fmt(h[1])}</td><td>${h[2] ? "متوفر" : "غير متوفر"}</td></tr>`).join("");
  $("#detail").showModal();
  drawChart($("#chart"), hist.filter((h) => h[1] != null).map((h) => ({ t: Date.parse(h[0] + ":00Z"), v: h[1] })));
}

function drawChart(el, pts) {
  el.innerHTML = "";
  if (!pts.length) { el.innerHTML = '<p class="muted">لا يوجد سجل بعد.</p>'; return; }
  const now = Date.now(), W = el.clientWidth || 600, H = el.clientHeight || 240;
  const m = { l: 52, r: 12, t: 12, b: 26 };
  const t0 = pts[0].t, t1 = Math.max(now, t0 + 86400000);
  let lo = Math.min(...pts.map((p) => p.v)), hi = Math.max(...pts.map((p) => p.v));
  const pad = (hi - lo) * 0.1 || hi * 0.05 || 1; lo -= pad; hi += pad;
  const x = (t) => m.l + ((t - t0) / (t1 - t0)) * (W - m.l - m.r);
  const y = (v) => m.t + (1 - (v - lo) / (hi - lo)) * (H - m.t - m.b);
  // خط سلّمي: السعر ثابت حتى التغيير التالي
  let d = `M${x(pts[0].t)},${y(pts[0].v)}`;
  pts.forEach((p, i) => { if (i) d += `H${x(p.t)}V${y(p.v)}`; });
  d += `H${x(t1)}`;
  const ticks = [0, 1, 2, 3].map((i) => lo + pad + ((hi - lo - 2 * pad) * i) / 3);
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" direction="ltr" role="img" aria-label="تاريخ السعر">
    ${ticks.map((v) => `<line x1="${m.l}" x2="${W - m.r}" y1="${y(v)}" y2="${y(v)}" stroke="var(--grid)"/>
      <text x="${m.l - 6}" y="${y(v) + 4}" text-anchor="end" font-size="11" fill="var(--muted)">${fmt(Math.round(v))}</text>`).join("")}
    <text x="${m.l}" y="${H - 6}" font-size="11" fill="var(--muted)">${day(t0)}</text>
    <text x="${W - m.r}" y="${H - 6}" text-anchor="end" font-size="11" fill="var(--muted)">${day(t1)}</text>
    <path d="${d}" fill="none" stroke="var(--series)" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
    <line class="xh" y1="${m.t}" y2="${H - m.b}" stroke="var(--muted)" stroke-dasharray="3 3" visibility="hidden"/>
    <circle class="dot" r="4.5" fill="var(--series)" stroke="var(--surface)" stroke-width="2" visibility="hidden"/>
    <rect x="${m.l}" y="0" width="${W - m.l - m.r}" height="${H}" fill="transparent"/>
  </svg><div class="tip" hidden></div>`;
  const svg = el.querySelector("svg"), xh = svg.querySelector(".xh"), dot = svg.querySelector(".dot"), tip = el.querySelector(".tip");
  const move = (ev) => {
    const r = svg.getBoundingClientRect(), px = ((ev.clientX - r.left) / r.width) * W;
    const t = t0 + ((px - m.l) / (W - m.l - m.r)) * (t1 - t0);
    if (t < t0 || t > t1) return hide();
    let p = pts[0];
    for (const q of pts) if (q.t <= t) p = q;
    const cx = x(t), cy = y(p.v);
    xh.setAttribute("x1", cx); xh.setAttribute("x2", cx); xh.setAttribute("visibility", "visible");
    dot.setAttribute("cx", cx); dot.setAttribute("cy", cy); dot.setAttribute("visibility", "visible");
    tip.hidden = false; tip.style.left = (cx / W) * 100 + "%"; tip.style.top = (cy / H) * r.height + "px";
    tip.textContent = `${day(t)}: ${fmt(p.v)} ريال`;
  };
  const hide = () => { xh.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden"); tip.hidden = true; };
  svg.addEventListener("pointermove", move);
  svg.addEventListener("pointerdown", move);
  svg.addEventListener("pointerleave", hide);
}

document.addEventListener("click", (e) => {
  const li = e.target.closest("[data-sku]");
  if (li && !e.target.closest("a")) openProduct(li.dataset.sku);
});

const clock = (ts) => new Date(ts).toLocaleString("ar-SA-u-nu-latn-ca-gregory", { timeZone: "Asia/Riyadh", weekday: "short", hour: "numeric", minute: "2-digit" });

function nextRun(ts) {
  // GitHub يشغّل الأداة عند الدقيقة 17 من كل ساعة
  const d = new Date(Math.max(Date.parse(ts) - 10 * 60000, Date.now()));
  if (d.getUTCMinutes() > 17) d.setUTCHours(d.getUTCHours() + 1);
  d.setUTCMinutes(17, 0, 0);
  return d;
}

function renderStatus() {
  const s = summary;
  const next = s.full_scan ? "المسح الأول جارٍ" : s.next_update ? `القادم ≈ ${clock(nextRun(s.next_update))}` : "";
  $("#status").innerHTML = `آخر تحديث ${clock(s.generated_at)} (${ago(s.generated_at)})<br>${esc(next)} · ${fmt(s.products)} منتج`;
}

// ---------- البداية ----------
(async function init() {
  try {
    summary = await getJSON("data/summary.json");
  } catch (e) {
    $("#status").textContent = "لا توجد بيانات بعد";
    return;
  }
  renderStatus();
  // إذا بقيت الصفحة مفتوحة: تحقق كل 10 دقائق وحدّث عند صدور بيانات جديدة
  setInterval(async () => {
    try {
      const s = await getJSON("data/summary.json");
      if (s.generated_at !== summary.generated_at) location.reload();
      else renderStatus();
    } catch (e) {}
  }, 10 * 60 * 1000);
  const types = [...new Set(summary.offers.map((o) => o.provider_type))];
  $("#offers-type").innerHTML += types.map((t) => `<option>${esc(t)}</option>`).join("");
  ["#drops-stock", "#drops-lowest", "#drops-min"].forEach((s) => $(s).addEventListener("change", renderDrops));
  ["#offers-type", "#offers-code"].forEach((s) => $(s).addEventListener("change", renderOffers));
  let timer;
  $("#q").addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(search, 150); });
  ["#sort", "#search-stock"].forEach((s) => $(s).addEventListener("change", search));
  renderDrops(); renderOffers(); renderWatch(); renderRuns();
  try {
    const t = localStorage.getItem("sayd-tab");
    if (t) document.querySelector(`.tabs button[data-tab="${t}"]`)?.click();
  } catch (e) {}
})();
