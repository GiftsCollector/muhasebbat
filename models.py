from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import Numeric, TypeDecorator, UniqueConstraint
from sqlalchemy.orm import declared_attr
from werkzeug.security import check_password_hash, generate_password_hash
import json

db = SQLAlchemy()


class MoneyDec(Decimal):
    """Decimal that still accepts float operands used in older calculations."""

    def __new__(cls, value="0"):
        if isinstance(value, Decimal):
            value = str(value)
        elif isinstance(value, float):
            value = str(value)
        elif value is None or value == "":
            value = "0"
        try:
            return Decimal.__new__(cls, value)
        except (InvalidOperation, ValueError, TypeError):
            return Decimal.__new__(cls, "0")

    @classmethod
    def from_raw(cls, value):
        if isinstance(value, cls):
            return value
        if value is None or value == "":
            return cls("0")
        if isinstance(value, bool):
            return cls("0")
        try:
            if isinstance(value, float):
                return cls(str(value))
            return cls(str(value).strip())
        except (InvalidOperation, ValueError, TypeError, AttributeError):
            return cls("0")

    @staticmethod
    def _coerce(other):
        if isinstance(other, Decimal):
            return other
        if isinstance(other, float):
            return Decimal(str(other))
        if other is None or other == "":
            return Decimal("0")
        return Decimal(str(other))

    def __add__(self, other):
        return MoneyDec(Decimal(self) + self._coerce(other))

    def __radd__(self, other):
        return MoneyDec(self._coerce(other) + Decimal(self))

    def __sub__(self, other):
        return MoneyDec(Decimal(self) - self._coerce(other))

    def __rsub__(self, other):
        return MoneyDec(self._coerce(other) - Decimal(self))

    def __mul__(self, other):
        return MoneyDec(Decimal(self) * self._coerce(other))

    def __rmul__(self, other):
        return MoneyDec(self._coerce(other) * Decimal(self))

    def __truediv__(self, other):
        return MoneyDec(Decimal(self) / self._coerce(other))

    def __rtruediv__(self, other):
        return MoneyDec(self._coerce(other) / Decimal(self))

    def __lt__(self, other):
        return Decimal(self) < self._coerce(other)

    def __le__(self, other):
        return Decimal(self) <= self._coerce(other)

    def __gt__(self, other):
        return Decimal(self) > self._coerce(other)

    def __ge__(self, other):
        return Decimal(self) >= self._coerce(other)

    def __eq__(self, other):
        if other is None:
            return False
        try:
            return Decimal(self) == self._coerce(other)
        except Exception:
            return False

    def __neg__(self):
        return MoneyDec(Decimal.__neg__(self))

    def __abs__(self):
        return MoneyDec(Decimal.__abs__(self))

    def __round__(self, ndigits=None):
        if ndigits is None:
            return int(Decimal.to_integral_value(self, rounding=ROUND_HALF_UP))
        quant = Decimal("1").scaleb(-int(ndigits))
        return MoneyDec(self.quantize(quant, rounding=ROUND_HALF_UP))


class Money(TypeDecorator):
    """NUMERIC on PostgreSQL, text on SQLite so Decimal is not coerced back to float."""

    impl = Numeric(18, 4)
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "sqlite":
            return dialect.type_descriptor(db.String(40))
        return dialect.type_descriptor(Numeric(18, 4))

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        quantized = MoneyDec.from_raw(value).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        if dialect.name == "sqlite":
            return format(quantized, "f")
        return quantized

    def process_result_value(self, value, dialect):
        if value is None or value == "":
            return MoneyDec("0")
        return MoneyDec.from_raw(value)



ROLE_ADMIN = "admin"
ROLE_ACCOUNTANT = "accountant"
ROLE_PROJECT_MANAGER = "project_manager"
ROLE_DATA_ENTRY = "data_entry"

ROLE_LABELS = {
    ROLE_ADMIN: "مدير نظام",
    ROLE_ACCOUNTANT: "محاسب",
    ROLE_PROJECT_MANAGER: "مدير مشاريع",
    ROLE_DATA_ENTRY: "إدخال بيانات",
}


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False)
    full_name = db.Column(db.String(128), nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    # admin / accountant / project_manager / data_entry
    role = db.Column(db.String(32), nullable=False, default=ROLE_DATA_ENTRY)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    permissions_json = db.Column(db.Text, nullable=True)
    amount_pin_hash = db.Column(db.String(256), nullable=True)

    DEFAULT_AMOUNT_PIN = "123456"

    @property
    def role_label(self):
        return ROLE_LABELS.get(self.role, self.role)

    @property
    def has_custom_amount_pin(self):
        return bool((self.amount_pin_hash or "").strip())

    def set_password(self, raw_password):
        self.password_hash = generate_password_hash(raw_password)

    def check_password(self, raw_password):
        return check_password_hash(self.password_hash, raw_password)

    def check_amount_pin(self, pin):
        pin = (pin or "").strip()
        if not pin:
            return False
        if self.has_custom_amount_pin:
            return check_password_hash(self.amount_pin_hash, pin)
        return pin == self.DEFAULT_AMOUNT_PIN

    def set_amount_pin(self, pin):
        self.amount_pin_hash = generate_password_hash((pin or "").strip())


class ActorStamp:
    """يسجّل من أنشأ أو عدّل الحركة وتوقيتها باليوم والساعة."""

    created_at = db.Column(db.String(32), nullable=True)
    created_by_name = db.Column(db.String(128), nullable=True)
    updated_at = db.Column(db.String(32), nullable=True)
    updated_by_name = db.Column(db.String(128), nullable=True)

    @declared_attr
    def created_by_id(cls):
        return db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)

    @declared_attr
    def updated_by_id(cls):
        return db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)


