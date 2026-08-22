from collections import defaultdict, deque
from datetime import date, datetime
from flask import g
from sqlalchemy import inspect as sa_inspect, text
from models import (
    db, Project, ChartOfAccount, ProgressPayment, ProgressPaymentItem, CostEntry,
    Subcontractor, Supplier, PurchaseOrder, InventoryTransaction, LaborEntry,
    Equipment, JournalEntry, CustodySettlement, DriverCompensationEntry,
    SubcontractorPayment, Estimation, EstimationItem, ClientReceipt,
    SupplierPayment, AccountingPeriod, DocumentAttachment,
)

SQLITE_MIGRATIONS = {
    "project": {
        "project_name": "ALTER TABLE project ADD COLUMN project_name VARCHAR(128) NOT NULL DEFAULT ''",
        "admin_percentage": "ALTER TABLE project ADD COLUMN admin_percentage FLOAT NOT NULL DEFAULT 0",
    },
    "purchase_order": {
        "item_name": "ALTER TABLE purchase_order ADD COLUMN item_name VARCHAR(128)",
        "quantity": "ALTER TABLE purchase_order ADD COLUMN quantity FLOAT NOT NULL DEFAULT 0",
        "unit_price": "ALTER TABLE purchase_order ADD COLUMN unit_price FLOAT NOT NULL DEFAULT 0",
        "discount": "ALTER TABLE purchase_order ADD COLUMN discount FLOAT NOT NULL DEFAULT 0",
        "warehouse_name": "ALTER TABLE purchase_order ADD COLUMN warehouse_name VARCHAR(128)",
    },
    "inventory_transaction": {
        "supplier_id": "ALTER TABLE inventory_transaction ADD COLUMN supplier_id INTEGER",
    },
    "chart_of_account": {
        "opening_balance": "ALTER TABLE chart_of_account ADD COLUMN opening_balance FLOAT NOT NULL DEFAULT 0",
        "term_days": "ALTER TABLE chart_of_account ADD COLUMN term_days INTEGER NOT NULL DEFAULT 0",
        "expense_class": "ALTER TABLE chart_of_account ADD COLUMN expense_class VARCHAR(32)",
    },
    "journal_entry": {
        "entry_number": "ALTER TABLE journal_entry ADD COLUMN entry_number VARCHAR(64)",
        "reference": "ALTER TABLE journal_entry ADD COLUMN reference VARCHAR(128)",
        "journal_name": "ALTER TABLE journal_entry ADD COLUMN journal_name VARCHAR(64) NOT NULL DEFAULT 'يومية عامة'",
        "branch": "ALTER TABLE journal_entry ADD COLUMN branch VARCHAR(128)",
        "stock_move": "ALTER TABLE journal_entry ADD COLUMN stock_move VARCHAR(128)",
        "status": "ALTER TABLE journal_entry ADD COLUMN status VARCHAR(20) NOT NULL DEFAULT 'مسودة'",
        "cost_center": "ALTER TABLE journal_entry ADD COLUMN cost_center VARCHAR(128)",
    },
    "custody_settlement": {
        "expense_item": "ALTER TABLE custody_settlement ADD COLUMN expense_item VARCHAR(128)",
        "expense_nature": "ALTER TABLE custody_settlement ADD COLUMN expense_nature VARCHAR(32)",
        "operation_type": "ALTER TABLE custody_settlement ADD COLUMN operation_type VARCHAR(32) NOT NULL DEFAULT 'صرف عهدة'",
        "settlement_lines": "ALTER TABLE custody_settlement ADD COLUMN settlement_lines TEXT",
    },
    "subcontractor": {
        "code": "ALTER TABLE subcontractor ADD COLUMN code VARCHAR(64)",
        "entity_kind": "ALTER TABLE subcontractor ADD COLUMN entity_kind VARCHAR(64) NOT NULL DEFAULT 'مقاول تنفيذي (مصنعية ومعدات/عمالة)'",
        "contact_info": "ALTER TABLE subcontractor ADD COLUMN contact_info VARCHAR(256)",
        "retention_percentage": "ALTER TABLE subcontractor ADD COLUMN retention_percentage FLOAT NOT NULL DEFAULT 0",
        "tax_percentage": "ALTER TABLE subcontractor ADD COLUMN tax_percentage FLOAT NOT NULL DEFAULT 0",
    },
    "supplier": {
        "code": "ALTER TABLE supplier ADD COLUMN code VARCHAR(64)",
        "entity_kind": "ALTER TABLE supplier ADD COLUMN entity_kind VARCHAR(64) NOT NULL DEFAULT 'مورد توريد مواد بناء'",
    },
    "progress_payment": {
        "payment_number": "ALTER TABLE progress_payment ADD COLUMN payment_number VARCHAR(64)",
        "date": "ALTER TABLE progress_payment ADD COLUMN date VARCHAR(20)",
        "retention_percentage": "ALTER TABLE progress_payment ADD COLUMN retention_percentage FLOAT NOT NULL DEFAULT 0",
        "tax_percentage": "ALTER TABLE progress_payment ADD COLUMN tax_percentage FLOAT NOT NULL DEFAULT 0",
        "other_deductions": "ALTER TABLE progress_payment ADD COLUMN other_deductions FLOAT NOT NULL DEFAULT 0",
        "advance_deduction": "ALTER TABLE progress_payment ADD COLUMN advance_deduction FLOAT NOT NULL DEFAULT 0",
    },
    "progress_payment_item": {
        "unit": "ALTER TABLE progress_payment_item ADD COLUMN unit VARCHAR(32)",
        "unit_price": "ALTER TABLE progress_payment_item ADD COLUMN unit_price FLOAT NOT NULL DEFAULT 0",
    },
}


def run_schema_migrations():
    """إضافة الأعمدة الناقصة على SQLite وPostgreSQL دون لمس البيانات الموجودة."""
    inspector = sa_inspect(db.engine)
    existing_tables = set(inspector.get_table_names())
    dialect = db.engine.dialect.name
    with db.engine.begin() as connection:
        for table_name, columns in SQLITE_MIGRATIONS.items():
            if table_name not in existing_tables:
                continue
            table_columns = {column["name"] for column in inspector.get_columns(table_name)}
            for column_name, alter_sql in columns.items():
                if column_name in table_columns:
                    continue
                sql = alter_sql
                if dialect == "postgresql":
                    sql = sql.replace(" FLOAT ", " DOUBLE PRECISION ")
                connection.execute(text(sql))


def run_sqlite_migrations():
    run_schema_migrations()


# تصنيفات قديمة تم توحيد مسمياتها مع نص SRS 2.1
LEGACY_ENTITY_KIND_RENAMES = {
    "مقاول تنفيذي": "مقاول تنفيذي (مصنعية ومعدات/عمالة)",
    "مقاول باطن أعمال": "مقاول تنفيذي (مصنعية ومعدات/عمالة)",
    "مورد خدمات": "مورد تقديم وتمرير خدمات",
    "مورد مواد": "مورد توريد مواد بناء",
}


def normalize_entity_kinds():
    changed = False
    for model in (Subcontractor, Supplier):
        for old_value, new_value in LEGACY_ENTITY_KIND_RENAMES.items():
            for item in model.query.filter(model.entity_kind == old_value).all():
                item.entity_kind = new_value
                changed = True
    if changed:
        db.session.commit()


JOURNAL_OPTIONS = [
    "نقطة بيع",
    "تقييم المخزون",
    "أرباح وخسائر العملات",
    "أصول ثابتة",
    "متنوع",
    "أرصدة افتتاحية",
    "المخزون",
    "المستخلصات",
    "المقايسات",
    "المصاريف",
    "يومية عامة",
]

# تصنيف جهات التعامل (SRS 2.1)
ENTITY_KIND_OPTIONS = [
    "مقاول تنفيذي (مصنعية ومعدات/عمالة)",
    "مورد توريد مواد بناء",
    "مورد تقديم وتمرير خدمات",
]

# وحدات قياس الكميات للمستخلصات والمقايسات (SRS 2.2 / 4.1)
UNIT_OPTIONS = [
    "متر مسطح",
    "متر مكعب",
    "متر طولي",
    "بالعدد",
    "طن",
    "كيلو جرام",
    "لتر",
    "يومية",
    "نقلة",
    "مقطوعية",
]

# تبويب المصروفات (SRS 3.1)
EXPENSE_CLASS_DIRECT = "مباشرة"
EXPENSE_CLASS_INDIRECT = "غير مباشرة"
EXPENSE_CLASS_ADMIN = "إدارية"
EXPENSE_CLASS_OPTIONS = [EXPENSE_CLASS_DIRECT, EXPENSE_CLASS_INDIRECT, EXPENSE_CLASS_ADMIN]

# طبيعة مصروف العهدة (SRS 3.2)
CUSTODY_EXPENSE_NATURES = ["مصروف نقلة", "مصروف يومي", "مصروف إداري"]

CUSTODY_OPERATION_TYPES = ["صرف عهدة", "تسوية عهدة", "رد باقي عهدة", "إعادة تغذية عهدة"]
CUSTODY_DISBURSE_OPERATIONS = ("صرف عهدة", "إعادة تغذية عهدة")

CUSTODY_OWNER_TYPES = ["سائق", "مندوب", "معدة"]


def as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def ensure_chart_account(code, name, category, expense_class=None):
    existing_by_code = ChartOfAccount.query.filter_by(code=code).first()
    if existing_by_code:
        if expense_class and not existing_by_code.expense_class:
            existing_by_code.expense_class = expense_class
            return True
        return False
    existing_by_name = ChartOfAccount.query.filter_by(name=name, category=category).first()
    if existing_by_name:
        if expense_class and not existing_by_name.expense_class:
            existing_by_name.expense_class = expense_class
            return True
        return False
    db.session.add(ChartOfAccount(code=code, name=name, category=category, expense_class=expense_class))
    return True


# بنود مصروفات العهد مع تبويبها المحاسبي (SRS 3.1 / 3.2)
CUSTODY_EXPENSE_ACCOUNTS = [
    ("EXP-ELC", "كهرباء", EXPENSE_CLASS_INDIRECT),
    ("EXP-WTR", "مياه", EXPENSE_CLASS_INDIRECT),
    ("EXP-SOL", "سولار", EXPENSE_CLASS_DIRECT),
    ("EXP-CRT", "كارتة", EXPENSE_CLASS_DIRECT),
    ("EXP-ROD", "صيانة طريق", EXPENSE_CLASS_DIRECT),
    ("EXP-OFF", "ادوات مكتبية", EXPENSE_CLASS_ADMIN),
    ("EXP-HOM", "ادوات منزلية", EXPENSE_CLASS_ADMIN),
    ("EXP-SPR", "قطع غيار", EXPENSE_CLASS_DIRECT),
    ("EXP-FUR", "اثاث", EXPENSE_CLASS_ADMIN),
    ("EXP-TIP", "اكراميات", EXPENSE_CLASS_INDIRECT),
    ("EXP-FOD", "اكل وشرب", EXPENSE_CLASS_INDIRECT),
    ("EXP-REN", "ايجارات", EXPENSE_CLASS_INDIRECT),
    ("EXP-OTH", "مصروفات اخرى", EXPENSE_CLASS_INDIRECT),
]

