"""حفظ رصيد كشف الحساب مع الحركة، وقراءته جاهزًا عند فتح الشاشة.

المعادلة نفسها ما بتتغيرش. اللي بيتغير إن الشاشة ما تعيدش حساب التاريخ كله في كل فتحة.
"""
import json
import logging

from sqlalchemy import event, inspect as sa_inspect
from sqlalchemy.orm import Session

from models import (
    ChartOfAccount, ClientReceipt, CustodySettlement, DriverCompensationEntry, Equipment,
    Estimation, EstimationItem, JournalEntry, LaborEntry, ProgressPayment, ProgressPaymentItem,
    Project, PurchaseOrder, PurchaseOrderItem, StoredPartyBalance, Subcontractor,
    SubcontractorPayment, SubcontractorRetentionRelease, Supplier, SupplierPayment, db,
)

logger = logging.getLogger(__name__)

PARTY_CATEGORIES = {"مقاولي الباطن", "العملاء", "الموردين", "موردين", "المشاريع"}
_SAVE_EVERY = 20


def register_balance_hooks():
    if getattr(register_balance_hooks, "_done", False):
        return
    event.listen(Session, "before_flush", _before_flush)
    event.listen(Session, "before_commit", _before_commit)
    register_balance_hooks._done = True


def account_screen_views(accounts):
    """نفس أرقام كشف الحساب لشاشة الحسابات والرئيسية، من الرصيد المحفوظ."""
    party_accounts = [
        account for account in (accounts or [])
        if (getattr(account, "category", None) or "").strip() in PARTY_CATEGORIES
    ]
    if not party_accounts:
        return {}
    stored = _load("account", [account.id for account in party_accounts])
    missing = [account for account in party_accounts if account.id not in stored]
    if missing:
        _prepare_reads(missing)
        pending = 0
        for account in missing:
            payload = _guarded(lambda current=account: _capture_account(current))
            if not payload:
                continue
            _upsert("account", account.id, payload)
            stored[account.id] = payload
            pending += 1
            if pending >= _SAVE_EVERY:
                _persist_page_snapshots()
                pending = 0
        if pending:
            _persist_page_snapshots()
    return stored


def ensure_subcontractor_snapshots(subcontractors):
    rows = list(subcontractors or [])
    stored = _load("subcontractor", [row.id for row in rows])
    missing = [row for row in rows if row.id not in stored]
    if not missing:
        return stored
    _prepare_reads(missing)
    pending = 0
    for sub in missing:
        payload = _guarded(lambda current=sub: _write_subcontractor(current))
        if payload is None:
            continue
        stored[sub.id] = payload
        pending += 1
        if pending >= _SAVE_EVERY:
            _persist_page_snapshots()
            pending = 0
    if pending:
        _persist_page_snapshots()
    return stored


def ensure_supplier_snapshots(suppliers):
    rows = list(suppliers or [])
    stored = _load("supplier", [row.id for row in rows])
    missing = [row for row in rows if row.id not in stored]
    if not missing:
        return stored
    _prepare_reads(missing)
    pending = 0
    for supplier in missing:
        payload = _guarded(lambda current=supplier: _write_supplier(current))
        if payload is None:
            continue
        stored[supplier.id] = payload
        pending += 1
        if pending >= _SAVE_EVERY:
            _persist_page_snapshots()
            pending = 0
    if pending:
        _persist_page_snapshots()
    return stored


def subcontractor_net_dues(subcontractor_ids):
    ids = [item for item in subcontractor_ids if item]
    if not ids:
        return {}
    subs = Subcontractor.query.filter(Subcontractor.id.in_(ids)).all()
    summaries = ensure_subcontractor_snapshots(subs)
    return {sid: round(float(summaries[sid].get("net_due") or 0), 2) for sid in summaries}


def subcontractor_due_total():
    summaries = ensure_subcontractor_snapshots(Subcontractor.query.all())
    return round(sum(float(item.get("net_due") or 0) for item in summaries.values()), 2)


def _prepare_reads(rows):
    from services.accounting import enable_party_document_cache, enable_project_document_cache

    enable_party_document_cache()
    if any((getattr(row, "category", None) or "").strip() == "المشاريع" for row in rows):
        enable_project_document_cache()


def _capture_account(account):
    _refresh_account_id(account.id)
    return _load("account", [account.id]).get(account.id)


