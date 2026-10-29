"""SQLite: المنتجات، سجل الأسعار (عند التغيّر فقط)، العروض، التنبيهات."""
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
"""


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


class DB:
    def __init__(self, path):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=DELETE")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

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

    def observe(self, p, ts, window_days):
        """يسجل مشاهدة منتج ويرجع Change إذا تغيّر السعر أو التوفر (أو منتج جديد)."""
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
            hid = self._history(p.sku, ts, p.price, p.was_price, p.in_stock)
            return Change(p.sku, p.name, p.url, p.category, p.category_codes, None, p.price, None, p.in_stock,
                          None, None, hid, is_new=True)

        new_price = p.price if p.price is not None else row["price"]
        price_changed = new_price is not None and (row["price"] is None or abs(new_price - row["price"]) >= 0.01)
        stock_changed = bool(row["in_stock"]) != p.in_stock or row["delisted"]
        c.execute("UPDATE products SET name=?,category=?,category_codes=?,url=?,was_price=?,marketplace=?,badges=?,"
                  "last_seen=?,delisted=0 WHERE sku=?",
                  (p.name, p.category, codes, p.url or row["url"], p.was_price, int(p.marketplace), badges, ts, p.sku))
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
        hid = self._history(p.sku, ts, new_price, p.was_price, p.in_stock)
        return Change(p.sku, p.name, p.url or row["url"], p.category, p.category_codes, row["price"], new_price,
                      None if row["delisted"] else bool(row["in_stock"]), p.in_stock, prev_min, wmin, hid)

    def _history(self, sku, ts, price, was, stock):
        cur = self.conn.execute("INSERT INTO price_history(sku,ts,price,was_price,in_stock) VALUES(?,?,?,?,?)",
                                (sku, ts, price, was, int(stock)))
        return cur.lastrowid

    def delist_unseen(self, since, ts):
        """بعد اكتمال مسح كامل: المنتجات التي لم تظهر تُعتبر غير متوفرة."""
        rows = self.conn.execute("SELECT sku, price, was_price FROM products WHERE last_seen<? AND delisted=0", (since,)).fetchall()
        for r in rows:
            self.conn.execute("UPDATE products SET delisted=1, in_stock=0, last_changed=? WHERE sku=?", (ts, r["sku"]))
            self._history(r["sku"], ts, r["price"], r["was_price"], False)
        return len(rows)

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