class Project(ActorStamp, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(64), unique=True, nullable=False)
    project_name = db.Column(db.String(128), nullable=False, default="")
    client_name = db.Column(db.String(128), nullable=False)
    contract_value = db.Column(Money, default=0)
    start_date = db.Column(db.String(20), nullable=True)
    end_date = db.Column(db.String(20), nullable=True)
    contract_type = db.Column(db.String(64), nullable=False)
    # نسبة المصروفات الإدارية الخاصة بالكتيبة/الإدارة (SRS 3.1)
    admin_percentage = db.Column(Money, default=0)
    boq_items = db.relationship("BOQItem", backref="project", lazy=True)
    progress_payments = db.relationship("ProgressPayment", backref="project", lazy=True)
    cost_entries = db.relationship("CostEntry", backref="project", lazy=True)
    purchase_orders = db.relationship("PurchaseOrder", backref="project", lazy=True)
    inventory_entries = db.relationship("InventoryTransaction", backref="project", lazy=True)
    labor_entries = db.relationship("LaborEntry", backref="project", lazy=True)
    equipment_items = db.relationship("Equipment", backref="project", lazy=True)
    journal_entries = db.relationship("JournalEntry", backref="project", lazy=True)

    @property
    def display_name(self):
        if self.project_name:
            return f"{self.project_name} ({self.code})"
        return self.code


class BOQItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=False)
    name = db.Column(db.String(128), nullable=False)
    estimated_cost = db.Column(Money, default=0)
    quantity = db.Column(Money, default=0)
    execution_percentage = db.Column(Money, default=0)
    stage = db.Column(db.String(128), nullable=True)
    progress_items = db.relationship("ProgressPaymentItem", backref="boq_item", lazy=True)
    cost_entries = db.relationship("CostEntry", backref="boq_item", lazy=True)


class ChartOfAccount(ActorStamp, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(64), unique=True, nullable=False)
    name = db.Column(db.String(128), nullable=False)
    category = db.Column(db.String(64), nullable=False)
    opening_balance = db.Column(Money, default=0)
    term_days = db.Column(db.Integer, default=0)
    # تبويب المصروفات: مباشرة / غير مباشرة / إدارية (SRS 3.1)
    expense_class = db.Column(db.String(32), nullable=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=True)
    boq_item_id = db.Column(db.Integer, db.ForeignKey("boq_item.id"), nullable=True)
    stage = db.Column(db.String(128), nullable=True)

    project = db.relationship("Project", foreign_keys=[project_id], backref=db.backref("chart_accounts", lazy=True))
    boq_item = db.relationship("BOQItem", foreign_keys=[boq_item_id], backref=db.backref("chart_accounts", lazy=True))


class ProgressPayment(ActorStamp, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=False)
    subcontractor_id = db.Column(db.Integer, db.ForeignKey("subcontractor.id"), nullable=True)
    # بيانات رأس المستخلص (SRS 2.2)
    payment_number = db.Column(db.String(64), nullable=True)
    date = db.Column(db.String(20), nullable=True)
    period_start = db.Column(db.String(20), nullable=True)
    period_end = db.Column(db.String(20), nullable=True)
    # الاستقطاعات: تُدخل بنسبة مئوية أو بقيمة مباشرة
    retention_percentage = db.Column(Money, default=0)
    tax_percentage = db.Column(Money, default=0)
    discount_insurance = db.Column(Money, default=0)
    tax = db.Column(Money, default=0)
    penalties = db.Column(Money, default=0)
    other_deductions = db.Column(Money, default=0)
    discount_percentage = db.Column(Money, default=0)
    discount_value = db.Column(Money, default=0)
    # مجموع الدفعات النقدية المنصرفة تحت الحساب والمخصومة في هذا المستخلص
    advance_deduction = db.Column(Money, default=0)
    total_value = db.Column(Money, default=0)
    net_value = db.Column(Money, default=0)
    notes = db.Column(db.Text, nullable=True)
    items = db.relationship("ProgressPaymentItem", backref="payment", lazy=True)

    @property
    def deductions_total(self):
        return (
            (self.discount_insurance or 0)
            + (self.tax or 0)
            + (self.penalties or 0)
            + (self.other_deductions or 0)
            + (self.discount_value or 0)
        )

    @property
    def document_number(self):
        return self.payment_number or f"PP-{self.id:06d}"


class ProgressPaymentItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    progress_payment_id = db.Column(db.Integer, db.ForeignKey("progress_payment.id"), nullable=False)
    boq_item_id = db.Column(db.Integer, db.ForeignKey("boq_item.id"), nullable=True)
    description = db.Column(db.String(128), nullable=True)
    # وحدة القياس وسعر الفئة المتفق عليه (SRS 2.2)
    unit = db.Column(db.String(32), nullable=True)
    unit_price = db.Column(Money, default=0)
    quantity = db.Column(Money, default=0)
    value = db.Column(Money, default=0)


