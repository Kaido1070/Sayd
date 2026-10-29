"""نسخ احتياطية لقاعدة البيانات في GitHub Releases (لا تعتمد على ملفات التشغيل المؤقتة أو الكاش).

python -m sayd.backup save --db data/sayd.db        نسخة اليوم (مرة واحدة يومياً)
python -m sayd.backup list                           النسخ المتوفرة
python -m sayd.backup restore --name sayd-2026-10-29.db.gz --out data/sayd.db

الاحتفاظ: آخر 14 نسخة يومية، ونسخة أول كل شهر تبقى دائماً.
يحتاج GH_TOKEN و GITHUB_REPOSITORY (متوفران داخل GitHub Actions). لا يُطبع التوكن أبداً."""
import argparse
import gzip
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone

import httpx

TAG = "db-backups"
API = "https://api.github.com"
KEEP_DAYS = 14
NAME = re.compile(r"^sayd-(\d{4}-\d{2}-\d{2})\.db\.gz$")


class Backups:
    def __init__(self):
        self.repo = os.environ["GITHUB_REPOSITORY"]
        self.client = httpx.Client(timeout=120, follow_redirects=True, headers={
            "Authorization": f"Bearer {os.environ['GH_TOKEN']}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"})

    def _ok(self, r, what):
        if r.status_code >= 300:
            raise SystemExit(f"{what}: HTTP {r.status_code}")
        return r

    def release(self, create=True):
        r = self.client.get(f"{API}/repos/{self.repo}/releases/tags/{TAG}")
        if r.status_code == 200:
            return r.json()
        if r.status_code != 404 or not create:
            self._ok(r, "قراءة الإصدار")
        return self._ok(self.client.post(f"{API}/repos/{self.repo}/releases", json={
            "tag_name": TAG, "name": "نسخ احتياطية لقاعدة البيانات", "prerelease": True, "make_latest": "false",
            "body": "نسخ يومية تلقائية لقاعدة بيانات صيد (آخر 14 يوماً + أول كل شهر دائماً)."}), "إنشاء الإصدار").json()

    def assets(self, rel):
        out, page = [], 1
        while True:
            r = self._ok(self.client.get(f"{API}/repos/{self.repo}/releases/{rel['id']}/assets",
                                         params={"per_page": 100, "page": page}), "قائمة النسخ")
            out += r.json()
            if len(r.json()) < 100:
                return out
            page += 1

    def save(self, db_path):
        check(db_path)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        name = f"sayd-{today}.db.gz"
        rel = self.release()
        assets = self.assets(rel)
        if any(a["name"] == name for a in assets):
            print(f"نسخة اليوم موجودة: {name}")
        else:
            with tempfile.NamedTemporaryFile(suffix=".gz", delete=False) as tmp:
                with open(db_path, "rb") as src, gzip.GzipFile(fileobj=tmp, mode="wb", compresslevel=6) as gz:
                    shutil.copyfileobj(src, gz)
            with open(tmp.name, "rb") as f:
                self._ok(self.client.post(f"https://uploads.github.com/repos/{self.repo}/releases/{rel['id']}/assets",
                                          params={"name": name}, content=f.read(),
                                          headers={"Content-Type": "application/gzip"}), "رفع النسخة")
            os.unlink(tmp.name)
            print(f"تم حفظ {name}")
            assets = self.assets(rel)
        self.prune(assets)

    def prune(self, assets):
        cutoff = (datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
        dated = sorted(((m.group(1), a) for a in assets if (m := NAME.match(a["name"]))), key=lambda x: x[0])
        for day, a in dated[:-3]:  # آخر 3 نسخ تبقى دائماً
            if day < cutoff and not day.endswith("-01"):
                self._ok(self.client.delete(f"{API}/repos/{self.repo}/releases/assets/{a['id']}"), "حذف نسخة قديمة")
                print(f"حُذفت نسخة قديمة: {a['name']}")

    def list(self):
        rel = self.release(create=False)
        for a in sorted(self.assets(rel), key=lambda a: a["name"]):
            print(f"{a['name']}\t{a['size'] // 1024} KB\t{a['created_at']}")

    def restore(self, name, out):
        rel = self.release(create=False)
        asset = next((a for a in self.assets(rel) if a["name"] == name), None)
        if not asset:
            raise SystemExit(f"النسخة غير موجودة: {name}")
        r = self._ok(self.client.get(f"{API}/repos/{self.repo}/releases/assets/{asset['id']}",
                                     headers={"Accept": "application/octet-stream"}), "تنزيل النسخة")
        tmp = out + ".restore"
        try:
            with open(tmp, "wb") as f:
                f.write(gzip.decompress(r.content))
            check(tmp)
        except (OSError, sqlite3.DatabaseError, SystemExit) as e:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise SystemExit(f"النسخة غير صالحة ({type(e).__name__})؛ القاعدة الحالية لم تتغير")
        os.replace(tmp, out)
        print(f"تمت الاستعادة من {name}")


def check(path):
    """قاعدة سليمة وفيها جداول السجل."""
    c = sqlite3.connect(path)
    try:
        if c.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise SystemExit("فحص السلامة فشل")
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = {"products", "price_history", "price_periods"} - tables
        if missing:
            raise SystemExit(f"جداول ناقصة: {missing}")
    finally:
        c.close()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="sayd.backup")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("save").add_argument("--db", default="data/sayd.db")
    sub.add_parser("list")
    r = sub.add_parser("restore")
    r.add_argument("--name", required=True)
    r.add_argument("--out", default="data/sayd.db")
    a = ap.parse_args(argv)
    b = Backups()
    if a.cmd == "save":
        b.save(a.db)
    elif a.cmd == "list":
        b.list()
    else:
        b.restore(a.name, a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
