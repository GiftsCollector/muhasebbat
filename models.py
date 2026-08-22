from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash

db = SQLAlchemy()


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

    @property
    def role_label(self):
        return ROLE_LABELS.get(self.role, self.role)

    def set_password(self, raw_password):
        self.password_hash = generate_password_hash(raw_password)

    def check_password(self, raw_password):
        return check_password_hash(self.password_hash, raw_password)


class Project(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(64), unique=True, nullable=False)
    project_name = db.Column(db.String(128), nullable=False, default="")
    client_name = db.Column(db.String(128), nullable=False)
    contract_value = db.Column(db.Float, default=0)
    start_date = db.Column(db.String(20), nullable=True)
    end_date = db.Column(db.String(20), nullable=True)
    contract_type = db.Column(db.String(64), nullable=False)
    # نسبة المصروفات الإدارية الخاصة بالكتيبة/الإدارة (SRS 3.1)
    admin_percentage = db.Column(db.Float, default=0)
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
    estimated_cost = db.Column(db.Float, default=0)
    quantity = db.Column(db.Float, default=0)
    execution_percentage = db.Column(db.Float, default=0)
    stage = db.Column(db.String(128), nullable=True)
    progress_items = db.relationship("ProgressPaymentItem", backref="boq_item", lazy=True)
    cost_entries = db.relationship("CostEntry", backref="boq_item", lazy=True)


class ChartOfAccount(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(64), unique=True, nullable=False)
    name = db.Column(db.String(128), nullable=False)
    category = db.Column(db.String(64), nullable=False)
    opening_balance = db.Column(db.Float, default=0)
    term_days = db.Column(db.Integer, default=0)
    # تبويب المصروفات: مباشرة / غير مباشرة / إدارية (SRS 3.1)
    expense_class = db.Column(db.String(32), nullable=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=True)
    boq_item_id = db.Column(db.Integer, db.ForeignKey("boq_item.id"), nullable=True)
    stage = db.Column(db.String(128), nullable=True)

    project = db.relationship("Project", foreign_keys=[project_id], backref=db.backref("chart_accounts", lazy=True))
    boq_item = db.relationship("BOQItem", foreign_keys=[boq_item_id], backref=db.backref("chart_accounts", lazy=True))


class ProgressPayment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=False)
    subcontractor_id = db.Column(db.Integer, db.ForeignKey("subcontractor.id"), nullable=True)
    # بيانات رأس المستخلص (SRS 2.2)
    payment_number = db.Column(db.String(64), nullable=True)
    date = db.Column(db.String(20), nullable=True)
    period_start = db.Column(db.String(20), nullable=True)
    period_end = db.Column(db.String(20), nullable=True)
    # الاستقطاعات: تُدخل بنسبة مئوية أو بقيمة مباشرة
    retention_percentage = db.Column(db.Float, default=0)
    tax_percentage = db.Column(db.Float, default=0)
    discount_insurance = db.Column(db.Float, default=0)
    tax = db.Column(db.Float, default=0)
    penalties = db.Column(db.Float, default=0)
    other_deductions = db.Column(db.Float, default=0)
    # مجموع الدفعات النقدية المنصرفة تحت الحساب والمخصومة في هذا المستخلص
    advance_deduction = db.Column(db.Float, default=0)
    total_value = db.Column(db.Float, default=0)
    net_value = db.Column(db.Float, default=0)
    notes = db.Column(db.Text, nullable=True)
    items = db.relationship("ProgressPaymentItem", backref="payment", lazy=True)

    @property
    def deductions_total(self):
        return (
            (self.discount_insurance or 0)
            + (self.tax or 0)
            + (self.penalties or 0)
            + (self.other_deductions or 0)
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
    unit_price = db.Column(db.Float, default=0)
    quantity = db.Column(db.Float, default=0)
    value = db.Column(db.Float, default=0)


class SubcontractorPayment(db.Model):
    """الدفعات النقدية/البنكية المنصرفة لمقاول الباطن تحت الحساب (SRS 2.2)."""

    id = db.Column(db.Integer, primary_key=True)
    subcontractor_id = db.Column(db.Integer, db.ForeignKey("subcontractor.id"), nullable=False)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=True)
    progress_payment_id = db.Column(db.Integer, db.ForeignKey("progress_payment.id"), nullable=True)
    date = db.Column(db.String(20), nullable=True)
    amount = db.Column(db.Float, default=0)
    payment_method = db.Column(db.String(32), nullable=False, default="نقدي")
    treasury_account_id = db.Column(db.Integer, db.ForeignKey("chart_of_account.id"), nullable=True)
    reference = db.Column(db.String(128), nullable=True)
    notes = db.Column(db.Text, nullable=True)

    subcontractor = db.relationship("Subcontractor", backref=db.backref("advance_payments", lazy=True))
    project = db.relationship("Project", foreign_keys=[project_id])
    progress_payment = db.relationship("ProgressPayment", backref=db.backref("linked_payments", lazy=True))
    treasury_account = db.relationship("ChartOfAccount", foreign_keys=[treasury_account_id])


