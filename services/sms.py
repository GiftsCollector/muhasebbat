import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request


class SmsError(Exception):
    pass


def normalize_egypt_phone(value):
    digits = "".join(ch for ch in (value or "") if ch.isdigit())
    if not digits:
        return ""
    if digits.startswith("0020"):
        digits = digits[2:]
    if digits.startswith("0") and len(digits) == 11:
        digits = "20" + digits[1:]
    if digits.startswith("20") and len(digits) == 12:
        return "+" + digits
    if len(digits) == 10 and digits.startswith("1"):
        return "+20" + digits
    return ""


def mask_phone(value):
    phone = normalize_egypt_phone(value) or (value or "").strip()
    if len(phone) < 6:
        return phone or "-"
    return phone[:4] + "****" + phone[-3:]


def sms_configured():
    provider = (os.getenv("SMS_PROVIDER") or "").strip().lower()
    if provider == "log":
        return True
    if (os.getenv("TWILIO_ACCOUNT_SID") or "").strip() and (os.getenv("TWILIO_AUTH_TOKEN") or "").strip():
        return True
    if provider in {"smsmisr", "http"} and (os.getenv("SMS_API_URL") or os.getenv("SMS_USERNAME") or "").strip():
        return True
    return False


def send_sms(phone, message):
    normalized = normalize_egypt_phone(phone)
    if not normalized:
        raise SmsError("رقم الموبايل غير صحيح")
    if not sms_configured():
        raise SmsError("خدمة الرسائل غير متصلة. أضف بيانات الإرسال في إعدادات السيرفر.")

    provider = (os.getenv("SMS_PROVIDER") or "").strip().lower()
    if provider == "log":
        return True
    if (os.getenv("TWILIO_ACCOUNT_SID") or "").strip():
        _send_twilio(normalized, message)
        return True
    if provider == "smsmisr":
        _send_smsmisr(normalized, message)
        return True
    if provider == "http":
        _send_http(normalized, message)
        return True
    raise SmsError("خدمة الرسائل غير متصلة")


def _post(url, data=None, headers=None, auth=None):
    body = None
    if isinstance(data, dict):
        body = urllib.parse.urlencode(data).encode("utf-8")
        headers = dict(headers or {})
        headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    elif data is not None:
        body = data if isinstance(data, bytes) else str(data).encode("utf-8")
        headers = dict(headers or {})
    req = urllib.request.Request(url, data=body, headers=headers or {}, method="POST")
    if auth:
        token = base64.b64encode(f"{auth[0]}:{auth[1]}".encode("utf-8")).decode("ascii")
        req.add_header("Authorization", f"Basic {token}")
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SmsError(f"فشل إرسال الرسالة ({exc.code})") from exc
    except urllib.error.URLError as exc:
        raise SmsError("تعذر الاتصال بخدمة الرسائل") from exc


def _send_twilio(phone, message):
    sid = (os.getenv("TWILIO_ACCOUNT_SID") or "").strip()
    token = (os.getenv("TWILIO_AUTH_TOKEN") or "").strip()
    sender = (os.getenv("TWILIO_FROM_NUMBER") or "").strip()
    if not sid or not token or not sender:
        raise SmsError("بيانات Twilio ناقصة")
    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    _post(url, {"To": phone, "From": sender, "Body": message}, auth=(sid, token))


def _send_smsmisr(phone, message):
    username = (os.getenv("SMS_USERNAME") or "").strip()
    password = (os.getenv("SMS_PASSWORD") or "").strip()
    sender = (os.getenv("SMS_SENDER") or "").strip()
    if not username or not password or not sender:
        raise SmsError("بيانات SMS Misr ناقصة")
    mobile = phone.replace("+", "")
    url = (os.getenv("SMS_API_URL") or "https://smsmisr.com/api/SMS/").strip()
    params = {
        "environment": os.getenv("SMS_ENVIRONMENT") or "1",
        "username": username,
        "password": password,
        "language": "2",
        "sender": sender,
        "mobile": mobile,
        "message": message,
    }
    full = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    req = urllib.request.Request(full, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        raise SmsError(f"فشل إرسال الرسالة ({exc.code})") from exc
    except urllib.error.URLError as exc:
        raise SmsError("تعذر الاتصال بخدمة الرسائل") from exc


def _send_http(phone, message):
    url = (os.getenv("SMS_API_URL") or "").strip()
    if not url:
        raise SmsError("رابط خدمة الرسائل غير مضبوط")
    payload = {
        "phone": phone,
        "mobile": phone.replace("+", ""),
        "message": message,
        "sender": (os.getenv("SMS_SENDER") or "").strip(),
    }
    headers = {"Content-Type": "application/json"}
    token = (os.getenv("SMS_API_TOKEN") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    _post(url, json.dumps(payload).encode("utf-8"), headers=headers)