# حسابات التشغيل الأساسية المطلوبة لصحة القيود المحاسبية
CORE_ACCOUNTING_ACCOUNTS = [
    # أصول
    ("INV-MAT", "مخزون مواد ومهمات", "الأصول", None),
    # التزامات واستقطاعات
    ("LIB-SUB", "مقاولو الباطن - أرصدة دائنة", "مقاولي الباطن", None),
    ("LIB-PAY", "موردون متنوعون - أرصدة دائنة", "الموردين", None),
    ("LIB-RET", "تأمينات وضمانات محتجزة", "الالتزامات", None),
    ("LIB-TAX", "ضرائب مستحقة (خصم وإضافة)", "الالتزامات", None),
    ("LIB-ACR", "مصروفات مستحقة", "الالتزامات", None),
    ("LIB-WAG", "أجور مستحقة", "الالتزامات", None),
    # إيرادات
    ("REV-WRK", "إيرادات أعمال ومقايسات", "الإيرادات", None),
    ("REV-PEN", "غرامات وخصومات محملة على الغير", "الإيرادات", None),
    ("REV-OTH", "خصومات واستقطاعات أخرى", "الإيرادات", None),
    # مصروفات مبوبة
    ("EXP-SUB", "مصروف أعمال مقاولي الباطن", "المصروفات", EXPENSE_CLASS_DIRECT),
    ("EXP-MAT", "خامات ومواد بناء", "المصروفات", EXPENSE_CLASS_DIRECT),
    ("EXP-LAB", "عمالة موقع", "المصروفات", EXPENSE_CLASS_DIRECT),
    ("EXP-TRN", "نقل مباشر", "المصروفات", EXPENSE_CLASS_DIRECT),
    ("EXP-EQP", "تشغيل وصيانة معدات", "المصروفات", EXPENSE_CLASS_DIRECT),
    ("EXP-DRV", "مصروفات السواقين", "المصروفات", EXPENSE_CLASS_DIRECT),
    ("EXP-SRV", "خدمات ومقاولات تمرير", "المصروفات", EXPENSE_CLASS_DIRECT),
    ("EXP-IND", "مصروفات موقع غير مباشرة", "المصروفات", EXPENSE_CLASS_INDIRECT),
    ("EXP-SUP", "إشراف هندسي وإداري بالموقع", "المصروفات", EXPENSE_CLASS_INDIRECT),
    ("EXP-ADM", "مصروفات إدارية (الكتيبة/الإدارة)", "المصروفات", EXPENSE_CLASS_ADMIN),
    ("EXP-PEN", "غرامات وجزاءات محملة على الشركة", "المصروفات", EXPENSE_CLASS_INDIRECT),
    # استقطاعات العميل على مستخلصاتنا (أصول محتجزة لدى الغير)
    ("RET-CLI", "تأمينات محتجزة لدى العملاء", "الأصول", None),
    ("TAX-WHT", "ضرائب مخصومة تحت الحساب", "الأصول", None),
]

def ensure_custody_expense_accounts():
    changed = False
    for code, name, expense_class in CUSTODY_EXPENSE_ACCOUNTS:
        changed = ensure_chart_account(code, name, "المصروفات", expense_class) or changed
    return changed


def ensure_core_accounting_accounts():
    changed = False
    for code, name, category, expense_class in CORE_ACCOUNTING_ACCOUNTS:
        changed = ensure_chart_account(code, name, category, expense_class) or changed
    return changed


def get_account_by_code(code):
    return ChartOfAccount.query.filter_by(code=code).first()


def get_next_prefixed_code(prefix, used_codes):
    index = 1
    while True:
        candidate = f"{prefix}-{index:04d}"
        if candidate not in used_codes:
            used_codes.add(candidate)
            return candidate
        index += 1


def sync_journal_related_accounts():
    changed = False
    used_codes = {account.code for account in ChartOfAccount.query.with_entities(ChartOfAccount.code).all()}

    # Parent accounts for core entities expected in journal debit/credit selectors.
    parent_accounts = [
        ("CAT-SUP", "الموردين", "الموردين"),
        ("CAT-CLI", "العملاء", "العملاء"),
        ("CAT-WHS", "المخازن", "المخازن"),
        ("CAT-EQP", "المعدات", "المعدات"),
        ("CAT-TRS", "الخزن الفرعية", "الخزن الفرعية"),
        ("CAT-DRV", "السواقين", "السواقين"),
        ("CAT-REP", "المناديب", "المناديب"),
        ("CAT-SUB", "مقاولي الباطن", "مقاولي الباطن"),
        ("CAT-PRJ", "المشاريع", "المشاريع"),
    ]
    for code, name, category in parent_accounts:
        changed = ensure_chart_account(code, name, category) or changed

    # حسابات تشغيلية أساسية لشركات المقاولات (خزنة رئيسية/موقع/عهد).
    treasury_defaults = [
        ("TRS-MAIN", "الخزنة الرئيسية", "الخزن الفرعية"),
        ("TRS-SITE", "خزنة الموقع", "الخزن الفرعية"),
        ("TRS-CUST", "العهد", "الخزن الفرعية"),
        ("TRS-BANK", "الحساب البنكي", "الخزن الفرعية"),
    ]
    for code, name, category in treasury_defaults:
        changed = ensure_chart_account(code, name, category) or changed

    changed = ensure_custody_expense_accounts() or changed
    changed = ensure_core_accounting_accounts() or changed

    for supplier in Supplier.query.order_by(Supplier.id).all():
        changed = ensure_chart_account(
            f"SUP-{supplier.id:04d}",
            f"مورد - {supplier.name}",
            "الموردين",
        ) or changed

    for subcontractor in Subcontractor.query.order_by(Subcontractor.id).all():
        changed = ensure_chart_account(
            f"SUB-{subcontractor.id:04d}",
            f"مقاول باطن - {subcontractor.name}",
            "مقاولي الباطن",
        ) or changed

    client_names = sorted({(project.client_name or "").strip() for project in Project.query.all() if (project.client_name or "").strip()})
    for client_name in client_names:
        if ChartOfAccount.query.filter_by(name=f"عميل - {client_name}", category="العملاء").first():
            continue
        changed = ensure_chart_account(get_next_prefixed_code("CLI", used_codes), f"عميل - {client_name}", "العملاء") or changed

    warehouse_names = sorted({
        (tx.warehouse_name or "").strip()
        for tx in InventoryTransaction.query.all()
        if (tx.warehouse_name or "").strip()
    }.union({
        (tx.destination_warehouse or "").strip()
        for tx in InventoryTransaction.query.all()
        if (tx.destination_warehouse or "").strip()
    }))
    for warehouse_name in warehouse_names:
        if ChartOfAccount.query.filter_by(name=f"مخزن - {warehouse_name}", category="المخازن").first():
            continue
        changed = ensure_chart_account(get_next_prefixed_code("WHS", used_codes), f"مخزن - {warehouse_name}", "المخازن") or changed

    labor_driver_names = {(entry.description or "").strip() for entry in LaborEntry.query.all() if (entry.description or "").strip()}
    compensation_driver_names = {
        (value[0] or "").strip()
        for value in DriverCompensationEntry.query.with_entities(DriverCompensationEntry.driver_name).all()
        if (value[0] or "").strip()
    }
    driver_names = sorted(labor_driver_names.union(compensation_driver_names))
    for driver_name in driver_names:
        if ChartOfAccount.query.filter_by(name=f"سائق - {driver_name}", category="السواقين").first():
            continue
        changed = ensure_chart_account(get_next_prefixed_code("DRV", used_codes), f"سائق - {driver_name}", "السواقين") or changed

    for equipment_item in Equipment.query.order_by(Equipment.id).all():
        changed = ensure_chart_account(
            f"EQP-{equipment_item.id:04d}",
            f"معدات - {equipment_item.name}",
            "المعدات",
        ) or changed

    for project in Project.query.order_by(Project.id).all():
        changed = ensure_chart_account(
            f"PRJ-{project.id:04d}",
            f"مشروع - {project.display_name}",
            "المشاريع",
        ) or changed

    if changed:
        db.session.commit()


def calculate_purchase_order_total(quantity, unit_price, discount):
    return max((quantity * unit_price) - discount, 0)


def get_material_names():
    names = set()
    for value in InventoryTransaction.query.with_entities(InventoryTransaction.material_name).all():
        name = (value[0] or "").strip()
        if name:
            names.add(name)
    for value in PurchaseOrder.query.with_entities(PurchaseOrder.item_name).all():
        name = (value[0] or "").strip()
        if name:
            names.add(name)
    return sorted(names)


def get_warehouse_names():
    names = set()
    for value in InventoryTransaction.query.with_entities(InventoryTransaction.warehouse_name).all():
        name = (value[0] or "").strip()
        if name:
            names.add(name)
    for value in InventoryTransaction.query.with_entities(InventoryTransaction.destination_warehouse).all():
        name = (value[0] or "").strip()
        if name:
            names.add(name)
    for value in PurchaseOrder.query.with_entities(PurchaseOrder.warehouse_name).all():
        name = (value[0] or "").strip()
        if name:
            names.add(name)
    return sorted(names)


def sync_purchase_order_to_inventory(order):
    marker = f"PO-AUTO:{order.id}"
    auto_tx = InventoryTransaction.query.filter(InventoryTransaction.notes.like(f"%{marker}%")).first()

    quantity = as_float(order.quantity)
    if quantity <= 0:
        if auto_tx:
            db.session.delete(auto_tx)
            db.session.commit()
        return

    effective_unit_cost = as_float(order.total_value) / quantity if quantity else as_float(order.unit_price)
    tx_payload = {
        "project_id": order.project_id,
        "supplier_id": order.supplier_id,
        "warehouse_name": (order.warehouse_name or "").strip() or "المخزن الرئيسي",
        "material_name": (order.item_name or "").strip() or "صنف غير مسمى",
        "quantity": quantity,
        "unit_cost": effective_unit_cost,
        "transaction_type": "إضافة",
        "date": order.date or date.today().isoformat(),
        "notes": f"{marker} - إضافة تلقائية من أمر شراء {order.order_number or order.invoice_number or order.id} - المورد {(order.supplier.name if order.supplier else 'بدون مورد')}",
    }

    if auto_tx:
        auto_tx.project_id = tx_payload["project_id"]
        auto_tx.supplier_id = tx_payload["supplier_id"]
        auto_tx.warehouse_name = tx_payload["warehouse_name"]
        auto_tx.material_name = tx_payload["material_name"]
        auto_tx.quantity = tx_payload["quantity"]
        auto_tx.unit_cost = tx_payload["unit_cost"]
        auto_tx.transaction_type = tx_payload["transaction_type"]
        auto_tx.date = tx_payload["date"]
        auto_tx.notes = tx_payload["notes"]
    else:
        db.session.add(InventoryTransaction(**tx_payload))

    db.session.commit()


