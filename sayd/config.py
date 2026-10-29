import os
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load(path=None):
    with open(path or ROOT / "config.toml", "rb") as f:
        cfg = tomllib.load(f)
    cfg.setdefault("watch", [])
    # الأسرار من متغيرات البيئة فقط (GitHub Secrets)
    cfg["secrets"] = {
        "telegram_token": os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
        "telegram_chat": os.environ.get("TELEGRAM_CHAT_ID", "").strip(),
        "ntfy_topic": os.environ.get("NTFY_TOPIC", "").strip(),
        "ntfy_server": os.environ.get("NTFY_SERVER", "").strip() or "https://ntfy.sh",
        "ntfy_token": os.environ.get("NTFY_TOKEN", "").strip(),
        # إيميل عبر SMTP (مثلاً Gmail مع App Password)
        "smtp_host": os.environ.get("SMTP_HOST", "").strip() or "smtp.gmail.com",
        "smtp_port": os.environ.get("SMTP_PORT", "").strip() or "465",
        "smtp_user": os.environ.get("SMTP_USER", "").strip(),
        "smtp_password": os.environ.get("SMTP_PASSWORD", "").strip(),
        "email_to": os.environ.get("EMAIL_TO", "").strip(),
    }
    return cfg
