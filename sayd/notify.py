"""الإرسال: تيليجرام أساسي و ntfy احتياطي، والإيميل قناة إضافية. لا يُطبع أي سر في السجلات."""
import logging
import smtplib
import ssl
import time
from email.message import EmailMessage

import httpx

log = logging.getLogger("sayd.notify")
TG_LIMIT = 3900


class Notifier:
    def __init__(self, secrets):
        self.s = secrets
        self.client = httpx.Client(timeout=20)

    @property
    def email_configured(self):
        return bool(self.s["smtp_user"] and self.s["smtp_password"])

    @property
    def configured(self):
        return bool(self.s["telegram_token"] and self.s["telegram_chat"]) or bool(self.s["ntfy_topic"]) \
            or self.email_configured

    def send(self, html_text, plain_text, title="Sayd", click=None):
        """يرجع أسماء القنوات التي نجحت (مفصولة بفاصلة) أو None."""
        done = []
        if self.s["telegram_token"] and self.s["telegram_chat"] and self._telegram(html_text):
            done.append("telegram")
        elif self.s["ntfy_topic"] and self._ntfy(plain_text, title, click):
            done.append("ntfy")
        if self.email_configured and self._email(html_text, plain_text, title):
            done.append("email")
        return ",".join(done) or None

    def _email(self, html_text, plain_text, title):
        msg = EmailMessage()
        msg["Subject"] = title
        msg["From"] = f"Sayd <{self.s['smtp_user']}>"
        msg["To"] = self.s["email_to"] or self.s["smtp_user"]
        msg.set_content(plain_text)
        body = html_text.replace("\n", "<br>")
        msg.add_alternative(f'<div dir="rtl" style="font-family:Tahoma,Arial,sans-serif;font-size:15px;line-height:1.7">{body}</div>',
                            subtype="html")
        try:
            port = int(self.s["smtp_port"] or 465)
            if port == 465:
                with smtplib.SMTP_SSL(self.s["smtp_host"], port, context=ssl.create_default_context(), timeout=30) as s:
                    s.login(self.s["smtp_user"], self.s["smtp_password"])
                    s.send_message(msg)
            else:
                with smtplib.SMTP(self.s["smtp_host"], port, timeout=30) as s:
                    s.starttls(context=ssl.create_default_context())
                    s.login(self.s["smtp_user"], self.s["smtp_password"])
                    s.send_message(msg)
        except (smtplib.SMTPException, OSError, ValueError) as e:
            log.warning("الإيميل: فشل الإرسال (%s)", type(e).__name__)
            return False
        return True

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