def sync_purchase_order_journal(order):
    marker = f"PO-JRN-AUTO:{order.id}"
    auto_journal = JournalEntry.query.filter(JournalEntry.description.like(f"%{marker}%")).first()

    amount = as_float(order.total_value)
    if amount <= 0:
        if auto_journal:
            db.session.delete(auto_journal)
            db.session.commit()
        return

    sync_journal_related_accounts()
    # مدين: مخزون المواد (أصل) / دائن: حساب المورد نفسه أو حساب الموردين المتنوعين.
    debit_account = get_account_by_code("INV-MAT")
    credit_account = get_or_create_supplier_account(order.supplier) if order.supplier_id else None
    if not credit_account:
        credit_account = get_account_by_code("LIB-PAY")

    if not debit_account or not credit_account:
        return

    supplier_label = order.supplier.name if order.supplier else "بدون مورد"
    project_label = order.project.display_name if order.project else "-"
    payload = {
        "date": order.date or date.today().isoformat(),
        "reference": order.order_number or order.invoice_number or f"PO-{order.id:06d}",
        "journal_name": "المخزون",
        "status": "مرحل",
        "description": f"{marker} - أمر شراء {order.order_number or order.invoice_number or order.id} - المورد {supplier_label} - المشروع {project_label}",
        "debit_account_id": debit_account.id,
        "credit_account_id": credit_account.id,
        "amount": round(amount, 2),
        "project_id": order.project_id,
        "cost_center": f"مشروع - {project_label}" if order.project else None,
    }

    if auto_journal:
        for field, value in payload.items():
            setattr(auto_journal, field, value)
    else:
        db.session.add(JournalEntry(**payload))

    db.session.commit()


def is_purchase_order_inventory(transaction):
    return "PO-AUTO:" in (transaction.notes or "")


def sync_inventory_transaction_journal(transaction):
    """قيد حركة المخزون: الإضافة تزيد الأصل، والسحب يحمّل المصروف على المشروع.

    حركة الإضافة الآتية من أمر شراء لا تُقيَّد هنا حتى لا يتكرر مدين المخزون
    (أمر الشراء يرحّل مدين مخزون / دائن مورد).
    """
    sync_journal_related_accounts()
    marker = f"INV-AUTO:{transaction.id}"
    if is_purchase_order_inventory(transaction):
        replace_auto_journals(marker, [])
        return

    amount = round(as_float(transaction.quantity) * as_float(transaction.unit_cost), 2)
    inventory_account = get_account_by_code("INV-MAT")
    project = transaction.project
    payloads = []

    if amount > 0 and inventory_account:
        debit_account = None
        credit_account = None
        if transaction.transaction_type == "إضافة":
            debit_account = inventory_account
            credit_account = get_or_create_supplier_account(transaction.supplier) if transaction.supplier_id else None
            if not credit_account:
                credit_account = get_account_by_code("LIB-PAY")
        elif transaction.transaction_type == "سحب":
            debit_account = get_account_by_code("EXP-MAT")
            credit_account = inventory_account

        # التحويل بين المخازن لا يغير قيمة المخزون الكلية فلا يُنشأ له قيد.
        if debit_account and credit_account:
            payloads.append({
                "date": transaction.date or date.today().isoformat(),
                "reference": f"INV-{transaction.id:06d}",
                "journal_name": "تقييم المخزون",
                "stock_move": transaction.transaction_type,
                "status": "مرحل",
                "description": (
                    f"{marker} - حركة مخزون {transaction.transaction_type} "
                    f"للمادة {transaction.material_name} - مخزن {transaction.warehouse_name}"
                ),
                "debit_account_id": debit_account.id,
                "credit_account_id": credit_account.id,
                "amount": amount,
                "project_id": transaction.project_id,
                "cost_center": f"مشروع - {project.display_name}" if project else None,
            })

    replace_auto_journals(marker, payloads)


def normalize_journal_status(value):
    return "مرحل" if value == "مرحل" else "مسودة"


def format_grouped_number(value):
    number = as_float(value)
    if abs(number - round(number)) < 0.000001:
        formatted = f"{int(round(number)):,}"
        return formatted.replace(",", ".")

    whole, decimal = f"{number:,.2f}".split(".")
    return f"{whole.replace(',', '.')},{decimal}"


def build_account_balances(accounts=None, posted_only=True):
    """أرصدة الحسابات = الرصيد الافتتاحي + المدين - الدائن.

    القيود المسودة لا تؤثر على الأرصدة، فقط القيود المرحلة (مبدأ الترحيل المحاسبي).
    """
    account_rows = accounts or ChartOfAccount.query.order_by(ChartOfAccount.code, ChartOfAccount.name).all()
    balances = {account.id: as_float(getattr(account, "opening_balance", 0)) for account in account_rows}

    query = JournalEntry.query
    if posted_only:
        query = query.filter(JournalEntry.status == "مرحل")

    for entry in query.all():
        amount = as_float(entry.amount)
        if entry.debit_account_id in balances:
            balances[entry.debit_account_id] += amount
        if entry.credit_account_id in balances:
            balances[entry.credit_account_id] -= amount
    return balances


def get_treasury_balance(accounts=None, balances=None):
    account_rows = accounts or ChartOfAccount.query.order_by(ChartOfAccount.code, ChartOfAccount.name).all()
    account_balances = balances or build_account_balances(account_rows)
    main_account, sub_accounts = split_treasury_accounts(account_rows)
    if main_account:
        return get_main_treasury_rollup_balance(account_rows, account_balances)

    return sum(
        account_balances.get(account.id, 0)
        for account in sub_accounts
    )


def get_journal_entries_in_range(from_date=None, to_date=None, posted_only=True):
    query = JournalEntry.query
    if posted_only:
        query = query.filter(JournalEntry.status == "مرحل")
    if from_date:
        query = query.filter(JournalEntry.date >= from_date)
    if to_date:
        query = query.filter(JournalEntry.date <= to_date)
    return query.order_by(JournalEntry.date.asc(), JournalEntry.id.asc()).all()


def is_treasury_account(account):
    code = (account.code or "").strip().upper()
    name = (account.name or "")
    category = (account.category or "")
    if category == "المصروفات":
        return False
    if category == "الخزن الفرعية":
        return True
    if code == "CAT-TRS" or code.startswith("TRS-"):
        return True
    name_keywords = ("خزنة", "خزنه", "صندوق")
    return any(keyword in name for keyword in name_keywords)


def is_operational_treasury_account(account):
    if not is_treasury_account(account):
        return False
    code = (account.code or "").strip().upper()
    return not code.startswith("CAT-")


def split_treasury_accounts(accounts):
    operational_treasury_accounts = [account for account in accounts if is_operational_treasury_account(account)]
    main_account = next(
        (
            account
            for account in operational_treasury_accounts
            if (account.code or "").strip().upper() == "TRS-MAIN"
            or (account.name or "").strip() == "الخزنة الرئيسية"
        ),
        None,
    )
    sub_accounts = [account for account in operational_treasury_accounts if not main_account or account.id != main_account.id]
    return main_account, sub_accounts


def get_main_treasury_rollup_balance(accounts=None, balances=None):
    account_rows = accounts or ChartOfAccount.query.order_by(ChartOfAccount.code, ChartOfAccount.name).all()
    account_balances = balances or build_account_balances(account_rows)
    main_account, sub_accounts = split_treasury_accounts(account_rows)
    main_own_balance = account_balances.get(main_account.id, 0.0) if main_account else 0.0
    sub_total_balance = sum(account_balances.get(account.id, 0.0) for account in sub_accounts)
    return main_own_balance + sub_total_balance


def classify_balance_sheet_section(account):
    category = (account.category or "").strip()
    if category in {
        "الأصول",
        "المخازن",
        "المعدات",
        "معدات ثقيلة",
        "سيارات",
        "الخزن الفرعية",
        "العملاء",
        "المشاريع",
        "مواد",
        "السواقين",
        "المناديب",
    }:
        return "assets"
    if category in {"الالتزامات", "الموردين", "موردين", "مقاولي الباطن"}:
        return "liabilities"
    if category in {"حقوق الملكية", "رأس المال"}:
        return "equity"
    return "other"


def get_age_bucket(entry_date):
    if not entry_date:
        return "current"
    try:
        age_days = (date.today() - date.fromisoformat(entry_date)).days
    except ValueError:
        return "current"

    if age_days <= 30:
        return "current"
    if age_days <= 60:
        return "30"
    if age_days <= 90:
        return "60"
    return "90"


def get_custody_owner_type(account):
    """يحدد نوع صاحب العهدة من الحساب: سائق / مندوب / معدة."""
    code = (account.code or "").strip().upper()
    name = (account.name or "").strip()
    category = (account.category or "").strip()
    if code.startswith("DRV-") or "سائق" in name or category == "السواقين":
        return "سائق"
    if code.startswith("REP-") or "مندوب" in name or category == "المناديب":
        return "مندوب"
    if code.startswith("EQP-") or "معدة" in name or "معدات" in name or category == "المعدات":
        return "معدة"
    return ""


def get_custody_entity_accounts(accounts):
    selected = []
    seen_ids = set()

    for account in accounts:
        code = (account.code or "").strip().upper()
        if code.startswith("CAT-"):
            continue
        if get_custody_owner_type(account) and account.id not in seen_ids:
            selected.append(account)
            seen_ids.add(account.id)

    selected.sort(key=lambda item: ((item.category or ""), (item.code or ""), (item.name or "")))
    return selected


def get_or_create_custody_expense_account(expense_item):
    item_name = (expense_item or "").strip()
    if not item_name:
        return None

    account_name = f"مصروف عهدة - {item_name}"
    existing = ChartOfAccount.query.filter_by(name=account_name, category="المصروفات").first()
    if existing:
        return existing

    used_codes = {account.code for account in ChartOfAccount.query.with_entities(ChartOfAccount.code).all()}
    account_code = get_next_prefixed_code("EXP-CUST", used_codes)
    account = ChartOfAccount(code=account_code, name=account_name, category="المصروفات")
    db.session.add(account)
    db.session.flush()
    return account


def get_or_create_driver_account(driver_name):
    clean_name = (driver_name or "").strip()
    if not clean_name:
        return None
    account_name = f"سائق - {clean_name}"
    existing = ChartOfAccount.query.filter_by(name=account_name, category="السواقين").first()
    if existing:
        return existing

    used_codes = {account.code for account in ChartOfAccount.query.with_entities(ChartOfAccount.code).all()}
    account = ChartOfAccount(
        code=get_next_prefixed_code("DRV", used_codes),
        name=account_name,
        category="السواقين",
    )
    db.session.add(account)
    db.session.flush()
    return account