class SubcontractorPayment(ActorStamp, db.Model):
    """الدفعات النقدية/البنكية المنصرفة لمقاول الباطن تحت الحساب (SRS 2.2)."""

    id = db.Column(db.Integer, primary_key=True)
    subcontractor_id = db.Column(db.Integer, db.ForeignKey("subcontractor.id"), nullable=False)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=True)
    progress_payment_id = db.Column(db.Integer, db.ForeignKey("progress_payment.id"), nullable=True)
    date = db.Column(db.String(20), nullable=True)
    amount = db.Column(Money, default=0)
    payment_kind = db.Column(db.String(32), nullable=False, default="تحت الحساب")
    payment_method = db.Column(db.String(32), nullable=False, default="نقدي")
    treasury_account_id = db.Column(db.Integer, db.ForeignKey("chart_of_account.id"), nullable=True)
    reference = db.Column(db.String(128), nullable=True)
    notes = db.Column(db.Text, nullable=True)

    subcontractor = db.relationship("Subcontractor", backref=db.backref("advance_payments", lazy=True))
    project = db.relationship("Project", foreign_keys=[project_id])
    progress_payment = db.relationship("ProgressPayment", backref=db.backref("linked_payments", lazy=True))
    treasury_account = db.relationship("ChartOfAccount", foreign_keys=[treasury_account_id])

    @property
    def is_settlement(self):
        return (self.payment_kind or "") == "صرف مستخلص"

    @property
    def kind_label(self):
        if self.is_settlement:
            return "صرف مستخلص"
        return "تحت الحساب"


class SubcontractorRetentionRelease(ActorStamp, db.Model):
    """إفراج ضمان محتجز لمقاول باطن: مدين التأمينات / دائن حساب المقاول."""

    id = db.Column(db.Integer, primary_key=True)
    subcontractor_id = db.Column(db.Integer, db.ForeignKey("subcontractor.id"), nullable=False)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=True)
    date = db.Column(db.String(20), nullable=True)
    amount = db.Column(Money, default=0)
    reference = db.Column(db.String(128), nullable=True)
    notes = db.Column(db.Text, nullable=True)

    subcontractor = db.relationship("Subcontractor", backref=db.backref("retention_releases", lazy=True))
    project = db.relationship("Project", foreign_keys=[project_id])


class CostEntry(ActorStamp, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=False)
    boq_item_id = db.Column(db.Integer, db.ForeignKey("boq_item.id"), nullable=True)
    cost_type = db.Column(db.String(64), nullable=False)
    amount = db.Column(Money, default=0)
    description = db.Column(db.String(128), nullable=True)
    cost_center = db.Column(db.String(128), nullable=True)


class Subcontractor(ActorStamp, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False)
    # كود المقاول وتصنيف جهة التعامل (SRS 2.1)
    code = db.Column(db.String(64), nullable=True)
    entity_kind = db.Column(db.String(64), nullable=False, default="مقاول تنفيذي (مصنعية ومعدات/عمالة)")
    contact_info = db.Column(db.String(256), nullable=True)
    contract_value = db.Column(Money, default=0)
    discount_percentage = db.Column(Money, default=0)
    # نسب الاستقطاع الافتراضية المتفق عليها مع المقاول
    retention_percentage = db.Column(Money, default=0)
    tax_percentage = db.Column(Money, default=0)
    notes = db.Column(db.Text, nullable=True)
    payments = db.relationship("ProgressPayment", backref="subcontractor", lazy=True)

    @property
    def display_code(self):
        return self.code or f"SUB-{self.id:04d}"


class Supplier(ActorStamp, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False)
    code = db.Column(db.String(64), nullable=True)
    # تصنيف جهة التعامل (SRS 2.1)
    entity_kind = db.Column(db.String(64), nullable=False, default="مورد توريد مواد بناء")
    contact_info = db.Column(db.String(256), nullable=True)
    notes = db.Column(db.Text, nullable=True)
    purchase_orders = db.relationship("PurchaseOrder", backref="supplier", lazy=True)

    @property
    def display_code(self):
        return self.code or f"SUP-{self.id:04d}"


class PurchaseOrder(ActorStamp, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=False)
    supplier_id = db.Column(db.Integer, db.ForeignKey("supplier.id"), nullable=True)
    item_name = db.Column(db.String(128), nullable=True)
    warehouse_name = db.Column(db.String(128), nullable=True)
    warehouse_account_id = db.Column(db.Integer, db.ForeignKey("chart_of_account.id"), nullable=True)
    quantity = db.Column(Money, default=0)
    unit_price = db.Column(Money, default=0)
    discount = db.Column(Money, default=0)
    order_number = db.Column(db.String(64), nullable=True)
    invoice_number = db.Column(db.String(64), nullable=True)
    date = db.Column(db.String(20), nullable=True)
    status = db.Column(db.String(64), nullable=True)
    total_value = db.Column(Money, default=0)
    notes = db.Column(db.Text, nullable=True)
    warehouse_account = db.relationship("ChartOfAccount", foreign_keys=[warehouse_account_id])
    line_items = db.relationship("PurchaseOrderItem", backref="order", lazy=True, cascade="all, delete-orphan")

    @property
    def document_number(self):
        return self.order_number or self.invoice_number or f"PO-{self.id:06d}"

    @property
    def items_label(self):
        names = [item.item_name for item in (self.line_items or []) if item.item_name]
        if not names:
            return self.item_name or "-"
        if len(names) == 1:
            return names[0]
        return f"{names[0]} +{len(names) - 1}"

    @property
    def is_received(self):
        return (self.status or "") in {"مغلق", "مدفوع"}


class PurchaseOrderItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    purchase_order_id = db.Column(db.Integer, db.ForeignKey("purchase_order.id"), nullable=False)
    item_name = db.Column(db.String(128), nullable=False)
    unit = db.Column(db.String(32), nullable=True)
    quantity = db.Column(Money, default=0)
    unit_price = db.Column(Money, default=0)
    discount = db.Column(Money, default=0)
    value = db.Column(Money, default=0)


class InventoryTransaction(ActorStamp, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=False)
    supplier_id = db.Column(db.Integer, db.ForeignKey("supplier.id"), nullable=True)
    warehouse_name = db.Column(db.String(128), nullable=False)
    warehouse_account_id = db.Column(db.Integer, db.ForeignKey("chart_of_account.id"), nullable=True)
    destination_warehouse = db.Column(db.String(128), nullable=True)
    destination_account_id = db.Column(db.Integer, db.ForeignKey("chart_of_account.id"), nullable=True)
    material_name = db.Column(db.String(128), nullable=False)
    recipient_name = db.Column(db.String(128), nullable=True)
    storekeeper_name = db.Column(db.String(128), nullable=True)
    unit = db.Column(db.String(32), nullable=True)
    quantity = db.Column(Money, default=0)
    unit_cost = db.Column(Money, default=0)
    transaction_type = db.Column(db.String(64), nullable=False)
    date = db.Column(db.String(20), nullable=True)
    notes = db.Column(db.Text, nullable=True)

    supplier = db.relationship("Supplier", foreign_keys=[supplier_id])
    warehouse_account = db.relationship("ChartOfAccount", foreign_keys=[warehouse_account_id])
    destination_account = db.relationship("ChartOfAccount", foreign_keys=[destination_account_id])

    @property
    def is_auto_from_po(self):
        return "PO-AUTO:" in (self.notes or "")


class LaborEntry(ActorStamp, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=False)
    date = db.Column(db.String(20), nullable=True)
    description = db.Column(db.String(128), nullable=False)
    hours = db.Column(Money, default=0)
    amount = db.Column(Money, default=0)
    advances = db.Column(Money, default=0)
    deductions = db.Column(Money, default=0)


class Equipment(ActorStamp, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False)
    purchase_cost = db.Column(Money, default=0)
    operating_cost = db.Column(Money, default=0)
    maintenance = db.Column(Money, default=0)
    hours_used = db.Column(Money, default=0)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=True)


class CustodySettlement(ActorStamp, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.String(20), nullable=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=True)
    entity_type = db.Column(db.String(32), nullable=False)  # سائق / مندوب / معدة
    entity_name = db.Column(db.String(128), nullable=True)
    expense_item = db.Column(db.String(128), nullable=True)
    # طبيعة المصروف: مصروف نقلة / مصروف يومي / مصروف إداري (SRS 3.2)
    expense_nature = db.Column(db.String(32), nullable=True)
    voucher_type = db.Column(db.String(32), nullable=False)  # صرف / رد
    # صرف عهدة / تسوية عهدة / رد باقي عهدة / إعادة تغذية عهدة
    operation_type = db.Column(db.String(32), nullable=False, default="صرف عهدة")
    reference = db.Column(db.String(128), nullable=True)
    treasury_account_id = db.Column(db.Integer, db.ForeignKey("chart_of_account.id"), nullable=False)
    entity_account_id = db.Column(db.Integer, db.ForeignKey("chart_of_account.id"), nullable=False)
    amount = db.Column(Money, default=0)
    settlement_lines = db.Column(db.Text, nullable=True)
    notes = db.Column(db.Text, nullable=True)

    project = db.relationship("Project", foreign_keys=[project_id])
    treasury_account = db.relationship("ChartOfAccount", foreign_keys=[treasury_account_id])
    entity_account = db.relationship("ChartOfAccount", foreign_keys=[entity_account_id])


DRIVER_VEHICLE_TYPES = ["جرار", "قلاب", "سيارة", "خلاطة", "لودر"]
DRIVER_ACCOUNT_FIELDS = [
    ("commission", "حساب كمسيون النقل"),
    ("maintenance_days", "حساب ايام صيانة"),
    ("seller_fee", "حساب تباع"),
    ("daily_expense", "مصروف يومى"),
]
DRIVER_TRANSPORT_FIELDS = [
    ("gas", "جاز"),
    ("scales", "موازين"),
    ("cards", "كارتات"),
    ("towing", "تعتيق"),
    ("loading", "تحميل"),
    ("cart_man", "عرباوى"),
    ("stone", "حجر"),
]
DRIVER_SUNDRY_FIELDS = [
    ("filters", "فلاتر"),
    ("oils", "زيوت"),
    ("spare_parts", "قطع غيار"),
    ("supplies", "مستلزمات (مانديل-معطر-فوط)"),
    ("tips", "اكرميات"),
    ("fines", "مخالفات"),
    ("ac", "تكييف"),
    ("cash_fees", "رسوم كاش"),
    ("other_sundries", "اخرى"),
]
DRIVER_DEDUCTION_FIELDS = [
    ("custody", "العهدة"),
    ("discounts", "الخصومات"),
    ("advances", "السلف"),
    ("other_deductions", "اخرى"),
]
DRIVER_SHEET_AMOUNT_KEYS = [key for key, _label in (
    DRIVER_ACCOUNT_FIELDS + DRIVER_TRANSPORT_FIELDS + DRIVER_SUNDRY_FIELDS + DRIVER_DEDUCTION_FIELDS
)]


