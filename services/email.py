import os
import re
import smtplib
import time
from collections import defaultdict
from email.message import EmailMessage


class MailError(Exception):
    pass


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_rate_hits = defaultdict(list)


def normalize_email(value):
    email = (value or "").strip().lower()
    if not email or len(email) > 254 or not _EMAIL_RE.match(email):
        return ""
    return email


def mask_email(value):
    email = normalize_email(value) or (value or "").strip()
    if "@" not in email:
        return email or "-"
    local, _, domain = email.partition("@")
    if not local:
        return email
    visible = local[0]
    return f"{visible}***@{domain}"


def mail_configured():
    return bool((os.getenv("MAIL_USERNAME") or "").strip() and (os.getenv("MAIL_PASSWORD") or "").strip())


def clear_rate_limits():
    _rate_hits.clear()


def rate_limited(key, max_hits, window_seconds):
    now = time.time()
    recent = [stamp for stamp in _rate_hits[key] if now - stamp < window_seconds]
    if len(recent) >= max_hits:
        _rate_hits[key] = recent
        return True
    recent.append(now)
    _rate_hits[key] = recent
    return False


def send_email(to_addr, subject, body):
    normalized = normalize_email(to_addr)
    if not normalized:
        raise MailError("البريد الإلكتروني غير صحيح")
    if not mail_configured():
        raise MailError("خدمة البريد غير متصلة. أضف بيانات الإرسال في إعدادات السيرفر.")

    server = (os.getenv("MAIL_SERVER") or "smtp.gmail.com").strip()
    try:
        port = int((os.getenv("MAIL_PORT") or "587").strip() or "587")
    except ValueError:
        port = 587
    use_tls = (os.getenv("MAIL_USE_TLS") or "true").strip().lower() in {"1", "true", "yes", "on"}
    username = (os.getenv("MAIL_USERNAME") or "").strip()
    password = os.getenv("MAIL_PASSWORD") or ""
    sender = (os.getenv("MAIL_DEFAULT_SENDER") or username).strip()

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = normalized
    message.set_content(body)

    try:
        with smtplib.SMTP(server, port, timeout=20) as smtp:
            smtp.ehlo()
            if use_tls:
                smtp.starttls()
                smtp.ehlo()
            smtp.login(username, password)
            smtp.send_message(message)
    except smtplib.SMTPAuthenticationError as exc:
        raise MailError("فشل تسجيل الدخول إلى خدمة البريد. راجع بيانات الإرسال على السيرفر.") from exc
    except (smtplib.SMTPException, OSError) as exc:
        raise MailError("تعذر إرسال رسالة البريد حالياً.") from exc
    return True