def get_or_create_representative_account(representative_name):
    clean_name = (representative_name or "").strip()
    if not clean_name:
        return None
    account_name = f"مندوب - {clean_name}"
    existing = ChartOfAccount.query.filter_by(name=account_name, category="المناديب").first()
    if existing:
        return existing

    used_codes = {account.code for account in ChartOfAccount.query.with_entities(ChartOfAccount.code).all()}
    account = ChartOfAccount(
        code=get_next_prefixed_code("REP", used_codes),
        name=account_name,
        category="المناديب",
    )
    db.session.add(account)
    db.session.flush()
    return account


def get_or_create_custody_owner_account(owner_type, owner_name):
    clean_type = (owner_type or "").strip()
    if clean_type == "مندوب":
        return get_or_create_representative_account(owner_name)
    if clean_type == "معدة":
        clean_name = (owner_name or "").strip()
        if not clean_name:
            return None
        account_name = f"معدات - {clean_name}"
        existing = ChartOfAccount.query.filter_by(name=account_name, category="المعدات").first()
        if existing:
            return existing
        used_codes = {account.code for account in ChartOfAccount.query.with_entities(ChartOfAccount.code).all()}
        account = ChartOfAccount(
            code=get_next_prefixed_code("EQP", used_codes),
            name=account_name,
            category="المعدات",
        )
        db.session.add(account)
        db.session.flush()
        return account
    return get_or_create_driver_account(owner_name)


def get_or_create_subcontractor_account(subcontractor):
    if not subcontractor:
        return None
    code = f"SUB-{subcontractor.id:04d}"
    account = get_account_by_code(code)
    if account:
        return account
    account = ChartOfAccount(
        code=code,
        name=f"مقاول باطن - {subcontractor.name}",
        category="مقاولي الباطن",
    )
    db.session.add(account)
    db.session.flush()
    return account


def get_or_create_supplier_account(supplier):
    if not supplier:
        return None
    code = f"SUP-{supplier.id:04d}"
    account = get_account_by_code(code)
    if account:
        return account
    account = ChartOfAccount(
        code=code,
        name=f"مورد - {supplier.name}",
        category="الموردين",
    )
    db.session.add(account)
    db.session.flush()
    return account


def normalize_client_name(value):
    return " ".join((value or "").split())


def canonical_client_name(value, known_names=None):
    """يعيد الاسم المسجّل إن وُجد بنفس الأحرف بعد توحيد المسافات."""
    clean = normalize_client_name(value)
    if not clean:
        return ""
    names = known_names if known_names is not None else list_client_names()
    clean_folded = clean.casefold()
    for name in names:
        if normalize_client_name(name).casefold() == clean_folded:
            return name
    return clean


def resolve_client_name(form):
    """يفضّل حقل العميل الجديد إن وُجد، وإلا الاختيار من القائمة، مع منع التكرار الإملائي."""
    return canonical_client_name(
        form.get("client_name_new") or form.get("client_name")
    )


def get_or_create_client_account(client_name):
    clean_name = canonical_client_name(client_name)
    if not clean_name:
        return None
    wanted = clean_name.casefold()
    for account in ChartOfAccount.query.filter_by(category="العملاء").all():
        suffix = account.name or ""
        if suffix.startswith("عميل - "):
            suffix = suffix[7:]
        if normalize_client_name(suffix).casefold() == wanted:
            return account
    used_codes = {account.code for account in ChartOfAccount.query.with_entities(ChartOfAccount.code).all()}
    account = ChartOfAccount(
        code=get_next_prefixed_code("CLI", used_codes),
        name=f"عميل - {clean_name}",
        category="العملاء",
    )
    db.session.add(account)
    db.session.flush()
    return account


def get_or_create_project_account(project):
    if not project:
        return None
    code = f"PRJ-{project.id:04d}"
    account = get_account_by_code(code)
    if account:
        return account
    account = ChartOfAccount(
        code=code,
        name=f"مشروع - {project.display_name}",
        category="المشاريع",
    )
    db.session.add(account)
    db.session.flush()
    return account


def is_auto_journal_entry(entry):
    """القيود التلقائية تُدار من مستنداتها (مستخلص/مقايسة/عهدة) ولا تُعدَّل يدويًا."""
    return bool(entry) and entry.is_auto


def find_closed_period(entry_date):
    """يعيد الفترة المغلقة التي يقع فيها التاريخ، أو None."""
    if not entry_date:
        return None
    for period in AccountingPeriod.query.filter_by(status="مغلقة").all():
        if (period.from_date or "") <= entry_date <= (period.to_date or ""):
            return period
    return None


class PeriodClosedError(Exception):
    def __init__(self, period):
        self.period = period
        super().__init__(
            f"الفترة المحاسبية «{period.name}» من {period.from_date} إلى {period.to_date} مغلقة"
        )


def assert_period_open(entry_date):
    period = find_closed_period(entry_date)
    if period:
        raise PeriodClosedError(period)


def replace_auto_journals(marker, payloads):
    """يحذف القيود التلقائية المرتبطة بالمستند ثم يعيد إنشاءها (مزامنة idempotent)."""
    existing = JournalEntry.query.filter(JournalEntry.description.like(f"%{marker}%")).all()
    dates_to_check = [item.date for item in existing if item.date]
    dates_to_check.extend(payload.get("date") for payload in payloads if payload.get("date"))
    for entry_date in dates_to_check:
        assert_period_open(entry_date)

    for journal in existing:
        db.session.delete(journal)

    for payload in payloads:
        if as_float(payload.get("amount")) <= 0:
            continue
        if not payload.get("debit_account_id") or not payload.get("credit_account_id"):
            continue
        if payload["debit_account_id"] == payload["credit_account_id"]:
            continue
        db.session.add(JournalEntry(**payload))

    db.session.commit()


def get_or_create_drivers_expense_account():
    account = ChartOfAccount.query.filter_by(code="EXP-DRV").first()
    if account:
        return account

    ensure_chart_account("EXP-DRV", "مصروفات السواقين", "المصروفات")
    account = ChartOfAccount.query.filter_by(code="EXP-DRV").first()
    if account:
        return account

    fallback = ChartOfAccount(code="EXP-DRV", name="مصروفات السواقين", category="المصروفات")
    db.session.add(fallback)
    db.session.flush()
    return fallback


def sync_driver_compensation_journals(entry):
    work_marker = f"DRV-WORK-AUTO:{entry.id}"
    pay_marker = f"DRV-PAY-AUTO:{entry.id}"
    work_journal = JournalEntry.query.filter(JournalEntry.description.like(f"%{work_marker}%")).first()
    pay_journal = JournalEntry.query.filter(JournalEntry.description.like(f"%{pay_marker}%")).first()

    gross_amount = as_float(entry.gross_amount)
    paid_amount = as_float(entry.paid_amount)
    driver_account = get_or_create_driver_account(entry.driver_name)
    expense_account = get_or_create_drivers_expense_account()

    if gross_amount <= 0 or not driver_account or not expense_account:
        if work_journal:
            db.session.delete(work_journal)
    else:
        work_payload = {
            "date": entry.date or date.today().isoformat(),
            "reference": entry.reference or f"DRV-W-{entry.id:06d}",
            "journal_name": "متنوع",
            "status": "مرحل",
            "description": (
                f"{work_marker} - استحقاق سائق {entry.driver_name} "
                f"({entry.settlement_basis} × {as_float(entry.units):g} × {as_float(entry.unit_rate):g})"
            ),
            "debit_account_id": expense_account.id,
            "credit_account_id": driver_account.id,
            "amount": gross_amount,
            "project_id": entry.project_id,
        }
        if work_journal:
            work_journal.date = work_payload["date"]
            work_journal.reference = work_payload["reference"]
            work_journal.journal_name = work_payload["journal_name"]
            work_journal.status = work_payload["status"]
            work_journal.description = work_payload["description"]
            work_journal.debit_account_id = work_payload["debit_account_id"]
            work_journal.credit_account_id = work_payload["credit_account_id"]
            work_journal.amount = work_payload["amount"]
            work_journal.project_id = work_payload["project_id"]
        else:
            db.session.add(JournalEntry(**work_payload))

    if paid_amount <= 0 or not entry.treasury_account_id or not driver_account:
        if pay_journal:
            db.session.delete(pay_journal)
    else:
        pay_payload = {
            "date": entry.date or date.today().isoformat(),
            "reference": entry.reference or f"DRV-P-{entry.id:06d}",
            "journal_name": "متنوع",
            "status": "مرحل",
            "description": f"{pay_marker} - سداد سائق {entry.driver_name}",
            "debit_account_id": driver_account.id,
            "credit_account_id": entry.treasury_account_id,
            "amount": paid_amount,
            "project_id": entry.project_id,
        }
        if pay_journal:
            pay_journal.date = pay_payload["date"]
            pay_journal.reference = pay_payload["reference"]
            pay_journal.journal_name = pay_payload["journal_name"]
            pay_journal.status = pay_payload["status"]
            pay_journal.description = pay_payload["description"]
            pay_journal.debit_account_id = pay_payload["debit_account_id"]
            pay_journal.credit_account_id = pay_payload["credit_account_id"]
            pay_journal.amount = pay_payload["amount"]
            pay_journal.project_id = pay_payload["project_id"]
        else:
            db.session.add(JournalEntry(**pay_payload))

    db.session.commit()


def get_custody_operation_type(settlement):
    operation_type = (getattr(settlement, "operation_type", "") or "").strip()
    if operation_type:
        return operation_type
    return ((settlement.voucher_type or "") + " عهدة").strip()


