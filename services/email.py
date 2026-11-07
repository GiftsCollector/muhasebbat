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


def _mail_username():
    return (os.getenv("MAIL_USERNAME") or "").strip().strip('"').strip("'")


def _mail_password():
    raw = (os.getenv("MAIL_PASSWORD") or "").strip().strip('"').strip("'")
    return re.sub(r"\s+", "", raw)


def mail_configured():
    return bool(_mail_username() and _mail_password())


def clear_rate_limits():
    _rate_hits.clear()


def rate_limited(key, max_hits, window_seconds):
    if rate_exceeded(key, max_hits, window_seconds):
        return True
    rate_record(key, window_seconds)
    return False


def rate_exceeded(key, max_hits, window_seconds):
    now = time.time()
    recent = [stamp for stamp in _rate_hits[key] if now - stamp < window_seconds]
    _rate_hits[key] = recent
    return len(recent) >= max_hits


def rate_record(key, window_seconds=3600):
    now = time.time()
    recent = [stamp for stamp in _rate_hits[key] if now - stamp < window_seconds]
    recent.append(now)
    _rate_hits[key] = recent


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
    username = _mail_username()
    password = _mail_password()
    sender = (os.getenv("MAIL_DEFAULT_SENDER") or username).strip() or username

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = normalized
    message.set_content(body)

    try:
        if port == 465:
            context_smtp = smtplib.SMTP_SSL(server, port, timeout=20)
        else:
            context_smtp = smtplib.SMTP(server, port, timeout=20)
        with context_smtp as smtp:
            smtp.ehlo()
            if use_tls and port != 465:
                smtp.starttls()
                smtp.ehlo()
            smtp.login(username, password)
            smtp.send_message(message)
    except smtplib.SMTPAuthenticationError as exc:
        print("MAIL_AUTH_FAILED")
        raise MailError("فشل تسجيل الدخول إلى خدمة البريد. راجع بيانات الإرسال على السيرفر.") from exc
    except (smtplib.SMTPException, OSError) as exc:
        print(f"MAIL_SEND_FAILED {type(exc).__name__}")
        raise MailError("تعذر إرسال رسالة البريد حالياً.") from exc
    return True