def _sum_sheet_fields(amounts, fields):
    total = MoneyDec("0")
    for key, _label in fields:
        try:
            total += MoneyDec.from_raw(amounts.get(key) or 0)
        except (TypeError, ValueError):
            continue
    return round(total, 2)


def driver_sheet_totals(amounts):
    amounts = amounts or {}
    driver_account_total = _sum_sheet_fields(amounts, DRIVER_ACCOUNT_FIELDS)
    transport_total = _sum_sheet_fields(amounts, DRIVER_TRANSPORT_FIELDS)
    sundries_total = _sum_sheet_fields(amounts, DRIVER_SUNDRY_FIELDS)
    deductions_total = _sum_sheet_fields(amounts, DRIVER_DEDUCTION_FIELDS)
    entitlements_total = round(driver_account_total + transport_total + sundries_total, 2)
    return {
        "driver_account_total": driver_account_total,
        "transport_total": transport_total,
        "sundries_total": sundries_total,
        "entitlements_total": entitlements_total,
        "deductions_total": deductions_total,
        "net_amount": round(entitlements_total - deductions_total, 2),
    }


class DriverCompensationEntry(ActorStamp, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.String(20), nullable=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=True)
    driver_name = db.Column(db.String(128), nullable=False)
    settlement_basis = db.Column(db.String(32), nullable=False, default="يومية")  # يومية / نقلة
    units = db.Column(Money, default=0)  # عدد الأيام أو عدد النقلات
    unit_rate = db.Column(Money, default=0)  # قيمة اليومية أو قيمة النقلة
    gross_amount = db.Column(Money, default=0)  # الاستحقاق
    paid_amount = db.Column(Money, default=0)  # المسدد
    treasury_account_id = db.Column(db.Integer, db.ForeignKey("chart_of_account.id"), nullable=True)
    reference = db.Column(db.String(128), nullable=True)
    notes = db.Column(db.Text, nullable=True)
    driver_code = db.Column(db.String(32), nullable=True)
    national_id = db.Column(db.String(32), nullable=True)
    phone = db.Column(db.String(32), nullable=True)
    vehicle_type = db.Column(db.String(64), nullable=True)
    tractor_number = db.Column(db.String(64), nullable=True)
    period_from = db.Column(db.String(20), nullable=True)
    period_to = db.Column(db.String(20), nullable=True)
    accountant_name = db.Column(db.String(128), nullable=True)
    accounts_officer = db.Column(db.String(128), nullable=True)
    details_json = db.Column(db.Text, nullable=True)
    driver_account_total = db.Column(Money, default=0)
    transport_total = db.Column(Money, default=0)
    sundries_total = db.Column(Money, default=0)
    entitlements_total = db.Column(Money, default=0)
    deductions_total = db.Column(Money, default=0)
    net_amount = db.Column(Money, default=0)

    project = db.relationship("Project", foreign_keys=[project_id])
    treasury_account = db.relationship("ChartOfAccount", foreign_keys=[treasury_account_id])

    def sheet_amounts(self):
        try:
            data = json.loads(self.details_json or "{}")
            return data if isinstance(data, dict) else {}
        except (TypeError, ValueError):
            return {}

    def amount_of(self, key):
        try:
            return MoneyDec.from_raw(self.sheet_amounts().get(key) or 0)
        except (TypeError, ValueError):
            return 0.0

    def recalculate(self):
        totals = driver_sheet_totals(self.sheet_amounts())
        self.driver_account_total = totals["driver_account_total"]
        self.transport_total = totals["transport_total"]
        self.sundries_total = totals["sundries_total"]
        self.entitlements_total = totals["entitlements_total"]
        self.deductions_total = totals["deductions_total"]
        self.net_amount = totals["net_amount"]
        if (self.settlement_basis or "") == "كشف" or self.details_json:
            self.settlement_basis = "كشف"
            self.units = 1
            self.unit_rate = self.entitlements_total
            self.gross_amount = self.entitlements_total
        return self