def sync_custody_settlement_journal(settlement):
    """قيود العهد (SRS 3.2 / 3.3).

    صرف عهدة / إعادة تغذية: مدين حساب صاحب العهدة (سلفة) / دائن الخزنة.
    تسوية عهدة: مدين حسابات المصروفات المعتمدة / دائن حساب صاحب العهدة.
    رد باقي عهدة: مدين الخزنة / دائن حساب صاحب العهدة.
    """
    marker = f"CUST-AUTO:{settlement.id}"
    amount = as_float(settlement.amount)
    operation_type = get_custody_operation_type(settlement)
    treasury_id = settlement.treasury_account_id
    entity_id = settlement.entity_account_id
    journal_date = settlement.date or date.today().isoformat()
    journal_reference = settlement.reference or f"CUST-{settlement.id:06d}"
    project = settlement.project
    cost_center = f"مشروع - {project.display_name}" if project else None
    created_entries = []

    if amount <= 0:
        replace_auto_journals(marker, [])
        return

    if operation_type == "تسوية عهدة":
        try:
            lines = json.loads(settlement.settlement_lines or "[]")
        except Exception:
            lines = []
        if not lines and (settlement.expense_item or "").strip():
            fallback_account = get_or_create_custody_expense_account(settlement.expense_item)
            lines = [{
                "line_no": 1,
                "expense_account_id": fallback_account.id if fallback_account else None,
                "amount": amount,
                "description": (settlement.expense_item or "").strip(),
                "reference": journal_reference,
                "expense_nature": settlement.expense_nature,
            }]

        for index, line in enumerate(lines, start=1):
            line_amount = as_float(line.get("amount"))
            expense_account_id = as_int(line.get("expense_account_id"))
            if line_amount <= 0 or not expense_account_id:
                continue
            line_description = (line.get("description") or "").strip()
            line_reference = (line.get("reference") or "").strip() or journal_reference
            line_notes = (line.get("notes") or "").strip()
            line_nature = (line.get("expense_nature") or "").strip()
            created_entries.append({
                "date": journal_date,
                "reference": line_reference,
                "journal_name": "متنوع",
                "status": "مرحل",
                "description": (
                    f"{marker}:{index} - تسوية عهدة {settlement.entity_type} {settlement.entity_name or ''}"
                    + (f" - {line_description}" if line_description else "")
                    + (f" - {line_nature}" if line_nature else "")
                    + (f" - ملاحظات: {line_notes}" if line_notes else "")
                ).strip(),
                "debit_account_id": expense_account_id,
                "credit_account_id": entity_id,
                "amount": round(line_amount, 2),
                "project_id": settlement.project_id,
                "cost_center": cost_center,
            })
    else:
        if operation_type in CUSTODY_DISBURSE_OPERATIONS or settlement.voucher_type == "صرف":
            debit_account_id = entity_id
            credit_account_id = treasury_id
        else:
            debit_account_id = treasury_id
            credit_account_id = entity_id

        description = f"{marker} - {operation_type} {settlement.entity_type} {settlement.entity_name or ''}".strip()
        if (settlement.expense_item or "").strip():
            description = f"{description} - بند: {settlement.expense_item.strip()}"

        created_entries.append({
            "date": journal_date,
            "reference": journal_reference,
            "journal_name": "متنوع",
            "status": "مرحل",
            "description": description,
            "debit_account_id": debit_account_id,
            "credit_account_id": credit_account_id,
            "amount": round(amount, 2),
            "project_id": settlement.project_id,
            "cost_center": cost_center,
        })

    replace_auto_journals(marker, created_entries)


def get_custody_settlement_amount(settlement):
    """قيمة العملية الفعلية: التسوية تُقاس بمجموع بنودها المعتمدة."""
    amount = as_float(settlement.amount)
    if get_custody_operation_type(settlement) == "تسوية عهدة":
        try:
            line_items = json.loads(settlement.settlement_lines or "[]")
        except Exception:
            line_items = []
        line_total = sum(as_float(line.get("amount")) for line in line_items)
        amount = line_total or amount
    return round(amount, 2)


def build_custody_balances(settlements):
    """تطبيق معادلة SRS 3.2 / 3.3 على كل صاحب عهدة.

    المتبقي من العهدة = إجمالي العهد المنصرفة - (المصروفات المعتمدة + المبالغ المرتجعة)
    مع تفصيل المصروفات إلى: مصروف نقلة / مصروف يومي / مصروف إداري.
    """
    owner_map = defaultdict(lambda: {
        "entity_type": "",
        "entity_name": "",
        "account_id": None,
        "disbursed": 0.0,
        "settled_total": 0.0,
        "settled_trip": 0.0,
        "settled_daily": 0.0,
        "settled_admin": 0.0,
        "returned": 0.0,
        "remaining": 0.0,
        "count": 0,
        "first_date": "",
        "latest_date": "",
    })

    for settlement in settlements:
        entity_name = (settlement.entity_name or "").strip() or "-"
        key = (settlement.entity_account_id, entity_name)
        owner = owner_map[key]
        owner["entity_type"] = (settlement.entity_type or "").strip() or "سائق"
        owner["entity_name"] = entity_name
        owner["account_id"] = settlement.entity_account_id
        owner["count"] += 1
        owner["latest_date"] = max(owner["latest_date"], settlement.date or "")
        owner["first_date"] = min(owner["first_date"] or "9999-12-31", settlement.date or "9999-12-31")

        operation_type = get_custody_operation_type(settlement)
        amount = get_custody_settlement_amount(settlement)

        if operation_type in CUSTODY_DISBURSE_OPERATIONS:
            owner["disbursed"] += amount
        elif operation_type == "رد باقي عهدة":
            owner["returned"] += amount
        else:
            owner["settled_total"] += amount
            try:
                line_items = json.loads(settlement.settlement_lines or "[]")
            except Exception:
                line_items = []
            if line_items:
                for line in line_items:
                    nature = (line.get("expense_nature") or "").strip()
                    line_amount = as_float(line.get("amount"))
                    if nature == "مصروف نقلة":
                        owner["settled_trip"] += line_amount
                    elif nature == "مصروف يومي":
                        owner["settled_daily"] += line_amount
                    elif nature == "مصروف إداري":
                        owner["settled_admin"] += line_amount
            else:
                nature = (settlement.expense_nature or "").strip()
                if nature == "مصروف نقلة":
                    owner["settled_trip"] += amount
                elif nature == "مصروف يومي":
                    owner["settled_daily"] += amount
                elif nature == "مصروف إداري":
                    owner["settled_admin"] += amount

    summaries = []
    for owner in owner_map.values():
        owner["remaining"] = round(owner["disbursed"] - (owner["settled_total"] + owner["returned"]), 2)
        for field in ("disbursed", "settled_total", "settled_trip", "settled_daily", "settled_admin", "returned"):
            owner[field] = round(owner[field], 2)
        if owner["first_date"] == "9999-12-31":
            owner["first_date"] = ""
        summaries.append(owner)

    summaries.sort(key=lambda row: (row["entity_type"], row["entity_name"]))
    return summaries


def parse_custody_settlement_lines(form):
    account_ids = form.getlist("line_account_id")
    amounts = form.getlist("line_amount")
    descriptions = form.getlist("line_description")
    references = form.getlist("line_reference")
    notes_list = form.getlist("line_notes")
    natures = form.getlist("line_expense_nature")
    lines = []

    for index, account_id in enumerate(account_ids):
        parsed_account_id = as_int(account_id)
        amount = as_float(amounts[index] if index < len(amounts) else 0)
        description = (descriptions[index] if index < len(descriptions) else "").strip()
        reference = (references[index] if index < len(references) else "").strip()
        notes = (notes_list[index] if index < len(notes_list) else "").strip()
        nature = (natures[index] if index < len(natures) else "").strip()
        if not parsed_account_id or amount <= 0:
            continue
        lines.append({
            "line_no": len(lines) + 1,
            "expense_account_id": parsed_account_id,
            "amount": amount,
            "description": description,
            "reference": reference,
            "notes": notes,
            "expense_nature": nature if nature in CUSTODY_EXPENSE_NATURES else None,
        })

    return lines


def build_custody_owner_rows(all_accounts, settlements, account_balances=None):
    """صفوف أصحاب العهد: حسابات السواقين/المناديب/المعدات وأي حساب له حركة عهدة."""
    account_balances = account_balances or {}
    accounts_by_id = {account.id: account for account in all_accounts}
    used_account_ids = {settlement.entity_account_id for settlement in settlements}
    owner_map = {}

    for account in all_accounts:
        code = (account.code or "").strip().upper()
        if code.startswith("CAT-"):
            continue
        owner_type = get_custody_owner_type(account)
        if not owner_type and account.id not in used_account_ids:
            continue
        owner_map[account.id] = {
            "account": account,
            "entity_type": owner_type or (account.category or "حساب"),
            "entity_name": account.name,
            "count": 0,
            "balance": as_float(account_balances.get(account.id, 0)),
            "latest_date": "",
            "settlements": [],
            "disbursed": 0.0,
            "settled": 0.0,
            "returned": 0.0,
        }

    for settlement in settlements:
        owner = owner_map.get(settlement.entity_account_id)
        if not owner:
            account = accounts_by_id.get(settlement.entity_account_id)
            if not account:
                continue
            owner = owner_map.setdefault(account.id, {
                "account": account,
                "entity_type": get_custody_owner_type(account) or (account.category or "حساب"),
                "entity_name": account.name,
                "count": 0,
                "balance": as_float(account_balances.get(account.id, 0)),
                "latest_date": "",
                "settlements": [],
                "disbursed": 0.0,
                "settled": 0.0,
                "returned": 0.0,
            })
        owner["count"] += 1
        owner["latest_date"] = max(owner["latest_date"], settlement.date or "")

        op_type = get_custody_operation_type(settlement)
        amount = get_custody_settlement_amount(settlement)
        if op_type in CUSTODY_DISBURSE_OPERATIONS:
            owner["disbursed"] += amount
        elif op_type == "رد باقي عهدة":
            owner["returned"] += amount
        else:
            owner["settled"] += amount

        owner["settlements"].append({
            "settlement": settlement,
            "operation_type": op_type,
            "amount": amount,
        })

    rows = list(owner_map.values())
    for row in rows:
        row["settlements"].sort(key=lambda item: ((item["settlement"].date or ""), item["settlement"].id))
        row["remaining"] = round(row["disbursed"] - (row["settled"] + row["returned"]), 2)
        for field in ("disbursed", "settled", "returned"):
            row[field] = round(row[field], 2)
    rows.sort(key=lambda row: (row["latest_date"], row["balance"], row["entity_name"]), reverse=True)
    return rows


def get_custody_expense_accounts(accounts, entity_accounts):
    """كل حسابات المصروفات متاحة للتسوية، فمصروفات العهد قد تشمل أي بند مصروف."""
    blocked_ids = {account.id for account in entity_accounts}
    expense_accounts = []
    for account in accounts:
        if account.id in blocked_ids:
            continue
        category = (account.category or "").strip()
        code = (account.code or "").strip().upper()
        if code.startswith("CAT-"):
            continue
        if category == "المصروفات":
            expense_accounts.append(account)
    expense_accounts.sort(key=lambda account: ((account.expense_class or "zz"), (account.code or ""), (account.name or "")))
    return expense_accounts


# ---------------------------------------------------------------------------
# مستخلصات مقاولي الباطن (SRS 2.2)
# ---------------------------------------------------------------------------


def generate_progress_payment_number(project_id, subcontractor_id):
    query = ProgressPayment.query.filter(ProgressPayment.project_id == project_id)
    if subcontractor_id:
        query = query.filter(ProgressPayment.subcontractor_id == subcontractor_id)
    else:
        query = query.filter(ProgressPayment.subcontractor_id.is_(None))
    return str(query.count() + 1)


