"""SQLite: المنتجات، سجل الأسعار، العروض، التنبيهات.
سجل الأسعار دائم ولا يُحذف أبداً:
- price_history: سطر عند كل تغيّر (السعر السابق والجديد ووقت الرصد).
- price_periods: فترات متصلة بنفس السعر/التوفر، مع أول وآخر رصد ناجح وعدد مرات الرصد؛
  الرصد المطابق يمدّ الفترة بدل تكرار السطر، والانقطاع الطويل يبدأ فترة جديدة فيظهر كفجوة.
كل الأوقات بتوقيت UTC بصيغة ISO."""
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS products(
  sku TEXT PRIMARY KEY, name TEXT, category TEXT, category_codes TEXT, url TEXT,
  price REAL, was_price REAL, in_stock INTEGER, marketplace INTEGER, badges TEXT,
  min_price REAL, min_price_at TEXT, max_price REAL, max_price_at TEXT,
  first_seen TEXT, last_seen TEXT, last_changed TEXT, pdp_checked_at TEXT,
  delisted INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS price_history(
  id INTEGER PRIMARY KEY, sku TEXT NOT NULL, ts TEXT NOT NULL,
  price REAL, was_price REAL, in_stock INTEGER);
CREATE INDEX IF NOT EXISTS ix_hist_sku_ts ON price_history(sku, ts);
CREATE TABLE IF NOT EXISTS offers(
  key TEXT PRIMARY KEY, source TEXT, provider_type TEXT, provider TEXT, code TEXT,
  pct REAL, amount REAL, min_purchase REAL, max_discount REAL, expires_at TEXT,
  text TEXT, link TEXT, content_hash TEXT, first_seen TEXT, last_seen TEXT, active INTEGER);
CREATE TABLE IF NOT EXISTS offer_products(
  offer_key TEXT, sku TEXT, last_seen TEXT, PRIMARY KEY(offer_key, sku));
CREATE TABLE IF NOT EXISTS alerts(
  id INTEGER PRIMARY KEY, kind TEXT, key TEXT, value TEXT, payload TEXT,
  created_at TEXT, sent_at TEXT, channel TEXT, UNIQUE(kind, key, value));
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY, started_at TEXT, finished_at TEXT, phases TEXT,
  products_seen INTEGER, changes INTEGER, alerts INTEGER, errors INTEGER, status TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS errors(id INTEGER PRIMARY KEY, ts TEXT, context TEXT, message TEXT);
CREATE TABLE IF NOT EXISTS sitemap(sku TEXT PRIMARY KEY, url TEXT, last_checked TEXT, dead_until TEXT);
CREATE TABLE IF NOT EXISTS price_periods(
  id INTEGER PRIMARY KEY, sku TEXT NOT NULL, price REAL NOT NULL, was_price REAL, in_stock INTEGER,
  first_ts TEXT NOT NULL, last_ts TEXT NOT NULL, checks INTEGER NOT NULL DEFAULT 1, source TEXT);
CREATE INDEX IF NOT EXISTS ix_periods_sku ON price_periods(sku, first_ts);
CREATE TABLE IF NOT EXISTS deal_state(
  sku TEXT PRIMARY KEY, status TEXT NOT NULL, basis REAL, price REAL,
  started_at TEXT, updated_at TEXT, ended_at TEXT, reason TEXT);
"""


def _norm_was(price, was):
    """السعر المشطوب المعلن يُحفظ فقط إذا كان أعلى من سعر البيع الفعلي."""
    return was if was and price and was > price + 0.005 else None


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def ts_minus(ts, **delta):
    return (parse_ts(ts) - timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Change:
    sku: str
    name: str
    url: str
    category: str
    category_codes: list
    old_price: float | None
    new_price: float | None
    old_stock: bool | None
    new_stock: bool
    prev_min: float | None      # أقل سعر تاريخي قبل هذا التغيير
    window_min: float | None    # أقل سعر خلال نافذة الأيام قبل هذا التغيير
    history_id: int | None
    is_new: bool = False
    was_price: float | None = None   # السعر قبل الخصم كما يعرضه الموقع


class DB:
    def __init__(self, path):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=DELETE")
        self.conn.executescript(SCHEMA)
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(products)")}
        for col, typ in (("pdp_price", "REAL"), ("pdp_at", "TEXT"), ("image", "TEXT"), ("model", "TEXT"),
                         ("brand", "TEXT"), ("specs", "TEXT"), ("dept", "TEXT"), ("variant_group", "TEXT"),
                         ("variant_label", "TEXT"), ("seller", "TEXT")):
            if col not in cols:  # ترقية قاعدة بيانات قديمة
                self.conn.execute(f"ALTER TABLE products ADD COLUMN {col} {typ}")
        if "prev_price" not in {r[1] for r in self.conn.execute("PRAGMA table_info(price_history)")}:
            self.conn.execute("ALTER TABLE price_history ADD COLUMN prev_price REAL")
            # السعر السابق للأسطر القديمة من السطر الذي قبلها لنفس المنتج (التواريخ لا تتغير)
            self.conn.execute("""UPDATE price_history SET prev_price=(
                SELECT h2.price FROM price_history h2 WHERE h2.sku=price_history.sku
                AND (h2.ts<price_history.ts OR (h2.ts=price_history.ts AND h2.id<price_history.id))
                ORDER BY h2.ts DESC, h2.id DESC LIMIT 1)""")
        self.gap_hours = 12
        self._import_periods()
        self.conn.commit()

    def _import_periods(self):
        """مرة واحدة: كل سطر قديم في price_history يصبح فترة بنفس وقت رصده. الفترات المغلقة القديمة
        لا نعرف آخر رصد لها فتبقى بطول صفر (source=import)؛ الفترة الأخيرة تمتد حتى آخر رصد للمنتج."""
        if self.get("periods_imported") or self.conn.execute("SELECT 1 FROM price_periods LIMIT 1").fetchone():
            return
        last = {}
        for r in self.conn.execute("SELECT sku, ts, price, was_price, in_stock FROM price_history "
                                   "WHERE price IS NOT NULL AND price>0 ORDER BY sku, ts, id"):
            cur = self.conn.execute(
                "INSERT INTO price_periods(sku,price,was_price,in_stock,first_ts,last_ts,checks,source) VALUES(?,?,?,?,?,?,1,'import')",
                (r["sku"], r["price"], _norm_was(r["price"], r["was_price"]), r["in_stock"], r["ts"], r["ts"]))
            last[r["sku"]] = cur.lastrowid
        # الفترة الأخيرة لكل منتج: أي تغيّر بعدها كان سيُسجَّل، فهي مستمرة حتى آخر رصد ناجح للمنتج
        seen = {r[0]: r[1] for r in self.conn.execute("SELECT sku, last_seen FROM products WHERE delisted=0")}
        for sku, pid in last.items():
            ls = seen.get(sku)
            if ls:
                self.conn.execute("UPDATE price_periods SET last_ts=?, checks=2 WHERE id=? AND first_ts<?", (ls, pid, ls))
        self.set("periods_imported", now())

    # ---------- meta ----------
    def get(self, key, default=None):
        r = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(r[0]) if r else default

    def set(self, key, value):
        self.conn.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                          (key, json.dumps(value, ensure_ascii=False)))

    def commit(self):
        self.conn.commit()

    # ---------- المنتجات ----------
    def window_min(self, sku, since):
        """أقل سعر ساري خلال الفترة منذ since (يشمل السعر الذي كان سارياً عند بدايتها)."""
        rows = self.conn.execute(
            "SELECT price FROM price_history WHERE sku=? AND ts>=? AND price IS NOT NULL "
            "UNION ALL SELECT * FROM (SELECT price FROM price_history WHERE sku=? AND ts<? AND price IS NOT NULL ORDER BY ts DESC LIMIT 1)",
            (sku, since, sku, since)).fetchall()
        vals = [r[0] for r in rows]
        return min(vals) if vals else None

    def observe(self, p, ts, window_days, source="api", pdp_trust_hours=12):
        """يسجل مشاهدة منتج ويرجع Change إذا تغيّر السعر أو التوفر (أو منتج جديد).
        source="pdp": السعر من صفحة المنتج (ما يراه المشتري). محرك البحث أحياناً يتأخر في تحديث السعر،
        فخلال pdp_trust_hours بعد قراءة الصفحة يُتجاهل سعر البحث إذا خالفها حتى لا يتذبذب السعر."""
        c = self.conn
        row = c.execute("SELECT * FROM products WHERE sku=?", (p.sku,)).fetchone()
        codes = json.dumps(p.category_codes, ensure_ascii=False)
        badges = json.dumps(p.badges, ensure_ascii=False)
        if row is None:
            c.execute(
                "INSERT INTO products(sku,name,category,category_codes,url,price,was_price,in_stock,marketplace,badges,"
                "min_price,min_price_at,max_price,max_price_at,first_seen,last_seen,last_changed) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (p.sku, p.name, p.category, codes, p.url, p.price, p.was_price, int(p.in_stock), int(p.marketplace),
                 badges, p.price, ts if p.price else None, p.price, ts if p.price else None, ts, ts, ts))
            self._details(p)
            hid = self._history(p.sku, ts, p.price, p.was_price, p.in_stock, None)
            self._period(p.sku, ts, p.price, p.was_price, p.in_stock, source)
            return Change(p.sku, p.name, p.url, p.category, p.category_codes, None, p.price, None, p.in_stock,
                          None, None, hid, is_new=True, was_price=p.was_price)

        new_price = p.price if p.price is not None else row["price"]
        stale_api = (source == "api" and row["pdp_at"] and row["pdp_price"] is not None and new_price is not None
                     and row["pdp_at"] >= ts_minus(ts, hours=pdp_trust_hours) and abs(new_price - row["pdp_price"]) >= 0.01)
        if stale_api:
            new_price = row["price"]  # سعر البحث متأخر عن الصفحة
        price_changed = new_price is not None and (row["price"] is None or abs(new_price - row["price"]) >= 0.01)
        stock_changed = bool(row["in_stock"]) != p.in_stock or row["delisted"]
        c.execute("UPDATE products SET name=?,category=?,category_codes=?,url=?,was_price=?,marketplace=?,badges=?,"
                  "last_seen=?,delisted=0 WHERE sku=?",
                  (p.name, p.category, codes, p.url or row["url"], p.was_price, int(p.marketplace), badges, ts, p.sku))
        self._details(p)
        if source == "pdp" and p.price is not None:
            c.execute("UPDATE products SET pdp_price=?, pdp_at=? WHERE sku=?", (p.price, ts, p.sku))
        if p.price is not None and not stale_api:  # رصد ناجح بسعر صالح فقط يمدّ الفترة أو يبدأ أخرى
            self._period(p.sku, ts, p.price, p.was_price, p.in_stock, source)
        if not (price_changed or stock_changed):
            return None

        wmin = self.window_min(p.sku, ts_minus(ts, days=window_days))
        prev_min = row["min_price"]
        upd = {"price": new_price, "in_stock": int(p.in_stock), "last_changed": ts}
        if new_price is not None and (prev_min is None or new_price < prev_min):
            upd.update(min_price=new_price, min_price_at=ts)
        if new_price is not None and (row["max_price"] is None or new_price > row["max_price"]):
            upd.update(max_price=new_price, max_price_at=ts)
        c.execute(f"UPDATE products SET {','.join(k + '=?' for k in upd)} WHERE sku=?", (*upd.values(), p.sku))
        hid = self._history(p.sku, ts, new_price, p.was_price, p.in_stock, row["price"])
        return Change(p.sku, p.name, p.url or row["url"], p.category, p.category_codes, row["price"], new_price,
                      False if row["delisted"] else bool(row["in_stock"]), p.in_stock, prev_min, wmin, hid,
                      was_price=p.was_price)

    def _details(self, p):
        """الصورة والموديل والمواصفات: القيمة الفارغة لا تمسح قيمة محفوظة (صفحة المنتج قد تكون أكمل)."""
        vals = {"image": p.image, "model": p.model, "brand": p.brand, "dept": p.dept,
                "specs": json.dumps(p.specs, ensure_ascii=False) if p.specs else ""}
        vals = {k: v for k, v in vals.items() if v}
        if vals:
            self.conn.execute(f"UPDATE products SET {','.join(k + '=?' for k in vals)} WHERE sku=?", (*vals.values(), p.sku))

    def set_page_info(self, sku, variants, seller):
        """خيارات المنتج كما يعرّفها المتجر: كل الخيارات تأخذ نفس رقم المجموعة (أصغر رمز فيها)."""
        if seller:
            self.conn.execute("UPDATE products SET seller=? WHERE sku=?", (seller, sku))
        labels = dict(variants)
        if len(labels) < 2 or sku not in labels:
            return
        group = min(labels)
        for code, label in labels.items():
            self.conn.execute("UPDATE products SET variant_group=?, variant_label=? WHERE sku=?", (group, label, code))

    def _history(self, sku, ts, price, was, stock, prev):
        cur = self.conn.execute("INSERT INTO price_history(sku,ts,price,was_price,in_stock,prev_price) VALUES(?,?,?,?,?,?)",
                                (sku, ts, price, was, int(stock), prev))
        return cur.lastrowid

    def _period(self, sku, ts, price, was, stock, source):
        """رصد ناجح: يمدّ الفترة الحالية إذا لم يتغير شيء ولم ينقطع الرصد أكثر من gap_hours، وإلا يبدأ فترة جديدة."""
        if price is None or price <= 0:
            return
        was = _norm_was(price, was)
        cur = self.conn.execute("SELECT id, price, was_price, in_stock, last_ts FROM price_periods WHERE sku=? "
                                "ORDER BY first_ts DESC, id DESC LIMIT 1", (sku,)).fetchone()
        if cur and ts < cur["last_ts"]:
            return  # رصد أقدم من آخر رصد محفوظ
        same = cur and abs(cur["price"] - price) < 0.005 and (cur["was_price"] or 0) == (was or 0) \
            and bool(cur["in_stock"]) == bool(stock)
        if same and (parse_ts(ts) - parse_ts(cur["last_ts"])).total_seconds() <= self.gap_hours * 3600:
            self.conn.execute("UPDATE price_periods SET last_ts=?, checks=checks+1 WHERE id=?", (ts, cur["id"]))
        else:
            self.conn.execute("INSERT INTO price_periods(sku,price,was_price,in_stock,first_ts,last_ts,checks,source) "
                              "VALUES(?,?,?,?,?,?,1,?)", (sku, price, was, int(bool(stock)), ts, ts, source))

    def periods(self, sku):
        return self.conn.execute("SELECT price, was_price, in_stock, first_ts, last_ts, checks, source FROM price_periods "
                                 "WHERE sku=? ORDER BY first_ts, id", (sku,)).fetchall()

    # ---------- حالة العروض ----------
    def deal(self, sku):
        return self.conn.execute("SELECT * FROM deal_state WHERE sku=?", (sku,)).fetchone()

    def set_deal(self, sku, **f):
        if self.deal(sku):
            self.conn.execute(f"UPDATE deal_state SET {','.join(k + '=?' for k in f)} WHERE sku=?", (*f.values(), sku))
        else:
            self.conn.execute(f"INSERT INTO deal_state(sku,{','.join(f)}) VALUES(?,{','.join('?' * len(f))})", (sku, *f.values()))

    def drop_deal(self, sku):
        self.conn.execute("DELETE FROM deal_state WHERE sku=?", (sku,))

    def delist_unseen(self, since, ts):
        """بعد اكتمال مسح كامل: المنتجات التي لم تظهر (ولا صفحة حية لها في خريطة الموقع) تُعتبر غير متوفرة."""
        rows = self.conn.execute(
            "SELECT sku, price, was_price FROM products WHERE last_seen<? AND delisted=0 "
            "AND sku NOT IN (SELECT sku FROM sitemap WHERE dead_until IS NULL OR dead_until<?)", (since, ts)).fetchall()
        for r in rows:
            self.conn.execute("UPDATE products SET delisted=1, in_stock=0, last_changed=? WHERE sku=?", (ts, r["sku"]))
            self._history(r["sku"], ts, r["price"], r["was_price"], False, r["price"])  # استنتاج، ليس رصداً: لا فترة
        return len(rows)

    # ---------- خريطة الموقع ----------
    def replace_sitemap(self, mapping):
        self.conn.execute("CREATE TEMP TABLE IF NOT EXISTS sm_new(sku TEXT PRIMARY KEY, url TEXT)")
        self.conn.execute("DELETE FROM sm_new")
        self.conn.executemany("INSERT OR REPLACE INTO sm_new VALUES(?,?)", mapping.items())
        self.conn.execute("DELETE FROM sitemap WHERE sku NOT IN (SELECT sku FROM sm_new)")
        self.conn.execute("INSERT INTO sitemap(sku,url) SELECT sku,url FROM sm_new WHERE true "
                          "ON CONFLICT(sku) DO UPDATE SET url=excluded.url")

    def sitemap_unseen(self, since, ts, mp_batch):
        """منتجات في خريطة الموقع لم يرجعها محرك البحث في هذا المسح: العادية كلها + دفعة دوّارة من منتجات البائعين."""
        q = ("SELECT s.sku, s.url FROM sitemap s LEFT JOIN products p ON p.sku=s.sku "
             "WHERE (p.last_seen IS NULL OR p.last_seen<?) AND (s.dead_until IS NULL OR s.dead_until<?) AND s.sku {} LIKE 'MP%' "
             "ORDER BY s.last_checked IS NOT NULL, s.last_checked")
        regular = self.conn.execute(q.format("NOT"), (since, ts)).fetchall()
        mp = self.conn.execute(q.format("") + " LIMIT ?", (since, ts, mp_batch)).fetchall()
        return [(r[0], r[1]) for r in regular + mp]

    def mark_sitemap(self, sku, ts, dead_days=None):
        dead = ts_minus(ts, days=-dead_days) if dead_days else None
        self.conn.execute("UPDATE sitemap SET last_checked=?, dead_until=? WHERE sku=?", (ts, dead, sku))

    def mark_pdp(self, sku, ts):
        self.conn.execute("UPDATE products SET pdp_checked_at=? WHERE sku=?", (ts, sku))

    def products_for_pdp(self, limit, recheck_before, priority_skus=()):
        """صفحات المنتجات المطلوب فتحها: المراقَبة أولاً ثم ما عليه شارات عروض، موزعة على الأقسام."""
        out = [s for s in priority_skus if self.conn.execute("SELECT 1 FROM products WHERE sku=?", (s,)).fetchone()]
        rows = self.conn.execute(
            "SELECT sku, category FROM products WHERE delisted=0 AND in_stock=1 AND url!='' "
            "AND (pdp_checked_at IS NULL OR pdp_checked_at<?) "
            "ORDER BY (badges!='[]') DESC, (was_price>price) DESC, pdp_checked_at IS NOT NULL, pdp_checked_at, RANDOM() LIMIT ?",
            (recheck_before, limit * 20)).fetchall()
        per_cat = {}
        for r in rows:
            top = (r["category"] or "").split(" > ")[0]
            if per_cat.get(top, 0) >= max(2, limit // 8) or r["sku"] in out:
                continue
            per_cat[top] = per_cat.get(top, 0) + 1
            out.append(r["sku"])
            if len(out) >= limit:
                break
        return out[:limit]

    def products_for_price_check(self, limit, exclude=()):
        """تدوير على منتجات اكسترا المتوفرة: محرك البحث أحياناً يتأخر في تحديث السعر عن صفحة المنتج،
        فنفتح صفحاتها بالتناوب — ما لم يُفحص أبداً أولاً (الأغلى قبل الأرخص)، ثم الأقدم فحصاً."""
        rows = self.conn.execute(
            "SELECT sku FROM products WHERE delisted=0 AND in_stock=1 AND marketplace=0 AND url!='' AND price>0 "
            "ORDER BY pdp_checked_at IS NOT NULL, pdp_checked_at, price DESC LIMIT ?", (limit + len(exclude),)).fetchall()
        ex = set(exclude)
        return [r[0] for r in rows if r[0] not in ex][:limit]

    def product(self, sku):
        return self.conn.execute("SELECT * FROM products WHERE sku=?", (sku,)).fetchone()

    def watch_skus(self, watch):
        skus = {str(w["sku"]) for w in watch if w.get("sku")}
        for w in watch:
            cat = str(w.get("category") or "").strip()
            if cat:
                for r in self.conn.execute("SELECT sku FROM products WHERE category_codes LIKE ? OR category LIKE ?",
                                           (f'%"{cat}"%', f"%{cat}%")):
                    skus.add(r[0])
        return skus

    # ---------- العروض ----------
    def upsert_offer(self, o, ts):
        """يرجع 'new' أو 'changed' أو None."""
        row = self.conn.execute("SELECT content_hash, active FROM offers WHERE key=?", (o["key"],)).fetchone()
        cols = ["source", "provider_type", "provider", "code", "pct", "amount", "min_purchase", "max_discount",
                "expires_at", "text", "link", "content_hash"]
        if row is None:
            self.conn.execute(f"INSERT INTO offers(key,{','.join(cols)},first_seen,last_seen,active) "
                              f"VALUES(?,{','.join('?' * len(cols))},?,?,1)",
                              (o["key"], *[o.get(k) for k in cols], ts, ts))
            return "new"
        self.conn.execute(f"UPDATE offers SET {','.join(k + '=?' for k in cols)},last_seen=?,active=1 WHERE key=?",
                          (*[o.get(k) for k in cols], ts, o["key"]))
        if row["content_hash"] != o["content_hash"]:
            return "changed"
        return "new" if not row["active"] else None

    def link_offer(self, key, skus, ts):
        self.conn.executemany("INSERT INTO offer_products(offer_key,sku,last_seen) VALUES(?,?,?) "
                              "ON CONFLICT(offer_key, sku) DO UPDATE SET last_seen=excluded.last_seen", [(key, s, ts) for s in skus])

    def expire_offers(self, ts, unseen_before):
        """عروض انتهى تاريخها أو لم تُشاهد منذ فترة تصبح غير نشطة."""
        self.conn.execute("UPDATE offers SET active=0 WHERE active=1 AND ((expires_at IS NOT NULL AND expires_at<?) OR last_seen<?)",
                          (ts, unseen_before))

    # ---------- التنبيهات ----------
    def queue_alert(self, kind, key, value, payload, ts):
        cur = self.conn.execute("INSERT OR IGNORE INTO alerts(kind,key,value,payload,created_at) VALUES(?,?,?,?,?)",
                                (kind, key, str(value), json.dumps(payload, ensure_ascii=False), ts))
        return cur.rowcount > 0

    def pending_alerts(self, since):
        return self.conn.execute("SELECT * FROM alerts WHERE sent_at IS NULL AND created_at>=? ORDER BY id", (since,)).fetchall()

    def mark_sent(self, ids, ts, channel):
        self.conn.executemany("UPDATE alerts SET sent_at=?, channel=? WHERE id=?", [(ts, channel, i) for i in ids])

    # ---------- السجلات ----------
    def log_error(self, context, message, ts):
        self.conn.execute("INSERT INTO errors(ts,context,message) VALUES(?,?,?)", (ts, context, str(message)[:500]))
        self.conn.execute("DELETE FROM errors WHERE id <= (SELECT MAX(id) - 2000 FROM errors)")

    def start_run(self, ts):
        return self.conn.execute("INSERT INTO runs(started_at,status) VALUES(?, 'running')", (ts,)).lastrowid

    def finish_run(self, run_id, ts, **kw):
        kw["phases"] = json.dumps(kw.get("phases", []), ensure_ascii=False)
        self.conn.execute(f"UPDATE runs SET finished_at=?,{','.join(k + '=?' for k in kw)} WHERE id=?",
                          (ts, *kw.values(), run_id))

    def integrity_ok(self):
        return self.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    def close(self):
        self.conn.commit()
        self.conn.close()