class SalesTrip(ActorStamp, db.Model):
    """نقلة مبيعات: البون والكميات المورّدة وسعر النقلة."""

    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.String(20), nullable=True)
    voucher_number = db.Column(db.String(64), nullable=True)
    trip_type = db.Column(db.String(64), nullable=True)
    origin = db.Column(db.String(128), nullable=True)
    destination = db.Column(db.String(128), nullable=True)
    distance_km = db.Column(Money, default=0)
    driver_name = db.Column(db.String(128), nullable=False)
    tractor_number = db.Column(db.String(64), nullable=True)
    trailer_number = db.Column(db.String(64), nullable=True)
    dump_cubage = db.Column(Money, default=0)
    trailer_cubage = db.Column(Money, default=0)
    cubage = db.Column(Money, default=0)
    discount = db.Column(Money, default=0)
    net_quantity = db.Column(Money, default=0)
    unit_price = db.Column(Money, default=0)
    total_amount = db.Column(Money, default=0)
    advances = db.Column(Money, default=0)
    remaining = db.Column(Money, default=0)
    period_label = db.Column(db.String(128), nullable=True)
    client_name = db.Column(db.String(128), nullable=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=True)
    notes = db.Column(db.Text, nullable=True)

    project = db.relationship("Project", foreign_keys=[project_id])

    @property
    def document_number(self):
        return (self.voucher_number or "").strip() or f"SALE-{self.id:06d}"

    def recalculate(self):
        dump = MoneyDec.from_raw(self.dump_cubage)
        trailer = MoneyDec.from_raw(self.trailer_cubage)
        if dump or trailer:
            cubage = dump + trailer
            self.cubage = cubage
        else:
            cubage = MoneyDec.from_raw(self.cubage)
        discount = MoneyDec.from_raw(self.discount)
        price = MoneyDec.from_raw(self.unit_price)
        advances = MoneyDec.from_raw(self.advances)
        net = max(cubage - discount, MoneyDec("0"))
        total = round(net * price, 2)
        self.net_quantity = round(net, 4)
        self.total_amount = total
        self.remaining = round(total - advances, 2)
        return self


class Estimation(ActorStamp, db.Model):
    """مقايسة العميل (SRS 4.1) مع نسب الخصم التجاري والعمولات/الإداريات."""

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(64), unique=True, nullable=False)
    date = db.Column(db.String(20), nullable=True)
    client_name = db.Column(db.String(128), nullable=False)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=True)
    project_name = db.Column(db.String(128), nullable=True)
    discount_percentage = db.Column(Money, default=0)
    admin_percentage = db.Column(Money, default=0)
    # إضافة / خصم — النسب الإدارية قد تكون مضافة أو مخصومة (SRS 4.1)
    admin_mode = db.Column(db.String(16), nullable=False, default="إضافة")
    status = db.Column(db.String(20), nullable=False, default="مسودة")  # مسودة / معتمدة / ملغاة
    total_value = db.Column(Money, default=0)
    discount_value = db.Column(Money, default=0)
    net_after_discount = db.Column(Money, default=0)
    admin_value = db.Column(Money, default=0)
    final_value = db.Column(Money, default=0)
    notes = db.Column(db.Text, nullable=True)

    project = db.relationship("Project", foreign_keys=[project_id], backref=db.backref("estimations", lazy=True))
    items = db.relationship("EstimationItem", backref="estimation", lazy=True, cascade="all, delete-orphan")

    @property
    def display_project(self):
        if self.project:
            return self.project.display_name
        return self.project_name or "-"


class EstimationItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    estimation_id = db.Column(db.Integer, db.ForeignKey("estimation.id"), nullable=False)
    description = db.Column(db.String(160), nullable=False)
    unit = db.Column(db.String(32), nullable=True)
    quantity = db.Column(Money, default=0)
    unit_price = db.Column(Money, default=0)
    # نسبة خصم على البند نفسه (خصم على بنود محددة - SRS 4.1)
    discount_percentage = db.Column(Money, default=0)
    total_before_discount = db.Column(Money, default=0)
    discount_value = db.Column(Money, default=0)


class JournalEntry(ActorStamp, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.String(20), nullable=True)
    # رقم القيد الذي يكتبه المستخدم لكل بند، وإن تُرك فارغًا يُعرض السريال التلقائي
    entry_number = db.Column(db.String(64), nullable=True)
    reference = db.Column(db.String(128), nullable=True)
    journal_name = db.Column(db.String(64), nullable=False, default="يومية عامة")
    branch = db.Column(db.String(128), nullable=True)
    stock_move = db.Column(db.String(128), nullable=True)
    status = db.Column(db.String(20), nullable=False, default="مسودة")
    description = db.Column(db.String(256), nullable=False)
    debit_account_id = db.Column(db.Integer, db.ForeignKey("chart_of_account.id"), nullable=False)
    credit_account_id = db.Column(db.Integer, db.ForeignKey("chart_of_account.id"), nullable=False)
    amount = db.Column(Money, default=0)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=True)
    cost_center = db.Column(db.String(128), nullable=True)

    debit_account = db.relationship("ChartOfAccount", foreign_keys=[debit_account_id])
    credit_account = db.relationship("ChartOfAccount", foreign_keys=[credit_account_id])

    AUTO_MARKERS = (
        "PP-AUTO:",
        "SPAY-AUTO:",
        "EST-AUTO:",
        "PO-AUTO:",
        "PO-JRN-AUTO:",
        "INV-AUTO:",
        "CUST-AUTO:",
        "DRV-WORK-AUTO:",
        "DRV-PAY-AUTO:",
        "REC-AUTO:",
        "PAY-AUTO:",
        "LAB-AUTO:",
        "EQP-AUTO:",
        "EQP-OP-AUTO:",
        "PAYROLL-AUTO:",
        "ESAL-AUTO:",
    )

    @property
    def display_number(self):
        return self.entry_number or f"JRN-{self.id:06d}"

    @property
    def is_auto(self):
        """القيود المولدة من المستندات تُدار من مصدرها ولا تُعدَّل يدويًا."""
        description = self.description or ""
        return any(marker in description for marker in self.AUTO_MARKERS)