def parse_progress_payment_items(form):
    """يقرأ بنود المستخلص: البند، الوحدة، الكمية المنفذة، سعر الفئة."""
    boq_item_ids = form.getlist("boq_item_id")
    descriptions = form.getlist("description")
    units = form.getlist("unit")
    quantities = form.getlist("quantity")
    unit_prices = form.getlist("unit_price")

    max_len = max(
        len(boq_item_ids),
        len(descriptions),
        len(units),
        len(quantities),
        len(unit_prices),
    ) if any([boq_item_ids, descriptions, units, quantities, unit_prices]) else 0

    lines = []
    for index in range(max_len):
        boq_item_id = as_int(boq_item_ids[index]) if index < len(boq_item_ids) else None
        description = (descriptions[index] if index < len(descriptions) else "").strip()
        unit = (units[index] if index < len(units) else "").strip()
        quantity = as_float(quantities[index] if index < len(quantities) else 0)
        unit_price = as_float(unit_prices[index] if index < len(unit_prices) else 0)
        line_value = round(quantity * unit_price, 2)

        if line_value <= 0 and not description and not boq_item_id:
            continue
        if line_value <= 0:
            continue

        lines.append({
            "boq_item_id": boq_item_id,
            "description": description or None,
            "unit": unit or None,
            "quantity": quantity,
            "unit_price": unit_price,
            "value": line_value,
        })
    return lines


def recalculate_progress_payment(payment):
    """المعادلات المحاسبية للمستخلص (SRS 2.2).

    الإجمالي المستحق للبند = الكمية المنفذة × سعر الفئة
    إجمالي قيمة الأعمال = مجموع إجماليات البنود
    صافي المستحق = إجمالي الأعمال - (الدفعات تحت الحساب + الاستقطاعات/الخصومات)
    """
    items = ProgressPaymentItem.query.filter_by(progress_payment_id=payment.id).all()
    total_value = 0.0
    for item in items:
        item.value = round(as_float(item.quantity) * as_float(item.unit_price), 2)
        total_value += item.value

    payment.total_value = round(total_value, 2)

    retention_percentage = as_float(payment.retention_percentage)
    if retention_percentage > 0:
        payment.discount_insurance = round(payment.total_value * retention_percentage / 100.0, 2)

    tax_percentage = as_float(payment.tax_percentage)
    if tax_percentage > 0:
        payment.tax = round(payment.total_value * tax_percentage / 100.0, 2)

    deductions_total = round(payment.deductions_total, 2)
    advances = as_float(payment.advance_deduction)
    payment.net_value = round(payment.total_value - (advances + deductions_total), 2)
    return payment


def get_subcontractor_outstanding_advances(subcontractor_id):
    """الدفعات تحت الحساب التي لم تُخصم في أي مستخلص بعد."""
    if not subcontractor_id:
        return 0.0
    paid = sum(
        as_float(item.amount)
        for item in SubcontractorPayment.query.filter_by(subcontractor_id=subcontractor_id).all()
    )
    deducted = sum(
        as_float(item.advance_deduction)
        for item in ProgressPayment.query.filter_by(subcontractor_id=subcontractor_id).all()
    )
    return round(paid - deducted, 2)


JOURNAL_SOURCE_MARKER = "JRN-SRC:"


def journal_source_note(entry_id):
    return f"{JOURNAL_SOURCE_MARKER}{entry_id}"


def is_journal_sourced_document(notes):
    return JOURNAL_SOURCE_MARKER in (notes or "")


def journal_id_from_source_note(notes):
    if JOURNAL_SOURCE_MARKER not in (notes or ""):
        return None
    token = (notes or "").split(JOURNAL_SOURCE_MARKER, 1)[1].split()[0]
    try:
        return int(token)
    except (TypeError, ValueError):
        return None


def _payment_method_from_treasury(account):
    blob = f"{(account.code or '')} {(account.name or '')}"
    if "BANK" in (account.code or "").upper() or "بنك" in blob:
        return "بنكي"
    return "نقدي"


def _skip_category_parent(account):
    code = ((account.code or "") if account else "").strip().upper()
    return not account or code.startswith("CAT-")


def subcontractor_from_account(account):
    if _skip_category_parent(account):
        return None
    code = (account.code or "").strip().upper()
    if code.startswith("SUB-"):
        try:
            return Subcontractor.query.get(int(code.split("-", 1)[1]))
        except (TypeError, ValueError):
            pass
    if account.category == "مقاولي الباطن":
        name = (account.name or "").replace("مقاول باطن - ", "", 1).strip()
        if name:
            return Subcontractor.query.filter_by(name=name).first()
    return None


def supplier_from_account(account):
    if _skip_category_parent(account):
        return None
    code = (account.code or "").strip().upper()
    if code.startswith("SUP-"):
        try:
            return Supplier.query.get(int(code.split("-", 1)[1]))
        except (TypeError, ValueError):
            pass
    if account.category == "الموردين":
        name = (account.name or "").replace("مورد - ", "", 1).strip()
        if name:
            return Supplier.query.filter_by(name=name).first()
    return None


def client_name_from_account(account):
    if _skip_category_parent(account) or not account or account.category != "العملاء":
        return ""
    name = account.name or ""
    if name.startswith("عميل - "):
        name = name[7:]
    return canonical_client_name(name)


def _query_journal_sourced(model, entry_id):
    marker = journal_source_note(entry_id)
    return model.query.filter(model.notes.like(f"%{marker}%")).all()


def _delete_journal_sourced_docs(entry_id):
    changed = False
    for model in (SubcontractorPayment, SupplierPayment, ClientReceipt):
        for item in _query_journal_sourced(model, entry_id):
            db.session.delete(item)
            changed = True
    return changed


def _upsert_journal_sourced_row(model, entry, defaults):
    existing = _query_journal_sourced(model, entry.id)
    row = existing[0] if existing else None
    for extra in existing[1:]:
        db.session.delete(extra)
    if as_float(defaults.get("amount")) <= 0:
        if row:
            db.session.delete(row)
        return True
    if row is None:
        row = model(**defaults)
        db.session.add(row)
        return True
    for field, value in defaults.items():
        setattr(row, field, value)
    return True


def clear_journal_sourced_docs(entry_id, commit=True):
    changed = _delete_journal_sourced_docs(entry_id)
    if changed and commit:
        db.session.commit()
    return changed


def sync_operational_docs_from_journal(entry, commit=True):
    """قيد اليومية اليدوي على حساب طرف يُنشئ دفعة/تحصيل تشغيلي حتى يظهر في كشفه."""
    if not entry or not getattr(entry, "id", None):
        return False
    if is_auto_journal_entry(entry) or normalize_journal_status(entry.status) != "مرحل":
        changed = _delete_journal_sourced_docs(entry.id)
        if changed and commit:
            db.session.commit()
        return changed

    debit = entry.debit_account or ChartOfAccount.query.get(entry.debit_account_id)
    credit = entry.credit_account or ChartOfAccount.query.get(entry.credit_account_id)
    amount = round(as_float(entry.amount), 2)
    marker = journal_source_note(entry.id)
    note = f"{marker} - من قيد اليومية {entry.display_number}"
    changed = False

    sub = subcontractor_from_account(debit)
    supplier = supplier_from_account(debit)
    client_name = client_name_from_account(credit)
    treasury_out = credit if is_operational_treasury_account(credit) else None
    treasury_in = debit if is_operational_treasury_account(debit) else None

    matched = False
    if sub and amount > 0:
        matched = True
        _delete_journal_sourced_docs(entry.id)
        _upsert_journal_sourced_row(SubcontractorPayment, entry, {
            "subcontractor_id": sub.id,
            "project_id": entry.project_id,
            "date": entry.date or date.today().isoformat(),
            "amount": amount,
            "payment_method": _payment_method_from_treasury(treasury_out or credit),
            "treasury_account_id": (treasury_out.id if treasury_out else None),
            "reference": entry.reference or entry.display_number,
            "notes": note,
        })
        changed = True
    elif supplier and amount > 0:
        matched = True
        _delete_journal_sourced_docs(entry.id)
        _upsert_journal_sourced_row(SupplierPayment, entry, {
            "supplier_id": supplier.id,
            "project_id": entry.project_id,
            "date": entry.date or date.today().isoformat(),
            "amount": amount,
            "payment_method": _payment_method_from_treasury(treasury_out or credit),
            "treasury_account_id": (treasury_out.id if treasury_out else None),
            "reference": entry.reference or entry.display_number,
            "notes": note,
            "status": "مرحّل",
        })
        changed = True
    elif client_name and treasury_in and amount > 0:
        matched = True
        _delete_journal_sourced_docs(entry.id)
        _upsert_journal_sourced_row(ClientReceipt, entry, {
            "client_name": client_name,
            "project_id": entry.project_id,
            "date": entry.date or date.today().isoformat(),
            "amount": amount,
            "payment_method": _payment_method_from_treasury(treasury_in),
            "treasury_account_id": treasury_in.id,
            "reference": entry.reference or entry.display_number,
            "notes": note,
            "status": "مرحّل",
        })
        changed = True

    if not matched:
        changed = _delete_journal_sourced_docs(entry.id) or changed

    if changed and commit:
        db.session.commit()
    return changed


def sync_posted_journals_to_documents():
    """يربط القيود المرحلة الحالية بالمستندات التشغيلية، بما فيها القيود القديمة."""
    changed = False
    for entry in JournalEntry.query.filter_by(status="مرحل").order_by(JournalEntry.id.asc()).all():
        if is_auto_journal_entry(entry):
            continue
        if sync_operational_docs_from_journal(entry, commit=False):
            changed = True
    if changed:
        db.session.commit()
    return changed


