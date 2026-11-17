"""طلب الحفظ الواحد يتنفّذ مرة واحدة، حتى لو الصفحة وقعت والمستخدم ضغط تاني."""

from datetime import datetime

from flask import flash, has_request_context, redirect, request, url_for
from sqlalchemy import event
from sqlalchemy.orm import Session
from urllib.parse import urlsplit

from models import (
    ActivityLog, AlertDismissal, AppSetting, StoredPartyBalance, SubmissionReceipt,
)

_TOKEN_SKIP = (SubmissionReceipt, ActivityLog, AlertDismissal, AppSetting, StoredPartyBalance)
_DONE = False


def register_submit_once():
    global _DONE
    if _DONE:
        return
    event.listen(Session, "before_flush", _attach_token)
    _DONE = True


def repeated_submission_response():
    """لو نفس نموذج الحفظ رجع تاني بعد ما الأول اتحفظ، نرجّع المستخدم برسالة من غير تسجيل."""
    if not has_request_context() or request.method != "POST":
        return None
    token = _posted_token()
    if not token or SubmissionReceipt.query.get(token) is None:
        return None
    flash("الحركة دي اتسجلت قبل كده، ومش هتتكرر.", "warning")
    return redirect(_safe_return())


def duplicate_submission_error(error):
    return "submission_receipt" in str(error or "").lower()


def _posted_token():
    token = (request.form.get("post_token") or "").strip()
    if not token or len(token) > 80:
        return ""
    return token


def _safe_return():
    target = request.referrer or ""
    parts = urlsplit(target)
    if target and (not parts.netloc or parts.netloc == request.host):
        return target
    return url_for("index")


def _attach_token(session, flush_context, instances):
    """يتسجل مع الحركة في نفس الحفظ، فلو الطلب وقع بعد كده الضغط التاني ما يكرّرهاش."""
    del flush_context, instances
    if not has_request_context():
        return
    token = _posted_token()
    if not token or session.info.get("submit_once") == token:
        return
    for obj in session.new:
        if isinstance(obj, SubmissionReceipt) and obj.token == token:
            session.info["submit_once"] = token
            return
    if not any(obj is not None and not isinstance(obj, _TOKEN_SKIP) for obj in session.new):
        return
    session.info["submit_once"] = token
    session.add(SubmissionReceipt(
        token=token,
        created_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    ))