def _account_payload(account):
    from services.accounting import as_float, present_account_statement

    screen = present_account_statement(account)
    if not screen.get("party_kind"):
        return None
    return {
        "closing_balance": round(as_float(screen.get("closing_balance")), 2),
        "closing_display": screen.get("closing_display") or 0,
        "closing_side": screen.get("closing_side") or "",
        "party_kind": screen.get("party_kind") or "",
        "party_net": screen.get("party_net"),
    }


def _write_subcontractor(sub):
    from services.accounting import (
        _finish_account_statement, as_float, build_subcontractor_statement, linked_account_for_subcontractor,
    )

    account = linked_account_for_subcontractor(sub)
    statement = build_subcontractor_statement(sub, party_account=account, create_account=False)
    payload = {
        "total_works": round(as_float(statement.get("total_works")), 2),
        "total_paid": round(as_float(statement.get("total_paid")), 2),
        "total_deductions": round(as_float(statement.get("total_deductions")), 2),
        "total_retention_released": round(as_float(statement.get("total_retention_released")), 2),
        "retention_outstanding": round(as_float(statement.get("retention_outstanding")), 2),
        "net_due": round(as_float(statement.get("net_due")), 2),
    }
    _upsert("subcontractor", sub.id, payload)
    if account is not None:
        done = _finish_account_statement(account, statement["movements"], None, None, "مقاول باطن")
        done["party_net"] = statement["net_due"]
        account_payload = _account_payload_from_screen(done)
        if account_payload:
            _upsert("account", account.id, account_payload)
    return payload


def _write_supplier(supplier):
    from services.accounting import as_float, present_account_statement, supplier_balance_parts

    parts = supplier_balance_parts(supplier)
    payload = {
        "opening": round(as_float(parts.get("opening")), 2),
        "purchases": round(as_float(parts.get("purchases")), 2),
        "payments": round(as_float(parts.get("payments")), 2),
        "due": round(as_float(parts.get("due")), 2),
    }
    _upsert("supplier", supplier.id, payload)
    account = parts.get("account")
    if account is not None:
        screen = present_account_statement(account)
        account_payload = _account_payload_from_screen(screen)
        if account_payload:
            _upsert("account", account.id, account_payload)
    return payload


def _account_payload_from_screen(screen):
    from services.accounting import as_float

    if not screen or not screen.get("party_kind"):
        return None
    return {
        "closing_balance": round(as_float(screen.get("closing_balance")), 2),
        "closing_display": screen.get("closing_display") or 0,
        "closing_side": screen.get("closing_side") or "",
        "party_kind": screen.get("party_kind") or "",
        "party_net": screen.get("party_net"),
    }


def _refresh_subcontractor_id(subcontractor_id):
    sub = Subcontractor.query.get(subcontractor_id) if subcontractor_id else None
    if sub is None:
        _delete("subcontractor", subcontractor_id)
        return
    _write_subcontractor(sub)


def _refresh_supplier_id(supplier_id):
    supplier = Supplier.query.get(supplier_id) if supplier_id else None
    if supplier is None:
        _delete("supplier", supplier_id)
        return
    _write_supplier(supplier)


def _refresh_account_id(account_id):
    from services.accounting import _chosen_subcontractor, _chosen_supplier

    account = ChartOfAccount.query.get(account_id) if account_id else None
    if account is None:
        _delete("account", account_id)
        return
    category = (account.category or "").strip()
    if category == "مقاولي الباطن":
        sub = _chosen_subcontractor(account)
        if sub is not None:
            _write_subcontractor(sub)
            return
    if category in {"الموردين", "موردين"}:
        supplier = _chosen_supplier(account)
        if supplier is not None:
            _write_supplier(supplier)
            return
    if category not in PARTY_CATEGORIES:
        return
    payload = _account_payload(account)
    if payload:
        _upsert("account", account.id, payload)


def _refresh_project_id(project_id):
    from services.accounting import find_account_by_person

    project = Project.query.get(project_id) if project_id else None
    if project is None:
        return
    account = find_account_by_person("المشاريع", project.project_name or project.code)
    if account is not None:
        _refresh_account_id(account.id)


def _refresh_client_name(name):
    from services.accounting import find_client_account

    account = find_client_account(name)
    if account is not None:
        _refresh_account_id(account.id)


