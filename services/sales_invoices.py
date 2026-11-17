"""سجل المبيعات والمستخلصات: القوائم الثابتة (نفس شيت الإكسل) وحالة السداد لكل فاتورة.

المحصل لكل فاتورة = مجموع سندات التحصيل المربوطة بيها. ده للعرض بس،
والفلوس نفسها بتدخل الخزنة من سند التحصيل.
"""
from collections import defaultdict

from models import ClientReceipt, ProgressPayment, SalesItem, db

SALES_OPERATION_TYPES = [
    "مستخلص جاري", "مستخلص ختامي", "توريد خامات", "أعمال نقل ومعدات",
    "فاتورة مبيعات عامة", "دفعة مقدمة", "أوامر تغييرية",
]
DEFAULT_OPERATION_TYPE = "مستخلص جاري"
SALES_ITEM_CATEGORIES = ["توريدات", "مقاولات", "نقل ومعدات", "خدمات عامة"]
SALES_UNITS = [
    "متر مكعب (م³)", "متر مربع (م²)", "متر طولي (م.ط)", "طن", "نقلة / حمولة",
    "ساعة تشغيل", "يوم عمل", "قطعة / عدد", "مقطوعية",
]
STATUS_PAID = "مسدد بالكامل"
STATUS_PARTIAL = "مسدد جزئياً"
STATUS_DUE = "آجل / مستحق"
PAYMENT_STATUSES = [STATUS_PAID, STATUS_PARTIAL, STATUS_DUE]


def _num(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def collected_by_invoice(invoice_ids=None):
    query = db.session.query(ClientReceipt.progress_payment_id, ClientReceipt.amount).filter(
        ClientReceipt.progress_payment_id.isnot(None)
    )
    if invoice_ids is not None:
        ids = [i for i in invoice_ids if i]
        if not ids:
            return {}
        query = query.filter(ClientReceipt.progress_payment_id.in_(ids))
    totals = defaultdict(float)
    for invoice_id, amount in query.all():
        totals[invoice_id] += _num(amount)
    return {key: round(value, 2) for key, value in totals.items()}


def payment_status(net_value, collected):
    net = round(_num(net_value), 2)
    paid = round(_num(collected), 2)
    if net <= 0:
        return ""
    if paid >= net:
        return STATUS_PAID
    if paid > 0:
        return STATUS_PARTIAL
    return STATUS_DUE


def operation_label(payment):
    return (getattr(payment, "operation_type", None) or "").strip() or DEFAULT_OPERATION_TYPE


def active_sales_items():
    return SalesItem.query.filter(SalesItem.is_active.is_(True)).order_by(SalesItem.code, SalesItem.name).all()


def client_invoice_options():
    """فواتير/مستخلصات العملاء للربط في سند التحصيل، مع المتبقي من كل واحدة."""
    payments = ProgressPayment.query.filter(ProgressPayment.subcontractor_id.is_(None)).order_by(
        ProgressPayment.id.desc()
    ).all()
    collected = collected_by_invoice([p.id for p in payments])
    options = []
    for payment in payments:
        net = round(_num(payment.net_value), 2)
        paid = collected.get(payment.id, 0.0)
        options.append({
            "id": payment.id,
            "number": payment.document_number,
            "client": payment.project.client_name if payment.project else "",
            "project": payment.project.display_name if payment.project else "",
            "net": net,
            "collected": paid,
            "remaining": round(net - paid, 2),
        })
    return options