class CostEntry(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=False)
    boq_item_id = db.Column(db.Integer, db.ForeignKey("boq_item.id"), nullable=True)
    cost_type = db.Column(db.String(64), nullable=False)
    amount = db.Column(db.Float, default=0)
    description = db.Column(db.String(128), nullable=True)
    cost_center = db.Column(db.String(128), nullable=True)


class Subcontractor(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False)
    # كود المقاول وتصنيف جهة التعامل (SRS 2.1)
    code = db.Column(db.String(64), nullable=True)
    entity_kind = db.Column(db.String(64), nullable=False, default="مقاول تنفيذي (مصنعية ومعدات/عمالة)")
    contact_info = db.Column(db.String(256), nullable=True)
    contract_value = db.Column(db.Float, default=0)
    discount_percentage = db.Column(db.Float, default=0)
    # نسب الاستقطاع الافتراضية المتفق عليها مع المقاول
    retention_percentage = db.Column(db.Float, default=0)
    tax_percentage = db.Column(db.Float, default=0)
    notes = db.Column(db.Text, nullable=True)
    payments = db.relationship("ProgressPayment", backref="subcontractor", lazy=True)

    @property
    def display_code(self):
        return self.code or f"SUB-{self.id:04d}"


class Supplier(db.Model):
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


class PurchaseOrder(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=False)
    supplier_id = db.Column(db.Integer, db.ForeignKey("supplier.id"), nullable=True)
    item_name = db.Column(db.String(128), nullable=True)
    warehouse_name = db.Column(db.String(128), nullable=True)
    quantity = db.Column(db.Float, default=0)
    unit_price = db.Column(db.Float, default=0)
    discount = db.Column(db.Float, default=0)
    order_number = db.Column(db.String(64), nullable=True)
    invoice_number = db.Column(db.String(64), nullable=True)
    date = db.Column(db.String(20), nullable=True)
    status = db.Column(db.String(64), nullable=True)
    total_value = db.Column(db.Float, default=0)
    notes = db.Column(db.Text, nullable=True)


class InventoryTransaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=False)
    supplier_id = db.Column(db.Integer, db.ForeignKey("supplier.id"), nullable=True)
    warehouse_name = db.Column(db.String(128), nullable=False)
    destination_warehouse = db.Column(db.String(128), nullable=True)
    material_name = db.Column(db.String(128), nullable=False)
    quantity = db.Column(db.Float, default=0)
    unit_cost = db.Column(db.Float, default=0)
    transaction_type = db.Column(db.String(64), nullable=False)
    date = db.Column(db.String(20), nullable=True)
    notes = db.Column(db.Text, nullable=True)

    supplier = db.relationship("Supplier", foreign_keys=[supplier_id])


class LaborEntry(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=False)
    date = db.Column(db.String(20), nullable=True)
    description = db.Column(db.String(128), nullable=False)
    hours = db.Column(db.Float, default=0)
    amount = db.Column(db.Float, default=0)
    advances = db.Column(db.Float, default=0)
    deductions = db.Column(db.Float, default=0)


