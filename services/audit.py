from datetime import datetime

from flask import g, has_request_context
from sqlalchemy import event, inspect as sa_inspect
from sqlalchemy.orm import Session

from models import (
    ActivityLog, ActorStamp, ChartOfAccount, ClientReceipt, CostEntry,
    CustodySettlement, DriverCompensationEntry, Employee, EmployeeAttendance,
    EmployeeSalaryPayment, Equipment, Estimation,
    InventoryTransaction, JournalEntry, LaborEntry, PayrollSlip, ProgressPayment, Project,
    PurchaseOrder, Subcontractor, SubcontractorPayment, Supplier, SupplierPayment,
)


ENTITY_LABELS = {
    JournalEntry: "قيد يومية",
    ProgressPayment: "مستخلص",
    SubcontractorPayment: "دفعة مقاول باطن",
    ClientReceipt: "تحصيل عميل",
    SupplierPayment: "سداد مورد",
    PurchaseOrder: "أمر شراء",
    InventoryTransaction: "حركة مخزون",
    LaborEntry: "حركة عمالة",
    Equipment: "معدة",
    CustodySettlement: "تسوية عهدة",
    DriverCompensationEntry: "محاسبة سائق",
    Estimation: "مقايسة",
    CostEntry: "قيد تكلفة",
    Project: "مشروع",
    ChartOfAccount: "حساب",
    Supplier: "مورد",
    Subcontractor: "مقاول باطن",
    Employee: "موظف",
    EmployeeAttendance: "حضور موظف",
    PayrollSlip: "كشف مرتب",
    EmployeeSalaryPayment: "صرف مرتب",
}

TRACKED_TYPES = tuple(ENTITY_LABELS.keys())
_HOOKS_REGISTERED = False


def now_stamp():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def current_actor():
    if has_request_context():
        user = getattr(g, "current_user", None)
        if user is not None:
            return user.id, user.full_name or user.username
    return None, "النظام"


def is_tracked(obj):
    return isinstance(obj, TRACKED_TYPES) and not isinstance(obj, ActivityLog)


def entity_label(obj):
    return ENTITY_LABELS.get(type(obj), type(obj).__name__)


def summarize(obj):
    pieces = []
    for attr in (
        "display_number",
        "display_name",
        "document_number",
        "payment_number",
        "order_number",
        "code",
        "name",
        "client_name",
        "driver_name",
        "entity_name",
        "material_name",
        "item_name",
        "description",
    ):
        value = getattr(obj, attr, None)
        if value:
            pieces.append(str(value)[:120])
            break
    description = getattr(obj, "description", None)
    if description and str(description) not in pieces:
        pieces.append(str(description)[:120])
    date_value = getattr(obj, "date", None)
    if date_value:
        pieces.append(str(date_value))
    amount = getattr(obj, "amount", None)
    if amount in (None, 0, 0.0):
        amount = getattr(obj, "total_value", None) or getattr(obj, "net_value", None) or getattr(obj, "gross_amount", None)
    if amount not in (None, 0, 0.0):
        pieces.append(str(amount))
    return " — ".join(pieces)[:500] or type(obj).__name__


def detect_action(obj, is_new=False, is_deleted=False):
    if is_deleted:
        return "حذف"
    if is_new:
        return "إضافة"
    status_attr = sa_inspect(obj).attrs.status if hasattr(obj, "status") else None
    if status_attr is not None and status_attr.history.has_changes():
        status = getattr(obj, "status", "")
        if status == "مرحل":
            return "ترحيل"
        if status in ("مسودة",):
            return "إلغاء ترحيل"
    return "تعديل"


def _before_flush(session, flush_context, instances):
    now = now_stamp()
    user_id, user_name = current_actor()
    pending = session.info.setdefault("activity_events", [])

    for obj in list(session.new):
        if not is_tracked(obj):
            continue
        if isinstance(obj, ActorStamp):
            if not obj.created_at:
                obj.created_at = now
                obj.created_by_id = user_id
                obj.created_by_name = user_name
            obj.updated_at = now
            obj.updated_by_id = user_id
            obj.updated_by_name = user_name
        pending.append({"kind": "new", "obj": obj, "action": detect_action(obj, is_new=True), "at": now, "user_id": user_id, "user_name": user_name})

    for obj in list(session.dirty):
        if not is_tracked(obj) or obj in session.new:
            continue
        if not sa_inspect(obj).modified:
            continue
        if isinstance(obj, ActorStamp):
            obj.updated_at = now
            obj.updated_by_id = user_id
            obj.updated_by_name = user_name
        pending.append({"kind": "dirty", "obj": obj, "action": detect_action(obj), "at": now, "user_id": user_id, "user_name": user_name})

    for obj in list(session.deleted):
        if not is_tracked(obj):
            continue
        pending.append(
            {
                "kind": "deleted",
                "action": "حذف",
                "at": now,
                "user_id": user_id,
                "user_name": user_name,
                "entity_type": type(obj).__name__,
                "entity_label": entity_label(obj),
                "entity_id": getattr(obj, "id", None),
                "summary": summarize(obj),
            }
        )


def _after_flush(session, flush_context):
    pending = session.info.pop("activity_events", [])
    if not pending:
        return
    for event_row in pending:
        if event_row["kind"] == "deleted":
            session.add(
                ActivityLog(
                    created_at=event_row["at"],
                    user_id=event_row["user_id"],
                    user_name=event_row["user_name"],
                    action=event_row["action"],
                    entity_type=event_row["entity_type"],
                    entity_label=event_row["entity_label"],
                    entity_id=event_row["entity_id"],
                    summary=event_row["summary"],
                )
            )
            continue
        obj = event_row["obj"]
        session.add(
            ActivityLog(
                created_at=event_row["at"],
                user_id=event_row["user_id"],
                user_name=event_row["user_name"],
                action=event_row["action"],
                entity_type=type(obj).__name__,
                entity_label=entity_label(obj),
                entity_id=getattr(obj, "id", None),
                summary=summarize(obj),
            )
        )


def register_audit_hooks():
    global _HOOKS_REGISTERED
    if _HOOKS_REGISTERED:
        return
    event.listen(Session, "before_flush", _before_flush, propagate=True)
    event.listen(Session, "after_flush", _after_flush, propagate=True)
    _HOOKS_REGISTERED = True