def sync_progress_payment_journals(payment):
    """قيود المستخلص: قيمة الأعمال بالكامل على حساب الطرف، ثم كل استقطاع بقيد مستقل."""
    sync_journal_related_accounts()
    marker = f"PP-AUTO:{payment.id}"
    journal_date = payment.date or payment.period_end or date.today().isoformat()
    reference = payment.payment_number or f"PP-{payment.id:06d}"
    project = payment.project
    cost_center = f"مشروع - {project.display_name}" if project else None
    total_value = as_float(payment.total_value)
    payloads = []

    def base_payload(description, debit_id, credit_id, amount):
        return {
            "date": journal_date,
            "reference": reference,
            "journal_name": "المستخلصات",
            "status": "مرحل",
            "description": f"{marker} - {description}",
            "debit_account_id": debit_id,
            "credit_account_id": credit_id,
            "amount": round(as_float(amount), 2),
            "project_id": payment.project_id,
            "cost_center": cost_center,
        }

    if payment.subcontractor_id:
        subcontractor = payment.subcontractor
        party_account = get_or_create_subcontractor_account(subcontractor)
        expense_account = get_account_by_code("EXP-SUB")
        party_label = subcontractor.name if subcontractor else "مقاول باطن"

        if party_account and expense_account and total_value > 0:
            payloads.append(base_payload(
                f"مستخلص رقم {reference} - أعمال منفذة لمقاول الباطن {party_label}",
                expense_account.id,
                party_account.id,
                total_value,
            ))

        # الاستقطاعات تُخصم من حساب المقاول لصالح حسابات الاحتجاز/الضرائب/الغرامات
        deduction_map = [
            (as_float(payment.discount_insurance), "LIB-RET", "استقطاع تأمينات/ضمان"),
            (as_float(payment.tax), "LIB-TAX", "استقطاع ضرائب"),
            (as_float(payment.penalties), "REV-PEN", "غرامات وخصومات"),
            (as_float(payment.other_deductions), "REV-OTH", "استقطاعات أخرى"),
        ]
        for amount, account_code, label in deduction_map:
            target_account = get_account_by_code(account_code)
            if amount > 0 and party_account and target_account:
                payloads.append(base_payload(
                    f"مستخلص رقم {reference} - {label} على مقاول الباطن {party_label}",
                    party_account.id,
                    target_account.id,
                    amount,
                ))
    else:
        # مستخلص مقدم للعميل: إيراد على حساب العميل واستقطاعاته أصول محتجزة لدى الغير
        client_account = get_or_create_client_account(project.client_name if project else "")
        revenue_account = get_account_by_code("REV-WRK")
        client_label = project.client_name if project else "عميل"

        if client_account and revenue_account and total_value > 0:
            payloads.append(base_payload(
                f"مستخلص رقم {reference} - أعمال منفذة للعميل {client_label}",
                client_account.id,
                revenue_account.id,
                total_value,
            ))

        deduction_map = [
            (as_float(payment.discount_insurance), "RET-CLI", "تأمينات محتجزة لدى العميل"),
            (as_float(payment.tax), "TAX-WHT", "ضرائب مخصومة تحت الحساب"),
            (as_float(payment.penalties), "EXP-PEN", "غرامات وجزاءات"),
            (as_float(payment.other_deductions), "EXP-PEN", "استقطاعات أخرى"),
        ]
        for amount, account_code, label in deduction_map:
            target_account = get_account_by_code(account_code)
            if amount > 0 and client_account and target_account:
                payloads.append(base_payload(
                    f"مستخلص رقم {reference} - {label}",
                    target_account.id,
                    client_account.id,
                    amount,
                ))

    replace_auto_journals(marker, payloads)


def sync_subcontractor_payment_journal(advance):
    """دفعة تحت الحساب: مدين حساب المقاول / دائن الخزنة."""
    if is_journal_sourced_document(advance.notes):
        replace_auto_journals(f"SPAY-AUTO:{advance.id}", [])
        return
    sync_journal_related_accounts()
    marker = f"SPAY-AUTO:{advance.id}"
    party_account = get_or_create_subcontractor_account(advance.subcontractor)
    treasury_account = None
    if advance.treasury_account_id:
        treasury_account = ChartOfAccount.query.get(advance.treasury_account_id)
    if not treasury_account:
        treasury_account = get_account_by_code("TRS-MAIN")

    payloads = []
    amount = as_float(advance.amount)
    if party_account and treasury_account and amount > 0:
        project = advance.project
        payloads.append({
            "date": advance.date or date.today().isoformat(),
            "reference": advance.reference or f"SPAY-{advance.id:06d}",
            "journal_name": "المستخلصات",
            "status": "مرحل",
            "description": (
                f"{marker} - دفعة تحت الحساب ({advance.payment_method}) "
                f"لمقاول الباطن {advance.subcontractor.name if advance.subcontractor else ''}"
            ).strip(),
            "debit_account_id": party_account.id,
            "credit_account_id": treasury_account.id,
            "amount": round(amount, 2),
            "project_id": advance.project_id,
            "cost_center": f"مشروع - {project.display_name}" if project else None,
        })

    replace_auto_journals(marker, payloads)


def build_subcontractor_statement(subcontractor):
    """كشف حساب مقاول باطن: الأعمال المنفذة، المسدد، الرصيد المتبقي (SRS 5)."""
    payments = ProgressPayment.query.filter_by(subcontractor_id=subcontractor.id)\
        .order_by(ProgressPayment.date.asc(), ProgressPayment.id.asc()).all()
    advances = SubcontractorPayment.query.filter_by(subcontractor_id=subcontractor.id)\
        .order_by(SubcontractorPayment.date.asc(), SubcontractorPayment.id.asc()).all()

    total_works = round(sum(as_float(item.total_value) for item in payments), 2)
    total_deductions = round(sum(as_float(item.deductions_total) for item in payments), 2)
    total_paid = round(sum(as_float(item.amount) for item in advances), 2)
    net_due = round(total_works - (total_paid + total_deductions), 2)

    movements = []
    for payment in payments:
        movements.append({
            "date": payment.date or payment.period_end or "",
            "source": "مستخلص",
            "reference": payment.document_number,
            "description": (
                f"أعمال منفذة - {payment.project.display_name if payment.project else '-'}"
            ),
            "credit": as_float(payment.total_value),
            "debit": 0.0,
        })
        if as_float(payment.deductions_total) > 0:
            movements.append({
                "date": payment.date or payment.period_end or "",
                "source": "استقطاعات",
                "reference": payment.document_number,
                "description": (
                    f"تأمينات/ضمان {format_grouped_number(payment.discount_insurance)}"
                    f" - ضرائب {format_grouped_number(payment.tax)}"
                    f" - غرامات {format_grouped_number(payment.penalties)}"
                    f" - أخرى {format_grouped_number(payment.other_deductions)}"
                ),
                "credit": 0.0,
                "debit": as_float(payment.deductions_total),
            })

    for advance in advances:
        source_label = "دفعة تحت الحساب"
        if is_journal_sourced_document(advance.notes):
            source_label = "قيد يومية / دفعة"
        movements.append({
            "date": advance.date or "",
            "source": source_label,
            "reference": advance.reference or f"SPAY-{advance.id:06d}",
            "description": f"صرف {advance.payment_method} - {advance.notes or ''}".strip(),
            "credit": 0.0,
            "debit": as_float(advance.amount),
        })

    party_account = get_or_create_subcontractor_account(subcontractor)
    sourced_ids = {
        journal_id_from_source_note(advance.notes)
        for advance in advances
        if journal_id_from_source_note(advance.notes)
    }
    if party_account:
        extra_entries = JournalEntry.query.filter(
            JournalEntry.status == "مرحل",
            (JournalEntry.debit_account_id == party_account.id) | (JournalEntry.credit_account_id == party_account.id),
        ).all()
        for extra in extra_entries:
            if is_auto_journal_entry(extra) or extra.id in sourced_ids:
                continue
            debit = as_float(extra.amount) if extra.debit_account_id == party_account.id else 0.0
            credit = as_float(extra.amount) if extra.credit_account_id == party_account.id else 0.0
            if debit <= 0 and credit <= 0:
                continue
            total_paid += debit
            total_works += credit
            movements.append({
                "date": extra.date or "",
                "source": "قيد يومية",
                "reference": extra.reference or extra.display_number,
                "description": extra.description or "",
                "credit": credit,
                "debit": debit,
            })
        net_due = round(total_works - (total_paid + total_deductions), 2)

    movements.sort(key=lambda row: ((row["date"] or ""), row["reference"] or ""))
    running = 0.0
    for movement in movements:
        running += movement["credit"] - movement["debit"]
        movement["running_balance"] = round(running, 2)

    return {
        "payments": payments,
        "advances": advances,
        "movements": movements,
        "total_works": total_works,
        "total_deductions": total_deductions,
        "total_paid": total_paid,
        "net_due": net_due,
    }


# ---------------------------------------------------------------------------
# مقايسات العملاء (SRS 4)
# ---------------------------------------------------------------------------


def generate_estimation_code():
    index = Estimation.query.count() + 1
    while True:
        candidate = f"EST-{index:05d}"
        if not Estimation.query.filter_by(code=candidate).first():
            return candidate
        index += 1


def parse_estimation_items(form):
    descriptions = form.getlist("item_description")
    units = form.getlist("item_unit")
    quantities = form.getlist("item_quantity")
    unit_prices = form.getlist("item_unit_price")
    discounts = form.getlist("item_discount_percentage")

    lines = []
    max_len = max(
        len(descriptions),
        len(units),
        len(quantities),
        len(unit_prices),
        len(discounts),
    ) if any([descriptions, units, quantities, unit_prices, discounts]) else 0

    for index in range(max_len):
        description = (descriptions[index] if index < len(descriptions) else "").strip()
        unit = (units[index] if index < len(units) else "").strip()
        quantity = as_float(quantities[index] if index < len(quantities) else 0)
        unit_price = as_float(unit_prices[index] if index < len(unit_prices) else 0)
        discount_percentage = as_float(discounts[index] if index < len(discounts) else 0)
        total_before_discount = round(quantity * unit_price, 2)

        if not description and total_before_discount <= 0:
            continue

        lines.append({
            "description": description or "بند بدون وصف",
            "unit": unit or None,
            "quantity": quantity,
            "unit_price": unit_price,
            "discount_percentage": discount_percentage,
            "total_before_discount": total_before_discount,
            "discount_value": round(total_before_discount * discount_percentage / 100.0, 2),
        })
    return lines


def recalculate_estimation(estimation):
    """معادلات المقايسة (SRS 4.2).

    إجمالي القيمة = SUM(الكمية × سعر الوحدة)
    قيمة الخصم = إجمالي القيمة × (نسبة الخصم / 100) + خصومات البنود المحددة
    الصافي بعد الخصم = إجمالي القيمة - قيمة الخصم
    القيمة الإجمالية النهائية = الصافي بعد الخصم ± (الصافي بعد الخصم × نسبة الإداريات / 100)
    """
    items = EstimationItem.query.filter_by(estimation_id=estimation.id).all()

    total_value = 0.0
    item_discount_total = 0.0
    for item in items:
        item.total_before_discount = round(as_float(item.quantity) * as_float(item.unit_price), 2)
        item.discount_value = round(
            item.total_before_discount * as_float(item.discount_percentage) / 100.0, 2
        )
        total_value += item.total_before_discount
        item_discount_total += item.discount_value

    total_value = round(total_value, 2)
    header_discount = round(total_value * as_float(estimation.discount_percentage) / 100.0, 2)

    estimation.total_value = total_value
    estimation.discount_value = round(header_discount + item_discount_total, 2)
    estimation.net_after_discount = round(total_value - estimation.discount_value, 2)
    estimation.admin_value = round(
        estimation.net_after_discount * as_float(estimation.admin_percentage) / 100.0, 2
    )
    if (estimation.admin_mode or "إضافة") == "خصم":
        estimation.final_value = round(estimation.net_after_discount - estimation.admin_value, 2)
    else:
        estimation.final_value = round(estimation.net_after_discount + estimation.admin_value, 2)
    return estimation


def sync_estimation_journal(estimation):
    """المقايسة عرض سعر وليست إثبات إيراد.

    الإيراد يُثبت فقط من مستخلص العميل حتى لا يتكرر مع اعتماد المقايسة.
    أي قيد EST-AUTO قديم يُحذف عند المزامنة.
    """
    marker = f"EST-AUTO:{estimation.id}"
    replace_auto_journals(marker, [])