class ClientReceipt(ActorStamp, db.Model):
    """تحصيل من عميل: مدين الخزنة/البنك — دائن حساب العميل."""

    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.String(20), nullable=True)
    client_name = db.Column(db.String(128), nullable=False)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=True)
    amount = db.Column(Money, default=0)
    payment_method = db.Column(db.String(32), nullable=False, default="نقدي")
    treasury_account_id = db.Column(db.Integer, db.ForeignKey("chart_of_account.id"), nullable=True)
    reference = db.Column(db.String(128), nullable=True)
    notes = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(20), nullable=False, default="مرحّل")

    project = db.relationship("Project", foreign_keys=[project_id])
    treasury_account = db.relationship("ChartOfAccount", foreign_keys=[treasury_account_id])

    @property
    def document_number(self):
        return self.reference or f"REC-{self.id:06d}"


class SupplierPayment(ActorStamp, db.Model):
    """سداد لمورد منفصل عن أمر الشراء: مدين المورد — دائن الخزنة/البنك."""

    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.String(20), nullable=True)
    supplier_id = db.Column(db.Integer, db.ForeignKey("supplier.id"), nullable=False)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=True)
    amount = db.Column(Money, default=0)
    payment_method = db.Column(db.String(32), nullable=False, default="نقدي")
    treasury_account_id = db.Column(db.Integer, db.ForeignKey("chart_of_account.id"), nullable=True)
    reference = db.Column(db.String(128), nullable=True)
    notes = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(20), nullable=False, default="مرحّل")

    supplier = db.relationship("Supplier", backref=db.backref("payments", lazy=True))
    project = db.relationship("Project", foreign_keys=[project_id])
    treasury_account = db.relationship("ChartOfAccount", foreign_keys=[treasury_account_id])

    @property
    def document_number(self):
        return self.reference or f"PAY-{self.id:06d}"


class Employee(ActorStamp, db.Model):
    """موظف في موديول الموارد البشرية، بحساب دائن مستقل عن الموردين."""

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False)
    code = db.Column(db.String(64), nullable=True)
    tag = db.Column(db.String(64), nullable=False, default="موظف")
    job_title = db.Column(db.String(128), nullable=True)
    hometown = db.Column(db.String(128), nullable=True)
    site = db.Column(db.String(128), nullable=True)
    basic_salary = db.Column(Money, default=0)
    contact_info = db.Column(db.String(256), nullable=True)
    notes = db.Column(db.Text, nullable=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)

    @property
    def display_code(self):
        return self.code or f"EMP-{self.id:04d}"

    @property
    def display_name(self):
        tag = (self.tag or "").strip()
        if tag:
            return f"{self.name} ({tag})"
        return self.name

    @property
    def payroll_job_title(self):
        return (self.job_title or self.tag or "").strip() or "موظف"

    @property
    def payroll_hometown(self):
        return (self.hometown or "").strip()

    @property
    def payroll_site(self):
        return (self.site or "").strip()


class EmployeeAttendance(ActorStamp, db.Model):
    """سجل يومي: حضور، انصراف، تأخير، استئذان، أنواع الإجازات."""

    __table_args__ = (UniqueConstraint("employee_id", "date", name="uq_attendance_employee_date"),)

    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey("employee.id"), nullable=False)
    date = db.Column(db.String(20), nullable=False)
    status = db.Column(db.String(32), nullable=False, default="حضور")
    check_in = db.Column(db.String(8), nullable=True)
    check_out = db.Column(db.String(8), nullable=True)
    delay_minutes = db.Column(Money, default=0)
    permission_hours = db.Column(Money, default=0)
    notes = db.Column(db.Text, nullable=True)
    record_status = db.Column(db.String(20), nullable=False, default="مرحل")

    employee = db.relationship("Employee", backref=db.backref("attendance_rows", lazy=True))


