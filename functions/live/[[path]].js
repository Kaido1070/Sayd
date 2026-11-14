// بيانات الموقع الحية (فرع live، تتحدث كل نصف ساعة) من نفس نطاق الموقع:
// متصفحات مثل سفاري قد تمنع الطلب لنطاق خارجي، وهنا تصل البيانات مضغوطة أيضًا.
const SOURCE = "https://raw.githubusercontent.com/Kaido1070/Sayd/live/";

export async function onRequestGet({ params }) {
  const path = [].concat(params.path || []).join("/");
  if (!/^(p\/)?[\w-]+\.json$/.test(path)) return new Response("Not found", { status: 404 });
  const r = await fetch(SOURCE + path, { cf: { cacheTtl: 60, cacheEverything: true } });
  if (!r.ok) return new Response("Not found", { status: r.status === 404 ? 404 : 502 });
  return new Response(r.body, {
    headers: { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff" },
  });
}