class Equipment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False)
    purchase_cost = db.Column(db.Float, default=0)
    operating_cost = db.Column(db.Float, default=0)
    maintenance = db.Column(db.Float, default=0)
    hours_used = db.Column(db.Float, default=0)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=True)


class CustodySettlement(db.Model):
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
    amount = db.Column(db.Float, default=0)
    settlement_lines = db.Column(db.Text, nullable=True)
    notes = db.Column(db.Text, nullable=True)

    project = db.relationship("Project", foreign_keys=[project_id])
    treasury_account = db.relationship("ChartOfAccount", foreign_keys=[treasury_account_id])
    entity_account = db.relationship("ChartOfAccount", foreign_keys=[entity_account_id])


class DriverCompensationEntry(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.String(20), nullable=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=True)
    driver_name = db.Column(db.String(128), nullable=False)
    settlement_basis = db.Column(db.String(32), nullable=False, default="يومية")  # يومية / نقلة
    units = db.Column(db.Float, default=0)  # عدد الأيام أو عدد النقلات
    unit_rate = db.Column(db.Float, default=0)  # قيمة اليومية أو قيمة النقلة
    gross_amount = db.Column(db.Float, default=0)  # الاستحقاق
    paid_amount = db.Column(db.Float, default=0)  # المسدد
    treasury_account_id = db.Column(db.Integer, db.ForeignKey("chart_of_account.id"), nullable=True)
    reference = db.Column(db.String(128), nullable=True)
    notes = db.Column(db.Text, nullable=True)

    project = db.relationship("Project", foreign_keys=[project_id])
    treasury_account = db.relationship("ChartOfAccount", foreign_keys=[treasury_account_id])


class Estimation(db.Model):
    """مقايسة العميل (SRS 4.1) مع نسب الخصم التجاري والعمولات/الإداريات."""

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(64), unique=True, nullable=False)
    date = db.Column(db.String(20), nullable=True)
    client_name = db.Column(db.String(128), nullable=False)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=True)
    project_name = db.Column(db.String(128), nullable=True)
    discount_percentage = db.Column(db.Float, default=0)
    admin_percentage = db.Column(db.Float, default=0)
    # إضافة / خصم — النسب الإدارية قد تكون مضافة أو مخصومة (SRS 4.1)
    admin_mode = db.Column(db.String(16), nullable=False, default="إضافة")
    status = db.Column(db.String(20), nullable=False, default="مسودة")  # مسودة / معتمدة / ملغاة
    total_value = db.Column(db.Float, default=0)
    discount_value = db.Column(db.Float, default=0)
    net_after_discount = db.Column(db.Float, default=0)
    admin_value = db.Column(db.Float, default=0)
    final_value = db.Column(db.Float, default=0)
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
    quantity = db.Column(db.Float, default=0)
    unit_price = db.Column(db.Float, default=0)
    # نسبة خصم على البند نفسه (خصم على بنود محددة - SRS 4.1)
    discount_percentage = db.Column(db.Float, default=0)
    total_before_discount = db.Column(db.Float, default=0)
    discount_value = db.Column(db.Float, default=0)


class JournalEntry(db.Model):
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
    amount = db.Column(db.Float, default=0)
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
    )

    @property
    def display_number(self):
        return self.entry_number or f"JRN-{self.id:06d}"

    @property
    def is_auto(self):
        """القيود المولدة من المستندات تُدار من مصدرها ولا تُعدَّل يدويًا."""
        description = self.description or ""
        return any(marker in description for marker in self.AUTO_MARKERS)


class ClientReceipt(db.Model):
    """تحصيل من عميل: مدين الخزنة/البنك — دائن حساب العميل."""

    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.String(20), nullable=True)
    client_name = db.Column(db.String(128), nullable=False)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=True)
    amount = db.Column(db.Float, default=0)
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


class SupplierPayment(db.Model):
    """سداد لمورد منفصل عن أمر الشراء: مدين المورد — دائن الخزنة/البنك."""

    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.String(20), nullable=True)
    supplier_id = db.Column(db.Integer, db.ForeignKey("supplier.id"), nullable=False)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=True)
    amount = db.Column(db.Float, default=0)
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