def _refresh_client_project(project_id):
    project = Project.query.get(project_id) if project_id else None
    if project is not None and (project.client_name or "").strip():
        _refresh_client_name(project.client_name)


def _load(scope, ids):
    if not ids:
        return {}
    rows = StoredPartyBalance.query.filter(
        StoredPartyBalance.scope == scope,
        StoredPartyBalance.scope_id.in_(list(ids)),
    ).all()
    found = {}
    for row in rows:
        try:
            found[row.scope_id] = json.loads(row.payload or "{}")
        except (TypeError, ValueError):
            continue
    return found


def _upsert(scope, scope_id, payload):
    if not scope_id:
        return
    row = StoredPartyBalance.query.filter_by(scope=scope, scope_id=scope_id).first()
    if row is None:
        row = StoredPartyBalance(scope=scope, scope_id=scope_id)
        db.session.add(row)
    row.payload = json.dumps(payload, ensure_ascii=False, default=_json_default)


def _delete(scope, scope_id):
    if not scope_id:
        return
    StoredPartyBalance.query.filter_by(scope=scope, scope_id=scope_id).delete(synchronize_session=False)


def _json_default(value):
    return float(value)


def _guarded(func):
    from flask import current_app

    nested = db.session.begin_nested()
    try:
        result = func()
        db.session.flush()
        nested.commit()
        return result
    except Exception:
        nested.rollback()
        if _testing():
            raise
        logger.exception("party balance refresh failed")
        current_app.logger.exception("party balance refresh failed")
        return None


def _testing():
    try:
        from flask import current_app
        return bool(current_app.config.get("TESTING") or current_app.propagate_exceptions)
    except Exception:
        return False


def _persist_page_snapshots():
    from services.accounting import is_page_view

    if not is_page_view():
        return
    business = [
        obj for obj in list(db.session.new) + list(db.session.dirty) + list(db.session.deleted)
        if not isinstance(obj, StoredPartyBalance)
    ]
    if business:
        return
    db.session.info["skip_balance_refresh"] = True
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        if _testing():
            raise
        logger.exception("saving party balances failed")
    finally:
        db.session.info.pop("skip_balance_refresh", None)


def _before_flush(session, flush_context, instances):
    if session.info.get("skip_balance_refresh") or session.info.get("_balancing"):
        return
    dirty = session.info.setdefault("_balance_dirty", set())
    seen = set()
    for bucket in (session.new, session.dirty, session.deleted):
        for obj in list(bucket):
            marker = id(obj)
            if marker in seen or isinstance(obj, StoredPartyBalance):
                continue
            seen.add(marker)
            _collect(session, dirty, obj)


def _before_commit(session):
    if session.info.get("skip_balance_refresh") or session.info.get("_balancing"):
        session.info.pop("_balance_dirty", None)
        return
    pending = session.info.pop("_balance_dirty", None)
    if not pending:
        return
    session.info["_balancing"] = True
    try:
        _resolve_pending(pending)
        for kind, value in pending:
            _apply_mark(kind, value)
    finally:
        session.info["_balancing"] = False


def _apply_mark(kind, value):
    if kind == "subcontractor":
        _guarded(lambda: _refresh_subcontractor_id(value))
    elif kind == "supplier":
        _guarded(lambda: _refresh_supplier_id(value))
    elif kind == "account":
        _guarded(lambda: _refresh_account_id(value))
    elif kind == "project":
        _guarded(lambda: _refresh_project_id(value))
    elif kind == "client_name":
        _guarded(lambda: _refresh_client_name(value))
    elif kind == "client_project":
        _guarded(lambda: _refresh_client_project(value))
    elif kind == "drop_sub":
        _guarded(lambda: _delete("subcontractor", value) or True)
    elif kind == "drop_supplier":
        _guarded(lambda: _delete("supplier", value) or True)


def _resolve_pending(pending):
    extra = set()
    for kind, value in list(pending):
        if kind == "progress_payment":
            payment = ProgressPayment.query.get(value)
            if payment is None:
                continue
            if payment.subcontractor_id:
                extra.add(("subcontractor", payment.subcontractor_id))
            if payment.project_id:
                extra.add(("project", payment.project_id))
                if not payment.subcontractor_id:
                    extra.add(("client_project", payment.project_id))
        elif kind == "purchase_order":
            order = PurchaseOrder.query.get(value)
            if order is None:
                continue
            if order.supplier_id:
                extra.add(("supplier", order.supplier_id))
            if order.project_id:
                extra.add(("project", order.project_id))
        elif kind == "estimation":
            row = Estimation.query.get(value)
            if row is None:
                continue
            if row.project_id:
                extra.add(("project", row.project_id))
            if (row.client_name or "").strip():
                extra.add(("client_name", row.client_name.strip()))
    pending.update(extra)