class PayrollSlip(ActorStamp, db.Model):
    """كشف مرتب شهري للموظف: الاستحقاق يحمّل مصروف المرتبات ويُثبت على حساب الموظف."""

    __table_args__ = (UniqueConstraint("employee_id", "period_month", name="uq_payroll_employee_month"),)

    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey("employee.id"), nullable=False)
    period_month = db.Column(db.String(7), nullable=False)
    date = db.Column(db.String(20), nullable=True)
    work_days = db.Column(Money, default=0)
    vacation_days = db.Column(Money, default=0)
    basic_salary = db.Column(Money, default=0)
    daily_rate = db.Column(Money, default=0)
    earned_salary = db.Column(Money, default=0)
    transport = db.Column(Money, default=0)
    overtime = db.Column(Money, default=0)
    incentives = db.Column(Money, default=0)
    delay_deduction = db.Column(Money, default=0)
    permission_deduction = db.Column(Money, default=0)
    other_deductions = db.Column(Money, default=0)
    daily_deduction_days = db.Column(Money, default=0)
    advances = db.Column(Money, default=0)
    notes = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(20), nullable=False, default="مرحل")

    employee = db.relationship("Employee", backref=db.backref("payroll_slips", lazy=True))

    @property
    def document_number(self):
        return f"PAYROLL-{self.period_month}-{self.id:05d}"

    @property
    def daily_rate_value(self):
        # يُحسب دائمًا من الراتب الأساسي الحالي على 30 يومًا، حتى لا تظهر بيانات قديمة
        # على القسيمة إذا تغيّر الراتب الأساسي بعد حفظ الكشف.
        basic = MoneyDec.from_raw(self.basic_salary)
        if basic:
            return round(basic / MoneyDec("30"), 2)
        return round(MoneyDec.from_raw(self.daily_rate), 2)

    @property
    def earned_salary_value(self):
        # نفس المبدأ: الأولوية دائمًا للحساب الفعلي من الراتب الأساسي وعدد أيام العمل
        # المسجّلة على الكشف، بدل الاعتماد على قيمة مخزّنة قد تصبح غير محدثة.
        basic = MoneyDec.from_raw(self.basic_salary)
        days = max(MoneyDec.from_raw(self.work_days), MoneyDec("0"))
        if basic:
            return round(basic * days / MoneyDec("30"), 2)
        rate = MoneyDec.from_raw(self.daily_rate)
        if rate:
            return round(rate * days, 2)
        return round(MoneyDec.from_raw(self.earned_salary), 2)

    @property
    def additions_total(self):
        return round(
            (self.earned_salary_value or 0)
            + (self.transport or 0)
            + (self.overtime or 0)
            + (self.incentives or 0),
            2,
        )

    @property
    def daily_deduction_amount(self):
        days = max(self.daily_deduction_days or 0, 0)
        return round(days * (self.daily_rate_value or 0), 2)

    @property
    def deductions_total(self):
        return round(
            (self.delay_deduction or 0)
            + (self.permission_deduction or 0)
            + (self.other_deductions or 0)
            + (self.daily_deduction_amount or 0)
            + (self.advances or 0),
            2,
        )

    @property
    def net_salary(self):
        return round(self.additions_total - self.deductions_total, 2)


class EmployeeSalaryPayment(ActorStamp, db.Model):
    """صرف مرتب من الخزنة: ينقص حساب الموظف ولا يمس مصروف المرتبات."""

    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey("employee.id"), nullable=False)
    date = db.Column(db.String(20), nullable=True)
    amount = db.Column(Money, default=0)
    payment_method = db.Column(db.String(32), nullable=False, default="نقدي")
    treasury_account_id = db.Column(db.Integer, db.ForeignKey("chart_of_account.id"), nullable=True)
    reference = db.Column(db.String(128), nullable=True)
    notes = db.Column(db.Text, nullable=True)

    employee = db.relationship("Employee", backref=db.backref("salary_payments", lazy=True))
    treasury_account = db.relationship("ChartOfAccount", foreign_keys=[treasury_account_id])

    @property
    def document_number(self):
        return self.reference or f"ESAL-{self.id:06d}"


class AccountingPeriod(db.Model):
    """فترة محاسبية. الفترة المغلقة تمنع إنشاء أو تعديل القيود المرحلة داخلها."""

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), nullable=False)
    from_date = db.Column(db.String(20), nullable=False)
    to_date = db.Column(db.String(20), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="مفتوحة")
    closed_at = db.Column(db.String(32), nullable=True)
    closed_by = db.Column(db.String(128), nullable=True)
    notes = db.Column(db.Text, nullable=True)


class DocumentAttachment(db.Model):
    """مرفق فاتورة/إيصال مرتبط بأي مستند تشغيلي."""

    id = db.Column(db.Integer, primary_key=True)
    entity_type = db.Column(db.String(64), nullable=False)
    entity_id = db.Column(db.Integer, nullable=False)
    original_name = db.Column(db.String(256), nullable=False)
    stored_name = db.Column(db.String(256), nullable=False)
    uploaded_at = db.Column(db.String(32), nullable=True)
    uploaded_by = db.Column(db.String(128), nullable=True)


class ActivityLog(db.Model):
    """سجل كل حركة على البرنامج: المستخدم، اليوم، الساعة، ونوع العملية."""

    __tablename__ = "activity_log"

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.String(32), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True, index=True)
    user_name = db.Column(db.String(128), nullable=False, default="")
    action = db.Column(db.String(32), nullable=False, default="إضافة")
    entity_type = db.Column(db.String(64), nullable=False, default="")
    entity_label = db.Column(db.String(64), nullable=False, default="")
    entity_id = db.Column(db.Integer, nullable=True)
    summary = db.Column(db.String(512), nullable=False, default="")

    user = db.relationship("User", foreign_keys=[user_id])


class AlertDismissal(db.Model):
    """إخفاء تنبيه حيّ لنفس الحالة. لو الحالة اتغيّرت بمفتاح جديد يظهر تاني."""

    __tablename__ = "alert_dismissal"
    __table_args__ = (UniqueConstraint("user_id", "alert_key", name="uq_alert_dismissal_user_key"),)

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    alert_key = db.Column(db.String(160), nullable=False)
    dismissed_at = db.Column(db.String(32), nullable=False)

    user = db.relationship("User", foreign_keys=[user_id])


class AppSetting(db.Model):
    __tablename__ = "app_setting"

    key = db.Column(db.String(64), primary_key=True)
    value = db.Column(db.Text, nullable=True)
