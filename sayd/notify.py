"""الإرسال: تيليجرام أساسي و ntfy احتياطي. لا يُطبع أي توكن أو رابط يحتويه في السجلات."""
import logging
import time

import httpx

log = logging.getLogger("sayd.notify")
TG_LIMIT = 3900


class Notifier:
    def __init__(self, secrets):
        self.s = secrets
        self.client = httpx.Client(timeout=20)

    @property
    def configured(self):
        return bool(self.s["telegram_token"] and self.s["telegram_chat"]) or bool(self.s["ntfy_topic"])

    def send(self, html_text, plain_text, title="Sayd", click=None):
        """يرجع اسم القناة التي نجحت أو None."""
        if self.s["telegram_token"] and self.s["telegram_chat"] and self._telegram(html_text):
            return "telegram"
        if self.s["ntfy_topic"] and self._ntfy(plain_text, title, click):
            return "ntfy"
        return None

    def _telegram(self, text):
        url = f"https://api.telegram.org/bot{self.s['telegram_token']}/sendMessage"
        body = {"chat_id": self.s["telegram_chat"], "text": text[:4096], "parse_mode": "HTML",
                "disable_web_page_preview": True}
        for attempt in range(3):
            try:
                r = self.client.post(url, json=body)
            except httpx.HTTPError as e:
                log.warning("تيليجرام: خطأ اتصال (%s)", type(e).__name__)
                time.sleep(3)
                continue
            if r.status_code == 200:
                time.sleep(1.1)  # حد تيليجرام: رسالة/ثانية للمحادثة
                return True
            if r.status_code == 429:
                wait = (r.json().get("parameters") or {}).get("retry_after", 5)
                time.sleep(min(int(wait) + 1, 60))
                continue
            if r.status_code == 400 and body.get("parse_mode"):
                # تنسيق HTML غير صالح: أعد الإرسال كنص عادي
                body.pop("parse_mode")
                continue
            log.warning("تيليجرام: فشل الإرسال (HTTP %s)", r.status_code)
            return False
        return False

    def _ntfy(self, text, title, click):
        headers = {"Authorization": f"Bearer {self.s['ntfy_token']}"} if self.s["ntfy_token"] else {}
        body = {"topic": self.s["ntfy_topic"], "message": text[:4000], "title": title}
        if click:
            body["click"] = click
        try:
            r = self.client.post(self.s["ntfy_server"].rstrip("/"), json=body, headers=headers)
        except httpx.HTTPError as e:
            log.warning("ntfy: خطأ اتصال (%s)", type(e).__name__)
            return False
        if r.status_code != 200:
            log.warning("ntfy: فشل الإرسال (HTTP %s)", r.status_code)
            return False
        return True

    def close(self):
        self.client.close()