def _collect(session, dirty, obj):
    if isinstance(obj, ProgressPayment):
        _mark_ids(dirty, obj, "subcontractor_id", "subcontractor")
        _mark_ids(dirty, obj, "project_id", "project")
        if not getattr(obj, "subcontractor_id", None):
            _mark_ids(dirty, obj, "project_id", "client_project")
    elif isinstance(obj, ProgressPaymentItem):
        _mark_ids(dirty, obj, "progress_payment_id", "progress_payment")
    elif isinstance(obj, (SubcontractorPayment, SubcontractorRetentionRelease)):
        _mark_ids(dirty, obj, "subcontractor_id", "subcontractor")
        _mark_ids(dirty, obj, "project_id", "project")
    elif isinstance(obj, JournalEntry):
        _mark_ids(dirty, obj, "debit_account_id", "account")
        _mark_ids(dirty, obj, "credit_account_id", "account")
        _mark_ids(dirty, obj, "project_id", "project")
    elif isinstance(obj, ClientReceipt):
        _mark_text(dirty, obj, "client_name", "client_name")
        _mark_ids(dirty, obj, "project_id", "project")
        _mark_ids(dirty, obj, "treasury_account_id", "account")
    elif isinstance(obj, SupplierPayment):
        _mark_ids(dirty, obj, "supplier_id", "supplier")
        _mark_ids(dirty, obj, "project_id", "project")
        _mark_ids(dirty, obj, "treasury_account_id", "account")
    elif isinstance(obj, PurchaseOrder):
        _mark_ids(dirty, obj, "supplier_id", "supplier")
        _mark_ids(dirty, obj, "project_id", "project")
    elif isinstance(obj, PurchaseOrderItem):
        _mark_ids(dirty, obj, "purchase_order_id", "purchase_order")
    elif isinstance(obj, (CustodySettlement, LaborEntry, DriverCompensationEntry)):
        _mark_ids(dirty, obj, "project_id", "project")
    elif isinstance(obj, Equipment):
        _mark_ids(dirty, obj, "project_id", "project")
    elif isinstance(obj, Estimation):
        _mark_ids(dirty, obj, "project_id", "project")
        _mark_text(dirty, obj, "client_name", "client_name")
    elif isinstance(obj, EstimationItem):
        _mark_ids(dirty, obj, "estimation_id", "estimation")
    elif isinstance(obj, ChartOfAccount):
        if obj.id and (
            obj in session.new
            or _attr_changed(obj, "opening_balance")
            or _attr_changed(obj, "name")
            or _attr_changed(obj, "category")
        ):
            dirty.add(("account", obj.id))
    elif isinstance(obj, Subcontractor):
        if obj in session.deleted:
            if obj.id:
                dirty.add(("drop_sub", obj.id))
        elif obj.id:
            dirty.add(("subcontractor", obj.id))
    elif isinstance(obj, Supplier):
        if obj in session.deleted:
            if obj.id:
                dirty.add(("drop_supplier", obj.id))
        elif obj.id:
            dirty.add(("supplier", obj.id))


def _attr_changed(obj, attr):
    try:
        return sa_inspect(obj).attrs[attr].history.has_changes()
    except Exception:
        return False


def _mark_ids(dirty, obj, attr, kind):
    for value in _history_values(obj, attr):
        number = _as_int(value)
        if number:
            dirty.add((kind, number))


def _mark_text(dirty, obj, attr, kind):
    for value in _history_values(obj, attr):
        text = (value or "").strip() if isinstance(value, str) else ""
        if text:
            dirty.add((kind, text))


def _history_values(obj, attr):
    values = []
    current = getattr(obj, attr, None)
    if current is not None:
        values.append(current)
    try:
        history = sa_inspect(obj).attrs[attr].history
    except Exception:
        return values
    values.extend(list(history.deleted or ()))
    return values


def _as_int(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number or None
