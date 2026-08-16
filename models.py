from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash

db = SQLAlchemy()


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False)
    full_name = db.Column(db.String(128), nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="user")
    is_active = db.Column(db.Boolean, nullable=False, default=True)

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
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=True)
    boq_item_id = db.Column(db.Integer, db.ForeignKey("boq_item.id"), nullable=True)
    stage = db.Column(db.String(128), nullable=True)

    project = db.relationship("Project", foreign_keys=[project_id], backref=db.backref("chart_accounts", lazy=True))
    boq_item = db.relationship("BOQItem", foreign_keys=[boq_item_id], backref=db.backref("chart_accounts", lazy=True))


class ProgressPayment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=False)
    subcontractor_id = db.Column(db.Integer, db.ForeignKey("subcontractor.id"), nullable=True)
    period_start = db.Column(db.String(20), nullable=True)
    period_end = db.Column(db.String(20), nullable=True)
    discount_insurance = db.Column(db.Float, default=0)
    tax = db.Column(db.Float, default=0)
    penalties = db.Column(db.Float, default=0)
    total_value = db.Column(db.Float, default=0)
    net_value = db.Column(db.Float, default=0)
    notes = db.Column(db.Text, nullable=True)
    items = db.relationship("ProgressPaymentItem", backref="payment", lazy=True)


class ProgressPaymentItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    progress_payment_id = db.Column(db.Integer, db.ForeignKey("progress_payment.id"), nullable=False)
    boq_item_id = db.Column(db.Integer, db.ForeignKey("boq_item.id"), nullable=True)
    description = db.Column(db.String(128), nullable=True)
    quantity = db.Column(db.Float, default=0)
    value = db.Column(db.Float, default=0)


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
    contract_value = db.Column(db.Float, default=0)
    discount_percentage = db.Column(db.Float, default=0)
    notes = db.Column(db.Text, nullable=True)
    payments = db.relationship("ProgressPayment", backref="subcontractor", lazy=True)


class Supplier(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False)
    contact_info = db.Column(db.String(256), nullable=True)
    notes = db.Column(db.Text, nullable=True)
    purchase_orders = db.relationship("PurchaseOrder", backref="supplier", lazy=True)


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
    entity_type = db.Column(db.String(32), nullable=False)  # سائق / معدة
    entity_name = db.Column(db.String(128), nullable=True)
    expense_item = db.Column(db.String(128), nullable=True)
    voucher_type = db.Column(db.String(32), nullable=False)  # صرف / رد
    operation_type = db.Column(db.String(32), nullable=False, default="صرف عهدة")  # صرف عهدة / تسوية عهدة / رد باقي عهدة
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


class JournalEntry(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.String(20), nullable=True)
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