def client_progress_query():
    return ProgressPayment.query.filter(ProgressPayment.subcontractor_id.is_(None))


def subcontractor_progress_query():
    return ProgressPayment.query.filter(ProgressPayment.subcontractor_id.isnot(None))


def sync_labor_journal(entry):
    """عمالة الموقع: مدين مصروف العمالة / دائن أجور مستحقة، والسلف من الخزنة."""
    sync_journal_related_accounts()
    marker = f"LAB-AUTO:{entry.id}"
    expense_account = get_account_by_code("EXP-LAB")
    payable_account = get_account_by_code("LIB-WAG") or get_account_by_code("LIB-ACR")
    treasury = get_account_by_code("TRS-MAIN")
    project = entry.project
    cost_center = f"مشروع - {project.display_name}" if project else None
    payloads = []
    amount = as_float(entry.amount)
    advances = as_float(entry.advances)
    deductions = as_float(entry.deductions)
    journal_date = entry.date or date.today().isoformat()

    def payload(description, debit_id, credit_id, value):
        return {
            "date": journal_date,
            "reference": f"LAB-{entry.id:06d}",
            "journal_name": "المصاريف",
            "status": "مرحل",
            "description": f"{marker} - {description}",
            "debit_account_id": debit_id,
            "credit_account_id": credit_id,
            "amount": round(as_float(value), 2),
            "project_id": entry.project_id,
            "cost_center": cost_center,
        }

    if expense_account and payable_account and amount > 0:
        payloads.append(payload(
            f"استحقاق عمالة {entry.description or ''}".strip(),
            expense_account.id,
            payable_account.id,
            amount,
        ))
    if payable_account and treasury and advances > 0:
        payloads.append(payload(
            f"سلفة عمالة {entry.description or ''}".strip(),
            payable_account.id,
            treasury.id,
            advances,
        ))
    if payable_account and deductions > 0:
        tax_account = get_account_by_code("LIB-TAX") or payable_account
        if tax_account.id != payable_account.id:
            payloads.append(payload(
                f"خصم عمالة {entry.description or ''}".strip(),
                payable_account.id,
                tax_account.id,
                deductions,
            ))
    replace_auto_journals(marker, payloads)


def sync_equipment_journals(item):
    """شراء المعدة أصل، والتشغيل/الصيانة مصروف على المشروع."""
    sync_journal_related_accounts()
    asset_account = ChartOfAccount.query.filter_by(code=f"EQP-{item.id:04d}").first()
    if not asset_account:
        sync_journal_related_accounts()
        asset_account = ChartOfAccount.query.filter_by(code=f"EQP-{item.id:04d}").first()
    payable = get_account_by_code("LIB-PAY")
    expense_account = get_account_by_code("EXP-EQP")
    accrued = get_account_by_code("LIB-ACR")
    project = item.project
    cost_center = f"مشروع - {project.display_name}" if project else None
    journal_date = date.today().isoformat()

    purchase_marker = f"EQP-AUTO:{item.id}"
    purchase_payloads = []
    purchase_cost = as_float(item.purchase_cost)
    if asset_account and payable and purchase_cost > 0:
        purchase_payloads.append({
            "date": journal_date,
            "reference": f"EQP-{item.id:06d}",
            "journal_name": "أصول ثابتة",
            "status": "مرحل",
            "description": f"{purchase_marker} - شراء معدة {item.name}",
            "debit_account_id": asset_account.id,
            "credit_account_id": payable.id,
            "amount": round(purchase_cost, 2),
            "project_id": item.project_id,
            "cost_center": cost_center,
        })
    replace_auto_journals(purchase_marker, purchase_payloads)

    operating_marker = f"EQP-OP-AUTO:{item.id}"
    operating_total = as_float(item.operating_cost) + as_float(item.maintenance)
    operating_payloads = []
    if expense_account and accrued and operating_total > 0:
        operating_payloads.append({
            "date": journal_date,
            "reference": f"EQP-OP-{item.id:06d}",
            "journal_name": "المصاريف",
            "status": "مرحل",
            "description": f"{operating_marker} - تشغيل وصيانة معدة {item.name}",
            "debit_account_id": expense_account.id,
            "credit_account_id": accrued.id,
            "amount": round(operating_total, 2),
            "project_id": item.project_id,
            "cost_center": cost_center,
        })
    replace_auto_journals(operating_marker, operating_payloads)


def sync_client_receipt_journal(receipt):
    if is_journal_sourced_document(receipt.notes):
        replace_auto_journals(f"REC-AUTO:{receipt.id}", [])
        return
    sync_journal_related_accounts()
    marker = f"REC-AUTO:{receipt.id}"
    client_account = get_or_create_client_account(receipt.client_name)
    treasury = None
    if receipt.treasury_account_id:
        treasury = ChartOfAccount.query.get(receipt.treasury_account_id)
    if not treasury:
        code = "TRS-BANK" if receipt.payment_method == "بنكي" else "TRS-MAIN"
        treasury = get_account_by_code(code) or get_account_by_code("TRS-MAIN")
    payloads = []
    amount = as_float(receipt.amount)
    if client_account and treasury and amount > 0:
        project = receipt.project
        payloads.append({
            "date": receipt.date or date.today().isoformat(),
            "reference": receipt.document_number,
            "journal_name": "يومية عامة",
            "status": "مرحل",
            "description": (
                f"{marker} - تحصيل {receipt.payment_method} من العميل {receipt.client_name}"
            ),
            "debit_account_id": treasury.id,
            "credit_account_id": client_account.id,
            "amount": round(amount, 2),
            "project_id": receipt.project_id,
            "cost_center": f"مشروع - {project.display_name}" if project else None,
        })
    replace_auto_journals(marker, payloads)


def sync_supplier_payment_journal(payment):
    if is_journal_sourced_document(payment.notes):
        replace_auto_journals(f"PAY-AUTO:{payment.id}", [])
        return
    sync_journal_related_accounts()
    marker = f"PAY-AUTO:{payment.id}"
    party_account = get_or_create_supplier_account(payment.supplier) if payment.supplier else None
    treasury = None
    if payment.treasury_account_id:
        treasury = ChartOfAccount.query.get(payment.treasury_account_id)
    if not treasury:
        code = "TRS-BANK" if payment.payment_method == "بنكي" else "TRS-MAIN"
        treasury = get_account_by_code(code) or get_account_by_code("TRS-MAIN")
    payloads = []
    amount = as_float(payment.amount)
    if party_account and treasury and amount > 0:
        project = payment.project
        payloads.append({
            "date": payment.date or date.today().isoformat(),
            "reference": payment.document_number,
            "journal_name": "يومية عامة",
            "status": "مرحل",
            "description": (
                f"{marker} - سداد {payment.payment_method} للمورد "
                f"{payment.supplier.name if payment.supplier else ''}"
            ).strip(),
            "debit_account_id": party_account.id,
            "credit_account_id": treasury.id,
            "amount": round(amount, 2),
            "project_id": payment.project_id,
            "cost_center": f"مشروع - {project.display_name}" if project else None,
        })
    replace_auto_journals(marker, payloads)


def document_journal_markers(entity_type, entity_id):
    mapping = {
        "progress_payment": [f"PP-AUTO:{entity_id}"],
        "subcontractor_payment": [f"SPAY-AUTO:{entity_id}"],
        "estimation": [f"EST-AUTO:{entity_id}"],
        "purchase_order": [f"PO-JRN-AUTO:{entity_id}"],
        "inventory": [f"INV-AUTO:{entity_id}"],
        "custody": [f"CUST-AUTO:{entity_id}"],
        "labor": [f"LAB-AUTO:{entity_id}"],
        "equipment": [f"EQP-AUTO:{entity_id}", f"EQP-OP-AUTO:{entity_id}"],
        "driver": [f"DRV-WORK-AUTO:{entity_id}", f"DRV-PAY-AUTO:{entity_id}"],
        "client_receipt": [f"REC-AUTO:{entity_id}"],
        "supplier_payment": [f"PAY-AUTO:{entity_id}"],
    }
    return mapping.get(entity_type, [])


def void_document_journals(entity_type, entity_id):
    for marker in document_journal_markers(entity_type, entity_id):
        replace_auto_journals(marker, [])


def build_project_cost_breakdown(project):
    """التكلفة الفعلية للمشروع من المستندات التشغيلية (نفس مصدر ربحية المقايسات)."""
    subcontractor_cost = round(sum(
        as_float(item.total_value)
        for item in ProgressPayment.query.filter_by(project_id=project.id).all()
        if item.subcontractor_id
    ), 2)
    material_cost = round(sum(
        as_float(item.total_value)
        for item in PurchaseOrder.query.filter_by(project_id=project.id).all()
    ), 2)

    custody_cost = 0.0
    for settlement in CustodySettlement.query.filter_by(project_id=project.id).all():
        if get_custody_operation_type(settlement) == "تسوية عهدة":
            custody_cost += get_custody_settlement_amount(settlement)
    custody_cost = round(custody_cost, 2)

    driver_cost = round(sum(
        as_float(item.gross_amount)
        for item in DriverCompensationEntry.query.filter_by(project_id=project.id).all()
    ), 2)
    labor_cost = round(sum(
        as_float(item.amount)
        for item in LaborEntry.query.filter_by(project_id=project.id).all()
    ), 2)
    equipment_cost = round(sum(
        as_float(item.operating_cost) + as_float(item.maintenance)
        for item in Equipment.query.filter_by(project_id=project.id).all()
    ), 2)

    direct_cost = round(
        subcontractor_cost + material_cost + custody_cost + driver_cost + labor_cost + equipment_cost, 2
    )
    admin_allocation = round(direct_cost * as_float(project.admin_percentage) / 100.0, 2)

    return {
        "subcontractor_cost": subcontractor_cost,
        "material_cost": material_cost,
        "custody_cost": custody_cost,
        "driver_cost": driver_cost,
        "labor_cost": labor_cost,
        "equipment_cost": equipment_cost,
        "other_cost": 0.0,
        "direct_cost": direct_cost,
        "admin_allocation": admin_allocation,
        "total_cost": round(direct_cost + admin_allocation, 2),
    }


def list_client_names():
    names = {(project.client_name or "").strip() for project in Project.query.all()}
    names.update({(item.client_name or "").strip() for item in Estimation.query.all()})
    names.update({(item.client_name or "").strip() for item in ClientReceipt.query.all()})
    for account in ChartOfAccount.query.filter_by(category="العملاء").all():
        label = (account.name or "").strip()
        if label.startswith("عميل - "):
            label = label[7:].strip()
        if label:
            names.add(label)
    unique = {}
    for name in names:
        clean = normalize_client_name(name)
        if clean and clean.casefold() not in unique:
            unique[clean.casefold()] = clean
    return sorted(unique.values(), key=lambda item: item.casefold())


