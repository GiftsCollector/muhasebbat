from collections import defaultdict, deque
from datetime import date
from io import BytesIO
import json
import os

from flask import Flask, render_template, request, url_for, flash, redirect, session, g, send_file
from sqlalchemy import inspect as sa_inspect, text
from sqlalchemy.exc import IntegrityError
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.table import Table, TableStyleInfo
from models import db, User, Project, BOQItem, ChartOfAccount, ProgressPayment, ProgressPaymentItem, CostEntry, Subcontractor, Supplier, PurchaseOrder, InventoryTransaction, LaborEntry, Equipment, JournalEntry, CustodySettlement, DriverCompensationEntry

# Create Flask app
app = Flask(__name__)

# Configuration
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-secret-key-2024")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# Database configuration - handles both local SQLite and production
DATABASE_URL = os.getenv("DATABASE_URL")
if DATABASE_URL:
    # Production - use PostgreSQL or other database
    app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
else:
    # Development - use SQLite
    db_folder = os.path.join(os.path.dirname(__file__), "instance")
    os.makedirs(db_folder, exist_ok=True)
    db_path = os.path.join(db_folder, "data.db")
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{db_path}"

# Initialize database
db.init_app(app)

# Create app context and initialize database
with app.app_context():
    db.create_all()
    if app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite"):
        inspector = sa_inspect(db.engine)
        project_columns = {column["name"] for column in inspector.get_columns("project")}
        if "project_name" not in project_columns:
            with db.engine.begin() as connection:
                connection.execute(text("ALTER TABLE project ADD COLUMN project_name VARCHAR(128) NOT NULL DEFAULT ''"))
        purchase_order_columns = {column["name"] for column in inspector.get_columns("purchase_order")}
        purchase_order_alters = {
            "item_name": "ALTER TABLE purchase_order ADD COLUMN item_name VARCHAR(128)",
            "quantity": "ALTER TABLE purchase_order ADD COLUMN quantity FLOAT NOT NULL DEFAULT 0",
            "unit_price": "ALTER TABLE purchase_order ADD COLUMN unit_price FLOAT NOT NULL DEFAULT 0",
            "discount": "ALTER TABLE purchase_order ADD COLUMN discount FLOAT NOT NULL DEFAULT 0",
            "warehouse_name": "ALTER TABLE purchase_order ADD COLUMN warehouse_name VARCHAR(128)",
        }
        with db.engine.begin() as connection:
            for column_name, alter_sql in purchase_order_alters.items():
                if column_name not in purchase_order_columns:
                    connection.execute(text(alter_sql))
        inventory_columns = {column["name"] for column in inspector.get_columns("inventory_transaction")}
        inventory_alters = {
            "supplier_id": "ALTER TABLE inventory_transaction ADD COLUMN supplier_id INTEGER",
        }
        with db.engine.begin() as connection:
            for column_name, alter_sql in inventory_alters.items():
                if column_name not in inventory_columns:
                    connection.execute(text(alter_sql))
        chart_columns = {column["name"] for column in inspector.get_columns("chart_of_account")}
        chart_alters = {
            "opening_balance": "ALTER TABLE chart_of_account ADD COLUMN opening_balance FLOAT NOT NULL DEFAULT 0",
            "term_days": "ALTER TABLE chart_of_account ADD COLUMN term_days INTEGER NOT NULL DEFAULT 0",
        }
        with db.engine.begin() as connection:
            for column_name, alter_sql in chart_alters.items():
                if column_name not in chart_columns:
                    connection.execute(text(alter_sql))
        journal_columns = {column["name"] for column in inspector.get_columns("journal_entry")}
        journal_alters = {
            "reference": "ALTER TABLE journal_entry ADD COLUMN reference VARCHAR(128)",
            "journal_name": "ALTER TABLE journal_entry ADD COLUMN journal_name VARCHAR(64) NOT NULL DEFAULT 'يومية عامة'",
            "branch": "ALTER TABLE journal_entry ADD COLUMN branch VARCHAR(128)",
            "stock_move": "ALTER TABLE journal_entry ADD COLUMN stock_move VARCHAR(128)",
            "status": "ALTER TABLE journal_entry ADD COLUMN status VARCHAR(20) NOT NULL DEFAULT 'مسودة'",
            "cost_center": "ALTER TABLE journal_entry ADD COLUMN cost_center VARCHAR(128)",
        }
        with db.engine.begin() as connection:
            for column_name, alter_sql in journal_alters.items():
                if column_name not in journal_columns:
                    connection.execute(text(alter_sql))
        custody_columns = {column["name"] for column in inspector.get_columns("custody_settlement")}
        custody_alters = {
            "expense_item": "ALTER TABLE custody_settlement ADD COLUMN expense_item VARCHAR(128)",
            "operation_type": "ALTER TABLE custody_settlement ADD COLUMN operation_type VARCHAR(32) NOT NULL DEFAULT 'صرف عهدة'",
            "settlement_lines": "ALTER TABLE custody_settlement ADD COLUMN settlement_lines TEXT",
        }
        with db.engine.begin() as connection:
            for column_name, alter_sql in custody_alters.items():
                if column_name not in custody_columns:
                    connection.execute(text(alter_sql))

JOURNAL_OPTIONS = [
    "نقطة بيع",
    "تقييم المخزون",
    "أرباح وخسائر العملات",
    "أصول ثابتة",
    "متنوع",
    "أرصدة افتتاحية",
    "المخزون",
    "يومية عامة",
]


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


def ensure_chart_account(code, name, category):
    existing_by_code = ChartOfAccount.query.filter_by(code=code).first()
    if existing_by_code:
        return False
    existing_by_name = ChartOfAccount.query.filter_by(name=name, category=category).first()
    if existing_by_name:
        return False
    db.session.add(ChartOfAccount(code=code, name=name, category=category))
    return True


CUSTODY_EXPENSE_ACCOUNTS = [
    ("EXP-ELC", "كهرباء"),
    ("EXP-WTR", "مياه"),
    ("EXP-SOL", "سولار"),
    ("EXP-OFF", "ادوات مكتبية"),
    ("EXP-HOM", "ادوات منزلية"),
    ("EXP-SPR", "قطع غيار"),
    ("EXP-FUR", "اثاث"),
    ("EXP-TIP", "اكراميات"),
    ("EXP-FOD", "اكل وشرب"),
    ("EXP-REN", "ايجارات"),
    ("EXP-OTH", "مصروفات اخرى"),
]


def ensure_custody_expense_accounts():
    changed = False
    for code, name in CUSTODY_EXPENSE_ACCOUNTS:
        changed = ensure_chart_account(code, name, "المصروفات") or changed
    return changed


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
        ("CAT-PRJ", "المشاريع", "المشاريع"),
    ]
    for code, name, category in parent_accounts:
        changed = ensure_chart_account(code, name, category) or changed

    # حسابات تشغيلية أساسية لشركات المقاولات (خزنة رئيسية/موقع/عهد).
    treasury_defaults = [
        ("TRS-MAIN", "الخزنة الرئيسية", "الخزن الفرعية"),
        ("TRS-SITE", "خزنة الموقع", "الخزن الفرعية"),
        ("TRS-CUST", "العهد", "الخزن الفرعية"),
    ]
    for code, name, category in treasury_defaults:
        changed = ensure_chart_account(code, name, category) or changed

    changed = ensure_custody_expense_accounts() or changed

    for supplier in Supplier.query.order_by(Supplier.id).all():
        changed = ensure_chart_account(
            f"SUP-{supplier.id:04d}",
            f"مورد - {supplier.name}",
            "الموردين",
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
    debit_account = ChartOfAccount.query.filter(ChartOfAccount.category.in_(["مواد", "الأصول"])).first()
    credit_account = None
    if order.supplier_id:
        credit_account = ChartOfAccount.query.filter_by(code=f"SUP-{order.supplier_id:04d}").first()
    if not credit_account:
        credit_account = ChartOfAccount.query.filter(ChartOfAccount.category == "موردين").first()

    if not debit_account or not credit_account:
        return

    supplier_label = order.supplier.name if order.supplier else "بدون مورد"
    payload = {
        "date": order.date or date.today().isoformat(),
        "reference": order.order_number or order.invoice_number or f"PO-{order.id:06d}",
        "journal_name": "المخزون",
        "status": "مرحل",
        "description": f"{marker} - أمر شراء {order.order_number or order.invoice_number or order.id} - المورد {supplier_label} - المشروع {order.project.code}",
        "debit_account_id": debit_account.id,
        "credit_account_id": credit_account.id,
        "amount": amount,
        "project_id": order.project_id,
    }

    if auto_journal:
        auto_journal.date = payload["date"]
        auto_journal.reference = payload["reference"]
        auto_journal.journal_name = payload["journal_name"]
        auto_journal.status = payload["status"]
        auto_journal.description = payload["description"]
        auto_journal.debit_account_id = payload["debit_account_id"]
        auto_journal.credit_account_id = payload["credit_account_id"]
        auto_journal.amount = payload["amount"]
        auto_journal.project_id = payload["project_id"]
    else:
        db.session.add(JournalEntry(**payload))

    db.session.commit()


def normalize_journal_status(value):
    return "مرحل" if value == "مرحل" else "مسودة"


def format_grouped_number(value):
    number = as_float(value)
    if abs(number - round(number)) < 0.000001:
        formatted = f"{int(round(number)):,}"
        return formatted.replace(",", ".")

    whole, decimal = f"{number:,.2f}".split(".")
    return f"{whole.replace(',', '.')},{decimal}"


app.jinja_env.filters["groupnum"] = format_grouped_number


def build_account_balances(accounts=None):
    account_rows = accounts or ChartOfAccount.query.order_by(ChartOfAccount.code, ChartOfAccount.name).all()
    balances = {account.id: as_float(getattr(account, "opening_balance", 0)) for account in account_rows}

    for entry in JournalEntry.query.all():
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


def get_journal_entries_in_range(from_date=None, to_date=None):
    query = JournalEntry.query
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
    if category in {"الأصول", "المخازن", "المعدات", "الخزن الفرعية", "العملاء", "المشاريع", "مواد"}:
        return "assets"
    if category in {"الالتزامات", "الموردين", "مقاولي الباطن"}:
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


def get_custody_entity_accounts(accounts):
    selected = []
    seen_ids = set()

    for account in accounts:
        category = (account.category or "").strip()
        code = (account.code or "").strip().upper()
        name = (account.name or "").strip()
        is_entity_account = (
            code.startswith("DRV-")
            or code.startswith("EQP-")
            or "سائق" in name
            or "معدة" in name
        )
        if is_entity_account and account.id not in seen_ids:
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


def sync_custody_settlement_journal(settlement):
    marker = f"CUST-AUTO:{settlement.id}"
    auto_journals = JournalEntry.query.filter(JournalEntry.description.like(f"%{marker}%")).all()
    amount = as_float(settlement.amount)
    operation_type = (getattr(settlement, "operation_type", "") or "").strip() or ((settlement.voucher_type or "") + " عهدة")

    if auto_journals:
        for journal in auto_journals:
            db.session.delete(journal)

    if amount <= 0:
        db.session.commit()
        return

    treasury_id = settlement.treasury_account_id
    entity_id = settlement.entity_account_id
    if settlement.voucher_type == "صرف":
        debit_account_id = entity_id
        credit_account_id = treasury_id
    else:
        debit_account_id = treasury_id
        credit_account_id = entity_id

    journal_date = settlement.date or date.today().isoformat()
    journal_reference = settlement.reference or f"CUST-{settlement.id:06d}"
    created_entries = []

    if operation_type == "تسوية عهدة":
        try:
            lines = json.loads(settlement.settlement_lines or "[]")
        except Exception:
            lines = []
        if not lines and (settlement.expense_item or "").strip():
            lines = [{
                "line_no": 1,
                "expense_account_id": settlement.entity_account_id,
                "amount": amount,
                "description": (settlement.expense_item or "").strip(),
                "reference": journal_reference,
            }]

        for index, line in enumerate(lines, start=1):
            line_amount = as_float(line.get("amount"))
            expense_account_id = as_int(line.get("expense_account_id"))
            if line_amount <= 0 or not expense_account_id:
                continue
            line_description = (line.get("description") or "").strip()
            line_reference = (line.get("reference") or "").strip() or journal_reference
            line_notes = (line.get("notes") or "").strip()
            created_entries.append({
                "date": journal_date,
                "reference": line_reference,
                "journal_name": "متنوع",
                "status": "مرحل",
                "description": (
                    f"{marker}:{index} - تسوية عهدة {settlement.entity_type} {settlement.entity_name or ''}"
                    + (f" - {line_description}" if line_description else "")
                    + (f" - ملاحظات: {line_notes}" if line_notes else "")
                ).strip(),
                "debit_account_id": expense_account_id,
                "credit_account_id": entity_id,
                "amount": line_amount,
                "project_id": settlement.project_id,
            })
    else:
        if settlement.voucher_type == "صرف":
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
            "amount": amount,
            "project_id": settlement.project_id,
        })

    for payload in created_entries:
        db.session.add(JournalEntry(**payload))

    db.session.commit()


def build_custody_summary(items):
    owner_map = defaultdict(lambda: {
        "entity_type": "",
        "entity_name": "",
        "total_spent": 0.0,
        "total_returned": 0.0,
        "balance": 0.0,
        "count": 0,
        "latest_date": "",
        "rows": [],
    })

    for item in items:
        entity_type = "سائق" if item.entity_type == "سائق" else "معدة"
        entity_name = (item.entity_name or "").strip() or "-"
        key = (entity_type, entity_name)
        owner = owner_map[key]
        owner["entity_type"] = entity_type
        owner["entity_name"] = entity_name
        owner["count"] += 1
        owner["latest_date"] = max(owner["latest_date"], item.date or "")

        amount = as_float(item.amount)
        signed_amount = amount if item.voucher_type == "صرف" else -amount
        owner["balance"] += signed_amount
        if item.voucher_type == "صرف":
          owner["total_spent"] += amount
        else:
          owner["total_returned"] += amount

        owner["rows"].append({
            "item": item,
            "signed_amount": signed_amount,
            "running_balance": owner["balance"],
        })

    summaries = list(owner_map.values())
    summaries.sort(key=lambda row: (row["entity_type"], row["entity_name"]))
    for summary in summaries:
        summary["rows"].sort(key=lambda row: ((row["item"].date or ""), row["item"].id))
    return summaries


def parse_custody_settlement_lines(form):
    account_ids = form.getlist("line_account_id")
    amounts = form.getlist("line_amount")
    descriptions = form.getlist("line_description")
    references = form.getlist("line_reference")
    notes_list = form.getlist("line_notes")
    lines = []

    for index, account_id in enumerate(account_ids):
        parsed_account_id = as_int(account_id)
        amount = as_float(amounts[index] if index < len(amounts) else 0)
        description = (descriptions[index] if index < len(descriptions) else "").strip()
        reference = (references[index] if index < len(references) else "").strip()
        notes = (notes_list[index] if index < len(notes_list) else "").strip()
        if not parsed_account_id or amount <= 0:
            continue
        lines.append({
            "line_no": len(lines) + 1,
            "expense_account_id": parsed_account_id,
            "amount": amount,
            "description": description,
            "reference": reference,
            "notes": notes,
        })

    return lines


def build_custody_owner_rows(entity_accounts, settlements, account_balances=None):
    account_balances = account_balances or {}
    owner_map = {}
    for account in entity_accounts:
        category = (account.category or "").strip()
        if "سائق" in (account.name or "") or (account.code or "").startswith("DRV-"):
            account_type = "سائق"
        elif "معدة" in (account.name or "") or (account.code or "").startswith("EQP-"):
            account_type = "معدة"
        else:
            account_type = category or "حساب"
        owner_map[account.id] = {
            "account": account,
            "entity_type": account_type,
            "entity_name": account.name,
            "count": 0,
            "balance": as_float(account_balances.get(account.id, 0)),
            "latest_date": "",
            "settlements": [],
        }

    for settlement in settlements:
        owner = owner_map.get(settlement.entity_account_id)
        if not owner:
            continue
        owner["count"] += 1
        owner["latest_date"] = max(owner["latest_date"], settlement.date or "")

        op_type = (getattr(settlement, "operation_type", "") or "").strip() or ((settlement.voucher_type or "") + " عهدة")
        amount = as_float(settlement.amount)
        if op_type == "تسوية عهدة":
            try:
                line_items = json.loads(settlement.settlement_lines or "[]")
            except Exception:
                line_items = []
            line_total = sum(as_float(line.get("amount")) for line in line_items)
            amount = line_total or amount

        owner["settlements"].append({
            "settlement": settlement,
            "operation_type": op_type,
            "amount": amount,
        })

    rows = list(owner_map.values())
    for row in rows:
        row["settlements"].sort(key=lambda item: ((item["settlement"].date or ""), item["settlement"].id))
    rows.sort(key=lambda row: (row["latest_date"], row["balance"], row["entity_name"]), reverse=True)
    return rows


def get_custody_expense_accounts(accounts, entity_accounts):
    blocked_ids = {account.id for account in entity_accounts}
    allowed_names = {name for _, name in CUSTODY_EXPENSE_ACCOUNTS}
    allowed_codes = {code for code, _ in CUSTODY_EXPENSE_ACCOUNTS}
    expense_accounts = []
    for account in accounts:
        if account.id in blocked_ids:
            continue
        category = (account.category or "").strip()
        name = (account.name or "").strip()
        code = (account.code or "").strip()
        if category == "المصروفات" and (name in allowed_names or code in allowed_codes):
            expense_accounts.append(account)
    expense_accounts.sort(key=lambda account: ((account.category or ""), (account.code or ""), (account.name or "")))
    return expense_accounts


PUBLIC_ENDPOINTS = {"login", "setup_admin", "static"}


@app.before_request
def load_user_and_protect_routes():
    g.current_user = None
    user_id = session.get("user_id")
    if user_id:
        g.current_user = User.query.get(user_id)
        if g.current_user is None or not g.current_user.is_active:
            session.clear()

    endpoint = request.endpoint or ""
    if endpoint in PUBLIC_ENDPOINTS or endpoint.startswith("static"):
        return None

    if g.current_user is None:
        return redirect(url_for("login", next=request.path))

    return None


@app.context_processor
def inject_current_user():
    user = getattr(g, "current_user", None)
    return {
        "current_user": user,
        "is_logged_in": user is not None,
        "is_admin": bool(user and user.role == "admin"),
    }


@app.route("/login", methods=["GET", "POST"])
def login():
    if User.query.count() == 0:
        return redirect(url_for("setup_admin"))

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        user = User.query.filter_by(username=username).first()

        if user and user.is_active and user.check_password(password):
            session["user_id"] = user.id
            flash("تم تسجيل الدخول بنجاح", "success")
            next_url = request.args.get("next")
            if next_url and next_url.startswith("/"):
                return redirect(next_url)
            return redirect(url_for("index"))

        flash("بيانات الدخول غير صحيحة", "danger")

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("تم تسجيل الخروج", "success")
    return redirect(url_for("login"))


@app.route("/setup-admin", methods=["GET", "POST"])
def setup_admin():
    if User.query.count() > 0:
        return redirect(url_for("login"))

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        full_name = (request.form.get("full_name") or "").strip()
        password = request.form.get("password") or ""
        confirm_password = request.form.get("confirm_password") or ""

        if not username or not full_name or not password:
            flash("يرجى استكمال جميع الحقول", "danger")
            return redirect(url_for("setup_admin"))

        if len(password) < 6:
            flash("كلمة المرور يجب أن تكون 6 أحرف على الأقل", "danger")
            return redirect(url_for("setup_admin"))

        if password != confirm_password:
            flash("تأكيد كلمة المرور غير متطابق", "danger")
            return redirect(url_for("setup_admin"))

        admin = User(username=username, full_name=full_name, role="admin", is_active=True)
        admin.set_password(password)
        db.session.add(admin)
        db.session.commit()

        flash("تم إنشاء حساب المدير بنجاح. يمكنك تسجيل الدخول الآن", "success")
        return redirect(url_for("login"))

    return render_template("setup_admin.html")


@app.route("/users", methods=["GET", "POST"])
def users():
    if g.current_user is None or g.current_user.role != "admin":
        flash("ليس لديك صلاحية للوصول لإدارة المستخدمين", "danger")
        return redirect(url_for("index"))

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        full_name = (request.form.get("full_name") or "").strip()
        password = request.form.get("password") or ""
        role = request.form.get("role") or "user"
        is_active = request.form.get("is_active") == "on"

        if not username or not full_name or not password:
            flash("يرجى استكمال بيانات المستخدم", "danger")
            return redirect(url_for("users"))

        if User.query.filter_by(username=username).first():
            flash("اسم المستخدم مستخدم بالفعل", "danger")
            return redirect(url_for("users"))

        user = User(username=username, full_name=full_name, role=role, is_active=is_active)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        flash("تم إنشاء المستخدم بنجاح", "success")
        return redirect(url_for("users"))

    all_users = User.query.order_by(User.id.desc()).all()
    return render_template("users.html", users=all_users)


@app.route("/users/<int:user_id>/update", methods=["POST"])
def update_user(user_id):
    if g.current_user is None or g.current_user.role != "admin":
        flash("ليس لديك صلاحية لتعديل المستخدمين", "danger")
        return redirect(url_for("index"))

    user = User.query.get_or_404(user_id)
    user.full_name = (request.form.get("full_name") or "").strip() or user.full_name
    user.role = request.form.get("role") or user.role
    user.is_active = request.form.get("is_active") == "on"

    new_password = request.form.get("password") or ""
    if new_password:
        if len(new_password) < 6:
            flash("كلمة المرور الجديدة يجب أن تكون 6 أحرف على الأقل", "danger")
            return redirect(url_for("users"))
        user.set_password(new_password)

    if user.id == g.current_user.id and not user.is_active:
        flash("لا يمكنك تعطيل حسابك أثناء تسجيل الدخول", "danger")
        return redirect(url_for("users"))

    db.session.commit()
    flash("تم تحديث المستخدم بنجاح", "success")
    return redirect(url_for("users"))


@app.route("/")
def index():
    projects = Project.query.count()
    accounts = ChartOfAccount.query.count()
    progress_payments = ProgressPayment.query.count()
    journal_entries = JournalEntry.query.count()
    subcontractors = Subcontractor.query.count()
    suppliers = Supplier.query.count()
    purchase_orders = PurchaseOrder.query.count()
    inventory_transactions = InventoryTransaction.query.count()
    labor_entries = LaborEntry.query.count()
    equipment_items = Equipment.query.count()
    draft_purchase_orders = PurchaseOrder.query.filter(PurchaseOrder.status == "مفتوح").count()
    paid_purchase_orders = PurchaseOrder.query.filter(PurchaseOrder.status == "مدفوع").count()
    outstanding_payments = ProgressPayment.query.filter(ProgressPayment.net_value > 0).count()
    total_journal_value = sum(item.amount for item in JournalEntry.query.all())
    recent_entries = JournalEntry.query.order_by(JournalEntry.id.desc()).limit(8).all()
    all_accounts = ChartOfAccount.query.order_by(ChartOfAccount.category, ChartOfAccount.code).all()
    account_balances = build_account_balances(all_accounts)

    supplier_dues_rows = []
    customer_dues_rows = []
    for account in all_accounts:
        balance = as_float(account_balances.get(account.id, 0.0))
        if account.category == "الموردين":
            due_amount = abs(balance) if balance < 0 else balance
            if due_amount > 0:
                supplier_dues_rows.append({"name": account.name, "amount": due_amount})
        elif account.category == "العملاء":
            due_amount = abs(balance)
            if due_amount > 0:
                customer_dues_rows.append({"name": account.name, "amount": due_amount})

    top_supplier_dues = sorted(supplier_dues_rows, key=lambda row: row["amount"], reverse=True)[:5]
    top_customer_dues = sorted(customer_dues_rows, key=lambda row: row["amount"], reverse=True)[:5]

    orders_sales_total = sum(as_float(item.net_value) for item in ProgressPayment.query.all())
    direct_sales_total = max(total_journal_value - orders_sales_total, 0.0)
    sales_total = orders_sales_total + direct_sales_total
    orders_sales_ratio = (orders_sales_total / sales_total) if sales_total > 0 else 0.0
    direct_sales_ratio = (direct_sales_total / sales_total) if sales_total > 0 else 0.0

    journal_boards = [
        {
            "title": "مبيعات",
            "subtitle": "قيود الإيرادات والمستحقات",
            "count": ProgressPayment.query.count(),
            "total": sum(item.net_value for item in ProgressPayment.query.all()),
            "primary_action": {"label": "إنشاء مستخلص", "url": url_for("progress_payments")},
            "secondary_action": {"label": "عرض القيود", "url": url_for("journal")},
            "theme": "sales",
        },
        {
            "title": "مشتريات",
            "subtitle": "أوامر شراء وفواتير موردين",
            "count": purchase_orders,
            "total": sum(item.total_value for item in PurchaseOrder.query.all()),
            "primary_action": {"label": "أمر شراء جديد", "url": url_for("purchase_orders")},
            "secondary_action": {"label": "الموردون", "url": url_for("suppliers")},
            "theme": "purchase",
        },
        {
            "title": "بنك وصندوق",
            "subtitle": "حركة الأموال والسيولة",
            "count": journal_entries,
            "total": total_journal_value,
            "primary_action": {"label": "قيد يومية", "url": url_for("journal")},
            "secondary_action": {"label": "دليل الحسابات", "url": url_for("accounts")},
            "theme": "bank",
        },
        {
            "title": "عمليات",
            "subtitle": "المخزون، العمالة، المعدات",
            "count": inventory_transactions + labor_entries + equipment_items,
            "total": 0,
            "primary_action": {"label": "حركة مخزون", "url": url_for("inventory")},
            "secondary_action": {"label": "تقارير المشروع", "url": url_for("project_report")},
            "theme": "ops",
        },
    ]

    return render_template(
        "index.html",
        projects=projects,
        accounts=accounts,
        progress_payments=progress_payments,
        journal_entries=journal_entries,
        subcontractors=subcontractors,
        suppliers=suppliers,
        purchase_orders=purchase_orders,
        inventory_transactions=inventory_transactions,
        labor_entries=labor_entries,
        equipment_items=equipment_items,
        draft_purchase_orders=draft_purchase_orders,
        paid_purchase_orders=paid_purchase_orders,
        outstanding_payments=outstanding_payments,
        total_journal_value=total_journal_value,
        recent_entries=recent_entries,
        journal_boards=journal_boards,
        top_supplier_dues=top_supplier_dues,
        top_customer_dues=top_customer_dues,
        orders_sales_total=orders_sales_total,
        direct_sales_total=direct_sales_total,
        orders_sales_ratio=orders_sales_ratio,
        direct_sales_ratio=direct_sales_ratio,
    )


@app.route("/accounts", methods=["GET", "POST"])
def accounts():
    sync_journal_related_accounts()
    default_categories = [
        "الأصول",
        "معدات ثقيلة",
        "سيارات",
        "الالتزامات",
        "موردين",
        "مقاولي الباطن",
        "المصروفات",
        "مواد",
        "عمالة مباشرة",
        "إيجار معدات",
        "الإيرادات",
        "فروق أسعار",
        "الموردين",
        "العملاء",
        "المخازن",
        "المعدات",
        "الخزن الفرعية",
        "السواقين",
        "المشاريع",
    ]

    if request.method == "POST":
        code = (request.form.get("code") or "").strip()
        name = (request.form.get("name") or "").strip()
        if not code or not name:
            flash("يرجى إدخال كود الحساب واسم الحساب", "danger")
            return redirect(url_for("accounts"))

        account = ChartOfAccount(
            code=code,
            name=name,
            category=request.form.get("category"),
            project_id=as_int(request.form.get("project_id")),
            boq_item_id=as_int(request.form.get("boq_item_id")),
            stage=request.form.get("stage"),
            opening_balance=as_float(request.form.get("opening_balance")),
        )
        db.session.add(account)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash("كود الحساب مستخدم بالفعل، اختر كودًا مختلفًا", "danger")
            return redirect(url_for("accounts"))
        flash("تم إضافة الحساب بنجاح", "success")
        return redirect(url_for("accounts"))

    charts = ChartOfAccount.query.order_by(ChartOfAccount.category, ChartOfAccount.code).all()
    account_balances = build_account_balances(charts)
    treasury_balance = get_treasury_balance(charts, account_balances)
    projects = Project.query.order_by(Project.code).all()
    boq_items = BOQItem.query.order_by(BOQItem.name).all()
    existing_categories = [value[0] for value in db.session.query(ChartOfAccount.category).distinct().all() if (value[0] or "").strip()]
    category_options = list(dict.fromkeys(default_categories + existing_categories))
    return render_template(
        "accounts.html",
        charts=charts,
        projects=projects,
        boq_items=boq_items,
        category_options=category_options,
        account_balances=account_balances,
        treasury_balance=treasury_balance,
    )


@app.route("/accounts/<int:account_id>/update", methods=["POST"])
def update_account(account_id):
    account = ChartOfAccount.query.get_or_404(account_id)
    code = (request.form.get("code") or "").strip()
    name = (request.form.get("name") or "").strip()
    if not code or not name:
        flash("يرجى إدخال كود الحساب واسم الحساب", "danger")
        return redirect(url_for("accounts"))

    account.code = code
    account.name = name
    account.category = request.form.get("category")
    account.project_id = as_int(request.form.get("project_id"))
    account.boq_item_id = as_int(request.form.get("boq_item_id"))
    account.stage = request.form.get("stage")
    account.opening_balance = as_float(request.form.get("opening_balance"))
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash("كود الحساب مستخدم بالفعل، اختر كودًا مختلفًا", "danger")
        return redirect(url_for("accounts"))
    flash("تم تحديث الحساب بنجاح", "success")
    return redirect(url_for("accounts"))


@app.route("/accounts/<int:account_id>/delete", methods=["POST"])
def delete_account(account_id):
    account = ChartOfAccount.query.get_or_404(account_id)

    journal_refs = JournalEntry.query.filter(
        (JournalEntry.debit_account_id == account.id) |
        (JournalEntry.credit_account_id == account.id)
    ).count()
    custody_refs = CustodySettlement.query.filter(
        (CustodySettlement.entity_account_id == account.id) |
        (CustodySettlement.treasury_account_id == account.id)
    ).count()
    driver_refs = DriverCompensationEntry.query.filter(
        DriverCompensationEntry.treasury_account_id == account.id
    ).count()

    if journal_refs or custody_refs or driver_refs:
        details = []
        if journal_refs:
            details.append(f"قيود يومية: {journal_refs}")
        if custody_refs:
            details.append(f"تسويات عهد: {custody_refs}")
        if driver_refs:
            details.append(f"محاسبة سواقين: {driver_refs}")
        details_text = " - ".join(details)
        flash(f"لا يمكن حذف الحساب لأنه ما زال مرتبطًا بمستندات ({details_text})", "danger")
        return redirect(url_for("accounts"))

    db.session.delete(account)
    db.session.commit()
    flash("تم حذف الحساب بنجاح", "success")
    return redirect(url_for("accounts"))


@app.route("/projects", methods=["GET", "POST"])
def projects():
    if request.method == "POST":
        project = Project(
            code=request.form.get("code"),
            project_name=request.form.get("project_name"),
            client_name=request.form.get("client_name"),
            contract_value=as_float(request.form.get("contract_value")),
            start_date=request.form.get("start_date") or None,
            end_date=request.form.get("end_date") or None,
            contract_type=request.form.get("contract_type"),
        )
        db.session.add(project)
        db.session.commit()
        flash("تم إضافة المشروع بنجاح", "success")
        return redirect(url_for("projects"))

    items = Project.query.order_by(Project.start_date.desc()).all()
    return render_template("projects.html", items=items)


@app.route("/projects/<int:project_id>/update", methods=["POST"])
def update_project(project_id):
    project = Project.query.get_or_404(project_id)
    project.code = request.form.get("code")
    project.project_name = request.form.get("project_name")
    project.client_name = request.form.get("client_name")
    project.contract_value = as_float(request.form.get("contract_value"))
    project.start_date = request.form.get("start_date") or None
    project.end_date = request.form.get("end_date") or None
    project.contract_type = request.form.get("contract_type")
    db.session.commit()
    flash("تم تحديث المشروع بنجاح", "success")
    return redirect(url_for("projects"))


@app.route("/projects/<int:project_id>", methods=["GET", "POST"])
def project_detail(project_id):
    project = Project.query.get_or_404(project_id)
    if request.method == "POST":
        boq_item = BOQItem(
            project_id=project.id,
            name=request.form.get("name"),
            estimated_cost=as_float(request.form.get("estimated_cost")),
            quantity=as_float(request.form.get("quantity")),
            execution_percentage=as_float(request.form.get("execution_percentage")),
            stage=request.form.get("stage"),
        )
        db.session.add(boq_item)
        db.session.commit()
        flash("تم إضافة بند الأعمال بنجاح", "success")
        return redirect(url_for("project_detail", project_id=project.id))

    boq_items = BOQItem.query.filter_by(project_id=project.id).all()
    costs = CostEntry.query.filter_by(project_id=project.id).all()
    purchase_orders = PurchaseOrder.query.filter_by(project_id=project.id).all()
    inventory_transactions = InventoryTransaction.query.filter_by(project_id=project.id).all()
    cost_summary = {
        "total_cost": sum(cost.amount for cost in costs),
        "materials": sum(cost.amount for cost in costs if cost.cost_type == "مواد"),
        "labor": sum(cost.amount for cost in costs if cost.cost_type == "عمالة"),
        "equipment": sum(cost.amount for cost in costs if cost.cost_type == "معدات"),
        "indirect": sum(cost.amount for cost in costs if cost.cost_type == "غير مباشرة"),
        "supervision": sum(cost.amount for cost in costs if cost.cost_type == "اشراف"),
        "overhead": sum(cost.amount for cost in costs if cost.cost_type == "مصاريف ادارية"),
    }
    return render_template(
        "project_detail.html",
        project=project,
        boq_items=boq_items,
        costs=costs,
        purchase_orders=purchase_orders,
        inventory_transactions=inventory_transactions,
        cost_summary=cost_summary,
    )


@app.route("/projects/<int:project_id>/boq/<int:boq_item_id>/update", methods=["POST"])
def update_boq_item(project_id, boq_item_id):
    project = Project.query.get_or_404(project_id)
    boq_item = BOQItem.query.filter_by(id=boq_item_id, project_id=project.id).first_or_404()
    boq_item.name = request.form.get("name")
    boq_item.estimated_cost = as_float(request.form.get("estimated_cost"))
    boq_item.quantity = as_float(request.form.get("quantity"))
    boq_item.execution_percentage = as_float(request.form.get("execution_percentage"))
    boq_item.stage = request.form.get("stage")
    db.session.commit()
    flash("تم تحديث بند الأعمال بنجاح", "success")
    return redirect(url_for("project_detail", project_id=project.id))


@app.route("/progress_payments", methods=["GET", "POST"])
def progress_payments():
    projects = Project.query.order_by(Project.code).all()
    subcontractors = Subcontractor.query.order_by(Subcontractor.name).all()
    boq_items = BOQItem.query.order_by(BOQItem.name).all()
    if request.method == "POST":
        project_id = as_int(request.form.get("project_id"))
        subcontractor_id = as_int(request.form.get("subcontractor_id"))
        if not project_id:
            flash("يرجى اختيار مشروع صحيح", "danger")
            return redirect(url_for("progress_payments"))
        payment = ProgressPayment(
            project_id=project_id,
            subcontractor_id=subcontractor_id,
            period_start=request.form.get("period_start") or None,
            period_end=request.form.get("period_end") or None,
            discount_insurance=as_float(request.form.get("discount_insurance")),
            tax=as_float(request.form.get("tax")),
            penalties=as_float(request.form.get("penalties")),
            notes=request.form.get("notes"),
        )
        db.session.add(payment)
        db.session.commit()

        total_value = 0
        for index, boq_item_id in enumerate(request.form.getlist("boq_item_id")):
            try:
                quantity = as_float(request.form.getlist("quantity")[index])
                value = as_float(request.form.getlist("value")[index])
            except (ValueError, IndexError):
                quantity = 0
                value = 0
            if boq_item_id and value > 0:
                item = ProgressPaymentItem(
                    progress_payment_id=payment.id,
                    boq_item_id=as_int(boq_item_id),
                    description=request.form.getlist("description")[index],
                    quantity=quantity,
                    value=value,
                )
                total_value += value
                db.session.add(item)
        payment.total_value = total_value
        payment.net_value = total_value - payment.discount_insurance - payment.tax - payment.penalties
        if payment.net_value < 0:
            payment.net_value = 0
        db.session.commit()

        # قيد محاسبي تلقائي
        debit_account = ChartOfAccount.query.filter(ChartOfAccount.category == "المصروفات").first()
        credit_account = ChartOfAccount.query.filter(ChartOfAccount.category == "الالتزامات").first()
        if debit_account and credit_account:
            journal = JournalEntry(
                date=date.today().isoformat(),
                reference=f"PP-{payment.id:06d}",
                journal_name="متنوع",
                status="مرحل",
                description=f"مستخلص تقدم للمشروع {payment.project.code}",
                debit_account_id=debit_account.id,
                credit_account_id=credit_account.id,
                amount=payment.net_value,
                project_id=payment.project_id,
            )
            db.session.add(journal)
            db.session.commit()

        flash("تم إضافة المستخلص بنجاح وتم إنشاء قيد محاسبي تلقائي", "success")
        return redirect(url_for("progress_payments"))

    payments = ProgressPayment.query.order_by(ProgressPayment.id.desc()).all()
    return render_template(
        "progress_payments.html",
        payments=payments,
        projects=projects,
        subcontractors=subcontractors,
        boq_items=boq_items,
    )


@app.route("/progress_payments/<int:payment_id>/update", methods=["POST"])
def update_progress_payment(payment_id):
    payment = ProgressPayment.query.get_or_404(payment_id)
    payment.project_id = as_int(request.form.get("project_id")) or payment.project_id
    payment.subcontractor_id = as_int(request.form.get("subcontractor_id"))
    payment.period_start = request.form.get("period_start") or None
    payment.period_end = request.form.get("period_end") or None
    payment.discount_insurance = as_float(request.form.get("discount_insurance"))
    payment.tax = as_float(request.form.get("tax"))
    payment.penalties = as_float(request.form.get("penalties"))
    payment.notes = request.form.get("notes")
    payment.net_value = max(payment.total_value - payment.discount_insurance - payment.tax - payment.penalties, 0)
    db.session.commit()
    flash("تم تحديث المستخلص بنجاح", "success")
    return redirect(url_for("progress_payments"))


@app.route("/subcontractors", methods=["GET", "POST"])
def subcontractors():
    if request.method == "POST":
        subcontractor = Subcontractor(
            name=request.form.get("name"),
            contract_value=as_float(request.form.get("contract_value")),
            discount_percentage=as_float(request.form.get("discount_percentage")),
            notes=request.form.get("notes"),
        )
        db.session.add(subcontractor)
        db.session.commit()
        flash("تم إضافة مقاول الباطن بنجاح", "success")
        return redirect(url_for("subcontractors"))
    items = Subcontractor.query.order_by(Subcontractor.name).all()
    return render_template("subcontractors.html", items=items)


@app.route("/subcontractors/<int:subcontractor_id>/update", methods=["POST"])
def update_subcontractor(subcontractor_id):
    item = Subcontractor.query.get_or_404(subcontractor_id)
    item.name = request.form.get("name")
    item.contract_value = as_float(request.form.get("contract_value"))
    item.discount_percentage = as_float(request.form.get("discount_percentage"))
    item.notes = request.form.get("notes")
    db.session.commit()
    flash("تم تحديث مقاول الباطن بنجاح", "success")
    return redirect(url_for("subcontractors"))


@app.route("/suppliers", methods=["GET", "POST"])
def suppliers():
    sync_journal_related_accounts()
    if request.method == "POST":
        supplier = Supplier(
            name=request.form.get("name"),
            contact_info=request.form.get("contact_info"),
            notes=request.form.get("notes"),
        )
        db.session.add(supplier)
        db.session.commit()
        flash("تم إضافة مورد بنجاح", "success")
        return redirect(url_for("suppliers"))
    items = Supplier.query.order_by(Supplier.name).all()
    return render_template("suppliers.html", items=items)


@app.route("/suppliers/<int:supplier_id>/statement")
def supplier_statement(supplier_id):
    sync_journal_related_accounts()
    supplier = Supplier.query.get_or_404(supplier_id)
    supplier_account = ChartOfAccount.query.filter_by(code=f"SUP-{supplier.id:04d}").first()

    supplier_orders = PurchaseOrder.query.filter_by(supplier_id=supplier.id).order_by(PurchaseOrder.date.desc(), PurchaseOrder.id.desc()).all()
    total_orders_value = sum(as_float(item.total_value) for item in supplier_orders)
    total_due_value = total_orders_value
    total_paid_value = 0
    supplier_balance = total_due_value - total_paid_value

    journal_movements = []
    if supplier_account:
        linked_entries = JournalEntry.query.filter(
            (JournalEntry.debit_account_id == supplier_account.id) |
            (JournalEntry.credit_account_id == supplier_account.id)
        ).order_by(JournalEntry.date.desc(), JournalEntry.id.desc()).all()

        total_journal_due = 0
        total_journal_paid = 0
        payment_keywords = ["دفع", "دفعة", "سداد", "تحويل", "صرف", "مدفوع"]

        for entry in linked_entries:
            debit = as_float(entry.amount) if entry.debit_account_id == supplier_account.id else 0
            credit = as_float(entry.amount) if entry.credit_account_id == supplier_account.id else 0
            entry_text = " ".join(
                [
                    (entry.description or ""),
                    (entry.reference or ""),
                    (entry.journal_name or ""),
                ]
            )
            is_payment_entry = (debit > 0) or any(keyword in entry_text for keyword in payment_keywords)
            if is_payment_entry:
                total_journal_paid += as_float(entry.amount)
            else:
                total_journal_due += credit
            journal_movements.append(
                {
                    "date": entry.date,
                    "source": "قيد يومية",
                    "reference": entry.reference or f"JRN-{entry.id:06d}",
                    "description": entry.description,
                    "project": entry.project.display_name if entry.project else "-",
                    "debit": debit,
                    "credit": credit,
                }
            )

        # إجمالي المستحق والمدفوع يعتمد على حركات حساب المورد نفسه.
        # نستخدم الأعلى بين أوامر الشراء والدائن المحاسبي حتى لا نخسر أي التزام مسجل يدويًا.
        total_due_value = max(total_orders_value, total_journal_due)
        total_paid_value = total_journal_paid
        supplier_balance = total_due_value - total_paid_value

    purchase_movements = [
        {
            "date": order.date,
            "source": "أمر شراء",
            "reference": order.order_number or order.invoice_number or f"PO-{order.id:06d}",
            "description": f"{order.item_name or 'صنف غير محدد'} - {order.status or 'غير محدد'}",
            "project": order.project.display_name if order.project else "-",
            "debit": 0,
            "credit": as_float(order.total_value),
        }
        for order in supplier_orders
    ]

    movements = sorted(
        purchase_movements + journal_movements,
        key=lambda item: ((item.get("date") or ""), item.get("reference") or ""),
        reverse=True,
    )

    return render_template(
        "supplier_statement.html",
        supplier=supplier,
        supplier_account=supplier_account,
        supplier_orders=supplier_orders,
        movements=movements,
        total_orders_value=total_orders_value,
        total_due_value=total_due_value,
        total_paid_value=total_paid_value,
        supplier_balance=supplier_balance,
    )


@app.route("/suppliers/<int:supplier_id>/update", methods=["POST"])
def update_supplier(supplier_id):
    item = Supplier.query.get_or_404(supplier_id)
    item.name = request.form.get("name")
    item.contact_info = request.form.get("contact_info")
    item.notes = request.form.get("notes")
    db.session.commit()
    flash("تم تحديث المورد بنجاح", "success")
    return redirect(url_for("suppliers"))


@app.route("/purchase_orders", methods=["GET", "POST"])
def purchase_orders():
    projects = Project.query.order_by(Project.code).all()
    suppliers = Supplier.query.order_by(Supplier.name).all()
    material_names = get_material_names()
    warehouse_names = get_warehouse_names()
    if request.method == "POST":
        quantity = as_float(request.form.get("quantity"))
        unit_price = as_float(request.form.get("unit_price"))
        discount = as_float(request.form.get("discount"))
        total_value = calculate_purchase_order_total(quantity, unit_price, discount)
        order = PurchaseOrder(
            project_id=as_int(request.form.get("project_id")),
            supplier_id=as_int(request.form.get("supplier_id")),
            item_name=request.form.get("item_name"),
            warehouse_name=request.form.get("warehouse_name"),
            quantity=quantity,
            unit_price=unit_price,
            discount=discount,
            order_number=request.form.get("order_number"),
            invoice_number=request.form.get("invoice_number"),
            date=request.form.get("date") or None,
            status=request.form.get("status"),
            total_value=total_value,
            notes=request.form.get("notes"),
        )
        db.session.add(order)
        db.session.commit()

        # حركة مخزون تلقائية: أي أمر شراء يضيف الكمية للمخزون.
        sync_purchase_order_to_inventory(order)

        # قيد أمر الشراء يتم مزامنته تلقائيًا (إنشاء/تحديث لنفس القيد).
        sync_purchase_order_journal(order)

        flash("تم حفظ أمر الشراء بنجاح", "success")
        return redirect(url_for("purchase_orders"))
    items = PurchaseOrder.query.order_by(PurchaseOrder.date.desc()).all()
    return render_template(
        "purchase_orders.html",
        items=items,
        projects=projects,
        suppliers=suppliers,
        material_names=material_names,
        warehouse_names=warehouse_names,
    )


@app.route("/purchase_orders/<int:order_id>/update", methods=["POST"])
def update_purchase_order(order_id):
    order = PurchaseOrder.query.get_or_404(order_id)
    order.project_id = as_int(request.form.get("project_id")) or order.project_id
    order.supplier_id = as_int(request.form.get("supplier_id"))
    order.item_name = request.form.get("item_name")
    order.warehouse_name = request.form.get("warehouse_name")
    order.quantity = as_float(request.form.get("quantity"))
    order.unit_price = as_float(request.form.get("unit_price"))
    order.discount = as_float(request.form.get("discount"))
    order.order_number = request.form.get("order_number")
    order.invoice_number = request.form.get("invoice_number")
    order.date = request.form.get("date") or None
    order.status = request.form.get("status")
    order.total_value = calculate_purchase_order_total(order.quantity, order.unit_price, order.discount)
    order.notes = request.form.get("notes")
    db.session.commit()

    # مزامنة حركة المخزون التلقائية عند تعديل أمر الشراء.
    sync_purchase_order_to_inventory(order)

    # مزامنة قيد أمر الشراء التلقائي عند التعديل.
    sync_purchase_order_journal(order)

    flash("تم تحديث أمر الشراء بنجاح", "success")
    return redirect(url_for("purchase_orders"))


@app.route("/inventory", methods=["GET", "POST"])
def inventory():
    projects = Project.query.order_by(Project.code).all()
    suppliers = Supplier.query.order_by(Supplier.name).all()
    material_names = get_material_names()
    if request.method == "POST":
        destination_warehouse = request.form.get("destination_warehouse") or ""
        project_id = as_int(request.form.get("project_id"))
        if not project_id:
            flash("يرجى اختيار مشروع صحيح", "danger")
            return redirect(url_for("inventory"))
        transaction = InventoryTransaction(
            project_id=project_id,
            supplier_id=as_int(request.form.get("supplier_id")),
            warehouse_name=request.form.get("warehouse_name"),
            material_name=request.form.get("material_name"),
            quantity=as_float(request.form.get("quantity")),
            unit_cost=as_float(request.form.get("unit_cost")),
            transaction_type=request.form.get("transaction_type"),
            date=request.form.get("date") or None,
            notes=request.form.get("notes") or "",
        )
        if request.form.get("transaction_type") == "تحويل" and destination_warehouse:
            transaction.destination_warehouse = destination_warehouse
            transaction.notes = f"تحويل إلى {destination_warehouse} " + transaction.notes
        db.session.add(transaction)
        db.session.commit()

        # قيد محاسبي تلقائي لحركة المخزون
        amount = transaction.quantity * transaction.unit_cost
        if amount > 0:
            if transaction.transaction_type == "إضافة":
                debit_account = ChartOfAccount.query.filter(ChartOfAccount.category.in_(["مواد", "الأصول"])).first()
                credit_account = ChartOfAccount.query.filter(ChartOfAccount.category == "موردين").first()
            elif transaction.transaction_type == "سحب":
                debit_account = ChartOfAccount.query.filter(ChartOfAccount.category == "المصروفات").first()
                credit_account = ChartOfAccount.query.filter(ChartOfAccount.category.in_(["مواد", "الأصول"])).first()
            else:
                debit_account = None
                credit_account = None

            if debit_account and credit_account:
                journal = JournalEntry(
                    date=transaction.date or date.today().isoformat(),
                    reference=f"INV-{transaction.id:06d}",
                    journal_name="تقييم المخزون",
                    stock_move=transaction.transaction_type,
                    status="مرحل",
                    description=f"حركة مخزون {transaction.transaction_type} للمادة {transaction.material_name}",
                    debit_account_id=debit_account.id,
                    credit_account_id=credit_account.id,
                    amount=amount,
                    project_id=transaction.project_id,
                )
                db.session.add(journal)
                db.session.commit()

        flash("تم تسجيل حركة المخزون بنجاح", "success")
        return redirect(url_for("inventory"))
    items = InventoryTransaction.query.order_by(InventoryTransaction.date.desc()).all()
    return render_template("inventory.html", items=items, projects=projects, suppliers=suppliers, material_names=material_names)


@app.route("/inventory/<int:transaction_id>/update", methods=["POST"])
def update_inventory_transaction(transaction_id):
    item = InventoryTransaction.query.get_or_404(transaction_id)
    item.project_id = as_int(request.form.get("project_id")) or item.project_id
    item.supplier_id = as_int(request.form.get("supplier_id"))
    item.warehouse_name = request.form.get("warehouse_name")
    item.destination_warehouse = request.form.get("destination_warehouse") or None
    item.material_name = request.form.get("material_name")
    item.quantity = as_float(request.form.get("quantity"))
    item.unit_cost = as_float(request.form.get("unit_cost"))
    item.transaction_type = request.form.get("transaction_type")
    item.date = request.form.get("date") or None
    item.notes = request.form.get("notes")
    db.session.commit()
    flash("تم تحديث حركة المخزون بنجاح", "success")
    return redirect(url_for("inventory"))


@app.route("/inventory_report")
def inventory_report():
    transactions = InventoryTransaction.query.order_by(InventoryTransaction.material_name, InventoryTransaction.date).all()
    material_reports = []
    grouped = defaultdict(lambda: {
        "project": None,
        "material_name": None,
        "receipt_qty": 0,
        "receipt_value": 0,
        "issue_qty": 0,
        "issue_value": 0,
        "transfer_qty": 0,
        "fifo_remaining": deque(),
        "fifo_cost": 0,
    })

    for tx in transactions:
        key = (tx.project_id, tx.material_name)
        group = grouped[key]
        if group["project"] is None:
            group["project"] = tx.project
            group["material_name"] = tx.material_name
        if tx.transaction_type == "إضافة":
            group["receipt_qty"] += tx.quantity
            group["receipt_value"] += tx.quantity * tx.unit_cost
            group["fifo_remaining"].append([tx.quantity, tx.unit_cost])
        elif tx.transaction_type in ("سحب", "تحويل"):
            group["issue_qty"] += tx.quantity
            group["issue_value"] += tx.quantity * tx.unit_cost
            if tx.transaction_type == "تحويل":
                group["transfer_qty"] += tx.quantity
            quantity = tx.quantity
            while quantity > 0 and group["fifo_remaining"]:
                lot = group["fifo_remaining"][0]
                if quantity >= lot[0]:
                    group["fifo_cost"] += lot[0] * lot[1]
                    quantity -= lot[0]
                    group["fifo_remaining"].popleft()
                else:
                    group["fifo_cost"] += quantity * lot[1]
                    lot[0] -= quantity
                    quantity = 0

    for key, group in grouped.items():
        closing_qty = group["receipt_qty"] - group["issue_qty"]
        avg_cost = group["receipt_value"] / group["receipt_qty"] if group["receipt_qty"] else 0
        closing_value_avg = closing_qty * avg_cost
        fifo_value = sum(lot[0] * lot[1] for lot in group["fifo_remaining"])
        material_reports.append({
            "project": group["project"],
            "material_name": group["material_name"],
            "receipt_qty": group["receipt_qty"],
            "receipt_value": group["receipt_value"],
            "issue_qty": group["issue_qty"],
            "issue_value": group["issue_value"],
            "closing_qty": closing_qty,
            "closing_value_avg": closing_value_avg,
            "closing_value_fifo": fifo_value,
            "avg_cost": avg_cost,
        })
    return render_template("inventory_report.html", material_reports=material_reports)


@app.route("/reports/general_ledger")
def general_ledger_report():
    from_date = (request.args.get("from_date") or "").strip()
    to_date = (request.args.get("to_date") or "").strip()

    accounts = ChartOfAccount.query.order_by(ChartOfAccount.code, ChartOfAccount.name).all()
    account_map = {account.id: account for account in accounts}
    entries = get_journal_entries_in_range(from_date or None, to_date or None)

    totals_by_account = defaultdict(lambda: {"debit": 0.0, "credit": 0.0})
    movements_by_account = defaultdict(list)

    for entry in entries:
        amount = as_float(entry.amount)

        totals_by_account[entry.debit_account_id]["debit"] += amount
        movements_by_account[entry.debit_account_id].append(
            {
                "date": entry.date,
                "reference": entry.reference or f"JRN-{entry.id:06d}",
                "description": entry.description,
                "project": entry.project.display_name if entry.project else "-",
                "debit": amount,
                "credit": 0.0,
                "status": entry.status,
            }
        )

        totals_by_account[entry.credit_account_id]["credit"] += amount
        movements_by_account[entry.credit_account_id].append(
            {
                "date": entry.date,
                "reference": entry.reference or f"JRN-{entry.id:06d}",
                "description": entry.description,
                "project": entry.project.display_name if entry.project else "-",
                "debit": 0.0,
                "credit": amount,
                "status": entry.status,
            }
        )

    ledger_accounts = []
    for account in accounts:
        account_total = totals_by_account.get(account.id)
        account_movements = movements_by_account.get(account.id, [])
        if not account_total and not account_movements:
            continue

        opening_balance = as_float(getattr(account, "opening_balance", 0))
        debit_total = (account_total or {}).get("debit", 0.0)
        credit_total = (account_total or {}).get("credit", 0.0)
        closing_balance = opening_balance + debit_total - credit_total

        ledger_accounts.append(
            {
                "account": account,
                "opening_balance": opening_balance,
                "debit_total": debit_total,
                "credit_total": credit_total,
                "closing_balance": closing_balance,
                "movements": sorted(account_movements, key=lambda x: (x["date"] or "", x["reference"] or "")),
            }
        )

    return render_template(
        "general_ledger.html",
        ledger_accounts=ledger_accounts,
        from_date=from_date,
        to_date=to_date,
    )


@app.route("/reports/trial_balance")
def trial_balance_report():
    from_date = (request.args.get("from_date") or "").strip()
    to_date = (request.args.get("to_date") or "").strip()
    accounts = ChartOfAccount.query.order_by(ChartOfAccount.code, ChartOfAccount.name).all()
    entries = get_journal_entries_in_range(from_date or None, to_date or None)

    period_totals = defaultdict(lambda: {"debit": 0.0, "credit": 0.0})
    for entry in entries:
        amount = as_float(entry.amount)
        period_totals[entry.debit_account_id]["debit"] += amount
        period_totals[entry.credit_account_id]["credit"] += amount

    rows = []
    total_debit_balance = 0.0
    total_credit_balance = 0.0
    for account in accounts:
        opening_balance = as_float(getattr(account, "opening_balance", 0))
        period_debit = period_totals[account.id]["debit"]
        period_credit = period_totals[account.id]["credit"]
        closing_balance = opening_balance + period_debit - period_credit

        debit_balance = closing_balance if closing_balance > 0 else 0.0
        credit_balance = abs(closing_balance) if closing_balance < 0 else 0.0

        if abs(opening_balance) < 0.000001 and abs(period_debit) < 0.000001 and abs(period_credit) < 0.000001 and abs(closing_balance) < 0.000001:
            continue

        total_debit_balance += debit_balance
        total_credit_balance += credit_balance
        rows.append(
            {
                "account": account,
                "opening_balance": opening_balance,
                "period_debit": period_debit,
                "period_credit": period_credit,
                "closing_balance": closing_balance,
                "debit_balance": debit_balance,
                "credit_balance": credit_balance,
            }
        )

    return render_template(
        "trial_balance.html",
        rows=rows,
        from_date=from_date,
        to_date=to_date,
        total_debit_balance=total_debit_balance,
        total_credit_balance=total_credit_balance,
    )


@app.route("/reports/profit_loss")
def profit_loss_report():
    from_date = (request.args.get("from_date") or "").strip()
    to_date = (request.args.get("to_date") or "").strip()
    entries = get_journal_entries_in_range(from_date or None, to_date or None)
    accounts = ChartOfAccount.query.order_by(ChartOfAccount.code, ChartOfAccount.name).all()
    account_map = {account.id: account for account in accounts}

    revenue_categories = {"الإيرادات", "فروق أسعار"}
    expense_categories = {"المصروفات", "مواد", "عمالة مباشرة", "إيجار معدات", "مقاولي الباطن"}
    net_by_account = defaultdict(float)

    for entry in entries:
        amount = as_float(entry.amount)
        net_by_account[entry.debit_account_id] += amount
        net_by_account[entry.credit_account_id] -= amount

    revenue_lines = []
    expense_lines = []
    revenue_total = 0.0
    expense_total = 0.0

    for account_id, net_amount in net_by_account.items():
        account = account_map.get(account_id)
        if not account:
            continue
        if account.category in revenue_categories:
            line_amount = max(-net_amount, 0.0)
            if line_amount > 0:
                revenue_total += line_amount
                revenue_lines.append({"account": account, "amount": line_amount})
        elif account.category in expense_categories:
            line_amount = max(net_amount, 0.0)
            if line_amount > 0:
                expense_total += line_amount
                expense_lines.append({"account": account, "amount": line_amount})

    net_profit = revenue_total - expense_total

    return render_template(
        "profit_loss.html",
        revenue_lines=sorted(revenue_lines, key=lambda x: (x["account"].code, x["account"].name)),
        expense_lines=sorted(expense_lines, key=lambda x: (x["account"].code, x["account"].name)),
        revenue_total=revenue_total,
        expense_total=expense_total,
        net_profit=net_profit,
        from_date=from_date,
        to_date=to_date,
    )


@app.route("/reports/treasury_dynamics")
def treasury_dynamics_report():
    from_date = (request.args.get("from_date") or "").strip()
    to_date = (request.args.get("to_date") or "").strip()
    accounts = ChartOfAccount.query.order_by(ChartOfAccount.code, ChartOfAccount.name).all()
    balances = build_account_balances(accounts)
    main_treasury_account, sub_treasury_accounts = split_treasury_accounts(accounts)
    treasury_accounts = ([main_treasury_account] if main_treasury_account else []) + sub_treasury_accounts
    entries = get_journal_entries_in_range(from_date or None, to_date or None)

    treasury_ids = {account.id for account in treasury_accounts}
    movements = []
    total_inflow = 0.0
    total_outflow = 0.0
    for entry in entries:
        amount = as_float(entry.amount)
        debit_is_treasury = entry.debit_account_id in treasury_ids
        credit_is_treasury = entry.credit_account_id in treasury_ids
        if not debit_is_treasury and not credit_is_treasury:
            continue

        # التحويل الداخلي بين خزنتين لا يغير السيولة الكلية، لذلك لا يُضاف لإجماليات المقبوض/المدفوع.
        is_internal_transfer = debit_is_treasury and credit_is_treasury
        inflow = 0.0 if is_internal_transfer else (amount if debit_is_treasury else 0.0)
        outflow = 0.0 if is_internal_transfer else (amount if credit_is_treasury else 0.0)
        total_inflow += inflow
        total_outflow += outflow
        movements.append(
            {
                "date": entry.date,
                "reference": entry.reference or f"JRN-{entry.id:06d}",
                "description": f"{entry.description} (تحويل داخلي)" if is_internal_transfer else entry.description,
                "debit_account": entry.debit_account.name if entry.debit_account else "-",
                "credit_account": entry.credit_account.name if entry.credit_account else "-",
                "inflow": inflow,
                "outflow": outflow,
                "project": entry.project.display_name if entry.project else "-",
            }
        )

    main_own_balance = balances.get(main_treasury_account.id, 0.0) if main_treasury_account else 0.0
    sub_total_balance = sum(balances.get(account.id, 0.0) for account in sub_treasury_accounts)
    main_rollup_balance = main_own_balance + sub_total_balance

    treasury_rows = [
        {
            "account": account,
            "balance": balances.get(account.id, 0.0),
            "is_rollup": False,
        }
        for account in treasury_accounts
    ]
    if main_treasury_account:
        treasury_rows.insert(
            0,
            {
                "account": main_treasury_account,
                "balance": main_rollup_balance,
                "is_rollup": True,
            },
        )

    return render_template(
        "treasury_dynamics.html",
        treasury_rows=treasury_rows,
        movements=movements,
        from_date=from_date,
        to_date=to_date,
        total_inflow=total_inflow,
        total_outflow=total_outflow,
        net_cash_flow=total_inflow - total_outflow,
        main_rollup_balance=main_rollup_balance,
        main_own_balance=main_own_balance,
        sub_total_balance=sub_total_balance,
    )


@app.route("/reports/entity_accounts")
def entity_accounts_report():
    from_date = (request.args.get("from_date") or "").strip()
    to_date = (request.args.get("to_date") or "").strip()
    accounts = ChartOfAccount.query.order_by(ChartOfAccount.category, ChartOfAccount.code).all()
    balances = build_account_balances(accounts)
    entries = get_journal_entries_in_range(from_date or None, to_date or None)

    tracked_categories = ["المعدات", "السواقين", "الموردين", "العملاء"]
    tracked_ids = {account.id for account in accounts if account.category in tracked_categories}
    period_totals = defaultdict(lambda: {"debit": 0.0, "credit": 0.0})

    for entry in entries:
        amount = as_float(entry.amount)
        if entry.debit_account_id in tracked_ids:
            period_totals[entry.debit_account_id]["debit"] += amount
        if entry.credit_account_id in tracked_ids:
            period_totals[entry.credit_account_id]["credit"] += amount

    grouped_rows = defaultdict(list)
    for account in accounts:
        if account.category not in tracked_categories:
            continue
        grouped_rows[account.category].append(
            {
                "account": account,
                "period_debit": period_totals[account.id]["debit"],
                "period_credit": period_totals[account.id]["credit"],
                "current_balance": balances.get(account.id, 0.0),
            }
        )

    return render_template(
        "entity_accounts_report.html",
        grouped_rows=grouped_rows,
        from_date=from_date,
        to_date=to_date,
    )


@app.route("/reports/aging")
def aging_report():
    from_date = (request.args.get("from_date") or "").strip()
    to_date = (request.args.get("to_date") or "").strip()
    entries = get_journal_entries_in_range(from_date or None, to_date or None)
    accounts = ChartOfAccount.query.filter(ChartOfAccount.category.in_(["الموردين", "العملاء"]))\
        .order_by(ChartOfAccount.category, ChartOfAccount.code).all()
    account_map = {account.id: account for account in accounts}

    rows_map = {
        account.id: {
            "account": account,
            "current": 0.0,
            "30": 0.0,
            "60": 0.0,
            "90": 0.0,
            "total": 0.0,
        }
        for account in accounts
    }

    for entry in entries:
        amount = as_float(entry.amount)
        bucket = get_age_bucket(entry.date)

        if entry.debit_account_id in rows_map:
            account = account_map[entry.debit_account_id]
            sign = 1.0 if account.category == "العملاء" else -1.0
            rows_map[entry.debit_account_id][bucket] += amount * sign

        if entry.credit_account_id in rows_map:
            account = account_map[entry.credit_account_id]
            sign = 1.0 if account.category == "الموردين" else -1.0
            rows_map[entry.credit_account_id][bucket] += amount * sign

    grouped_rows = defaultdict(list)
    for account_id, row in rows_map.items():
        row["total"] = row["current"] + row["30"] + row["60"] + row["90"]
        grouped_rows[row["account"].category].append(row)

    return render_template(
        "aging_report.html",
        grouped_rows=grouped_rows,
        from_date=from_date,
        to_date=to_date,
    )


@app.route("/reports/balance_sheet")
def balance_sheet_report():
    accounts = ChartOfAccount.query.order_by(ChartOfAccount.code, ChartOfAccount.name).all()
    balances = build_account_balances(accounts)

    assets = []
    liabilities = []
    equity = []
    for account in accounts:
        balance = balances.get(account.id, 0.0)
        section = classify_balance_sheet_section(account)
        row = {"account": account, "balance": balance}
        if section == "assets":
            assets.append(row)
        elif section == "liabilities":
            liabilities.append(row)
        elif section == "equity":
            equity.append(row)

    total_assets = sum(row["balance"] for row in assets)
    total_liabilities = sum(abs(row["balance"]) for row in liabilities)
    total_equity = sum(row["balance"] for row in equity)
    if abs(total_equity) < 0.000001:
        total_equity = total_assets - total_liabilities

    return render_template(
        "balance_sheet.html",
        assets=assets,
        liabilities=liabilities,
        equity=equity,
        total_assets=total_assets,
        total_liabilities=total_liabilities,
        total_equity=total_equity,
    )


@app.route("/reports/cash_flow")
def cash_flow_report():
    from_date = (request.args.get("from_date") or "").strip()
    to_date = (request.args.get("to_date") or "").strip()
    accounts = ChartOfAccount.query.order_by(ChartOfAccount.code, ChartOfAccount.name).all()
    treasury_ids = {account.id for account in accounts if is_treasury_account(account)}
    entries = get_journal_entries_in_range(from_date or None, to_date or None)

    operating_lines = []
    inflow_total = 0.0
    outflow_total = 0.0
    for entry in entries:
        amount = as_float(entry.amount)
        if entry.debit_account_id in treasury_ids:
            inflow_total += amount
            operating_lines.append(
                {
                    "date": entry.date,
                    "reference": entry.reference or f"JRN-{entry.id:06d}",
                    "description": entry.description,
                    "inflow": amount,
                    "outflow": 0.0,
                }
            )
        if entry.credit_account_id in treasury_ids:
            outflow_total += amount
            operating_lines.append(
                {
                    "date": entry.date,
                    "reference": entry.reference or f"JRN-{entry.id:06d}",
                    "description": entry.description,
                    "inflow": 0.0,
                    "outflow": amount,
                }
            )

    net_operating_cash = inflow_total - outflow_total
    return render_template(
        "cash_flow.html",
        operating_lines=operating_lines,
        inflow_total=inflow_total,
        outflow_total=outflow_total,
        net_operating_cash=net_operating_cash,
        from_date=from_date,
        to_date=to_date,
    )


@app.route("/custody_settlements", methods=["GET", "POST"])
def custody_settlements():
    sync_journal_related_accounts()
    projects = Project.query.order_by(Project.code).all()
    accounts = ChartOfAccount.query.order_by(ChartOfAccount.category, ChartOfAccount.code).all()
    treasury_accounts = [account for account in accounts if is_treasury_account(account)]
    default_treasury_account_id = treasury_accounts[0].id if treasury_accounts else None
    entity_accounts = get_custody_entity_accounts(accounts)
    expense_accounts = get_custody_expense_accounts(accounts, entity_accounts)
    operation_filter = (request.args.get("operation_type") or "").strip()
    from_date = (request.args.get("from_date") or "").strip()
    to_date = (request.args.get("to_date") or "").strip()
    search_name = (request.args.get("search_name") or request.args.get("entity_name") or "").strip()

    if request.method == "POST":
        operation_type = (request.form.get("operation_type") or request.form.get("voucher_type") or "صرف عهدة").strip()
        settlement_lines = parse_custody_settlement_lines(request.form)
        amount = as_float(request.form.get("amount"))
        if operation_type == "تسوية عهدة":
            amount = sum(line["amount"] for line in settlement_lines)
        if amount <= 0:
            flash("يرجى إدخال مبلغ أكبر من صفر", "danger")
            return redirect(url_for("custody_settlements"))

        entity_name = (request.form.get("entity_name") or "").strip()
        entity_account_id = as_int(request.form.get("entity_account_id"))
        entity_account = ChartOfAccount.query.get(entity_account_id) if entity_account_id else None
        if not entity_account and entity_name:
            entity_account = ChartOfAccount.query.filter_by(name=entity_name).first()
            if entity_account:
                entity_account_id = entity_account.id
        if not entity_account_id:
            flash("يرجى اختيار اسم صاحب عهدة من الحسابات المتاحة", "danger")
            return redirect(url_for("custody_settlements", operation_type=operation_type))

        entity_type = (request.form.get("entity_type") or "").strip()
        if not entity_type and entity_account:
            entity_type = "سائق" if (entity_account.code or "").startswith("DRV-") or "سائق" in (entity_account.name or "") else "معدة"
        if not entity_type:
            entity_type = "سائق"
        if not entity_name and entity_account:
            entity_name = (entity_account.name or "").strip()

        settlement = CustodySettlement(
            date=request.form.get("date") or date.today().isoformat(),
            project_id=as_int(request.form.get("project_id")),
            entity_type=entity_type,
            entity_name=entity_name,
            expense_item=(request.form.get("expense_item") or "").strip() or None,
            voucher_type="صرف" if operation_type != "رد باقي عهدة" else "رد",
            operation_type=operation_type,
            reference=(request.form.get("reference") or "").strip() or None,
            treasury_account_id=as_int(request.form.get("treasury_account_id")) or default_treasury_account_id,
            entity_account_id=entity_account_id,
            amount=amount,
            settlement_lines=json.dumps(settlement_lines, ensure_ascii=False),
            notes=request.form.get("notes"),
        )
        db.session.add(settlement)
        db.session.commit()
        sync_custody_settlement_journal(settlement)
        flash("تم حفظ مستند العهد بنجاح", "success")
        return redirect(url_for(
            "custody_settlements",
            search_name=(settlement.entity_name or "").strip(),
            operation_type=operation_type,
        ))

    all_items = CustodySettlement.query.order_by(CustodySettlement.date.asc(), CustodySettlement.id.asc()).all()
    all_accounts = ChartOfAccount.query.order_by(ChartOfAccount.category, ChartOfAccount.code).all()

    active_entity_account = None
    if search_name:
        search_lower = search_name.lower()
        active_entity_account = next((account for account in all_accounts if search_lower in (account.name or "").lower()), None)

    account_balances = build_account_balances(all_accounts)
    all_owner_rows = build_custody_owner_rows(all_accounts, all_items, account_balances)
    selected_owner = None
    if active_entity_account:
        selected_owner = next((row for row in all_owner_rows if row["account"].id == active_entity_account.id), None)

    selected_entity_name = selected_owner["entity_name"] if selected_owner else search_name
    selected_entity_type = selected_owner["entity_type"] if selected_owner else "سائق"

    selected_rows = []
    if selected_owner:
        running_balance = 0.0
        for item in selected_owner["settlements"]:
            settlement = item["settlement"]
            op_type = item["operation_type"]
            if operation_filter and operation_filter != op_type:
                continue
            if from_date and (settlement.date or "") < from_date:
                continue
            if to_date and (settlement.date or "") > to_date:
                continue

            amount = as_float(item["amount"])
            debit = amount if op_type == "صرف عهدة" else 0.0
            credit = amount if op_type in ("تسوية عهدة", "رد باقي عهدة") else 0.0
            running_balance += debit - credit

            selected_rows.append({
                "item": settlement,
                "operation_type": op_type,
                "amount": amount,
                "debit": debit,
                "credit": credit,
                "running_balance": running_balance,
            })

    selected_totals = {
        "spent": sum(row["amount"] for row in selected_rows if row["operation_type"] == "صرف عهدة"),
        "returned": sum(row["amount"] for row in selected_rows if row["operation_type"] == "رد باقي عهدة"),
        "balance": selected_owner["balance"] if selected_owner else 0.0,
        "count": len(selected_rows),
    }

    owner_summaries = all_owner_rows
    all_entity_names = [account.name for account in all_accounts]

    return render_template(
        "custody_settlements.html",
        items=all_items,
        owner_summaries=owner_summaries,
        selected_rows=selected_rows,
        selected_entity_type=selected_entity_type,
        selected_entity_name=selected_entity_name,
        selected_totals=selected_totals,
        all_entity_names=all_entity_names,
        projects=projects,
        treasury_accounts=treasury_accounts,
        entity_accounts=entity_accounts,
        expense_accounts=expense_accounts,
        operation_filter=operation_filter,
        from_date=from_date,
        to_date=to_date,
        accounts=accounts,
    )


@app.route("/custody_settlements/export")
def export_custody_settlements():
    sync_journal_related_accounts()
    accounts = ChartOfAccount.query.order_by(ChartOfAccount.category, ChartOfAccount.code).all()
    search_name = (request.args.get("search_name") or request.args.get("entity_name") or "").strip()
    operation_filter = (request.args.get("operation_type") or "").strip()
    from_date = (request.args.get("from_date") or "").strip()
    to_date = (request.args.get("to_date") or "").strip()

    all_items = CustodySettlement.query.order_by(CustodySettlement.date.asc(), CustodySettlement.id.asc()).all()
    account_balances = build_account_balances(accounts)
    owner_rows = build_custody_owner_rows(accounts, all_items, account_balances)
    search_lower = search_name.lower()
    active_entity_account = next((account for account in accounts if search_lower in (account.name or "").lower()), None)
    selected_owner = None
    if active_entity_account:
        selected_owner = next((row for row in owner_rows if row["account"].id == active_entity_account.id), None)

    selected_rows = []
    if selected_owner:
        running_balance = 0.0
        for item in selected_owner["settlements"]:
            settlement = item["settlement"]
            op_type = item["operation_type"]
            if operation_filter and operation_filter != op_type:
                continue
            if from_date and (settlement.date or "") < from_date:
                continue
            if to_date and (settlement.date or "") > to_date:
                continue

            amount = as_float(item["amount"])
            debit = amount if op_type == "صرف عهدة" else 0.0
            credit = amount if op_type in ("تسوية عهدة", "رد باقي عهدة") else 0.0
            running_balance += debit - credit

            selected_rows.append({
                "settlement": settlement,
                "operation_type": op_type,
                "debit": debit,
                "credit": credit,
                "running_balance": running_balance,
            })

    final_balance = selected_owner["balance"] if selected_owner else 0.0

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "كشف العهد"

    # Header info
    sheet.append(["كشف حساب العهدة"])
    sheet.append([f"صاحب العهدة: {search_name or 'كل الحسابات'}"])
    sheet.append([f"نوع العملية: {operation_filter or 'كل العمليات'}"])
    sheet.append([f"الفترة: {from_date or '-'} إلى {to_date or '-'}"])
    sheet.append([])

    header_row = 6
    headers = ["رقم العملية", "نوع العملية", "المرجع", "التاريخ", "اسم صاحب العهدة", "المدين", "الدائن", "الرصيد", "ملاحظات"]
    sheet.append(headers)

    for row in selected_rows:
        settlement = row["settlement"]
        sheet.append([
            f"CUS-{settlement.id:06d}",
            row["operation_type"],
            settlement.reference or "-",
            settlement.date or "-",
            settlement.entity_name or "-",
            row["debit"],
            row["credit"],
            row["running_balance"],
            settlement.notes or "-",
        ])

    total_row_index = sheet.max_row + 1
    sheet.append(["", "", "", "", "إجمالي الرصيد الفعلي للحساب بعد كل الحركات المالية", "", "", final_balance, ""])

    # Styling
    title_cell = sheet.cell(row=1, column=1)
    title_cell.font = Font(bold=True, size=14)
    title_cell.alignment = Alignment(horizontal="right")

    header_fill = PatternFill("solid", fgColor="D9E1F2")
    for col in range(1, len(headers) + 1):
        cell = sheet.cell(row=header_row, column=col)
        cell.font = Font(bold=True)
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center")

    for row_idx in range(header_row + 1, total_row_index):
        sheet.cell(row=row_idx, column=6).number_format = "#,##0.00"
        sheet.cell(row=row_idx, column=7).number_format = "#,##0.00"
        sheet.cell(row=row_idx, column=8).number_format = "#,##0.00"

    for col, width in {
        1: 16,
        2: 16,
        3: 18,
        4: 14,
        5: 28,
        6: 14,
        7: 14,
        8: 14,
        9: 30,
    }.items():
        sheet.column_dimensions[chr(64 + col)].width = width

    sheet.freeze_panes = "A7"

    if selected_rows:
        table_end_row = total_row_index - 1
        table_ref = f"A{header_row}:I{table_end_row}"
        table = Table(displayName="CustodyStatementTable", ref=table_ref)
        table.tableStyleInfo = TableStyleInfo(
            name="TableStyleMedium2",
            showFirstColumn=False,
            showLastColumn=False,
            showRowStripes=True,
            showColumnStripes=False,
        )
        sheet.add_table(table)

    total_fill = PatternFill("solid", fgColor="FFF2CC")
    for col in range(1, len(headers) + 1):
        cell = sheet.cell(row=total_row_index, column=col)
        cell.fill = total_fill
        if col in (5, 8):
            cell.font = Font(bold=True)
    sheet.cell(row=total_row_index, column=8).number_format = "#,##0.00"

    output = BytesIO()
    workbook.save(output)
    output.seek(0)
    filename = f"custody_statement_{(search_name or 'all').replace(' ', '_')}.xlsx"
    return send_file(
        output,
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.route("/custody_settlements/<int:settlement_id>/update", methods=["POST"])
def update_custody_settlement(settlement_id):
    settlement = CustodySettlement.query.get_or_404(settlement_id)
    operation_type = (request.form.get("operation_type") or request.form.get("voucher_type") or settlement.operation_type or "صرف عهدة").strip()
    settlement_lines = parse_custody_settlement_lines(request.form)
    amount = as_float(request.form.get("amount"))
    if operation_type == "تسوية عهدة":
        amount = sum(line["amount"] for line in settlement_lines)
    if amount <= 0:
        flash("يرجى إدخال مبلغ أكبر من صفر", "danger")
        return redirect(url_for("custody_settlements"))

    settlement.date = request.form.get("date") or settlement.date
    settlement.project_id = as_int(request.form.get("project_id"))
    settlement.entity_type = request.form.get("entity_type") or settlement.entity_type
    settlement.entity_name = (request.form.get("entity_name") or "").strip()
    settlement.expense_item = (request.form.get("expense_item") or "").strip() or None
    settlement.voucher_type = "صرف" if operation_type != "رد باقي عهدة" else "رد"
    settlement.operation_type = operation_type
    settlement.reference = (request.form.get("reference") or "").strip() or None
    settlement.treasury_account_id = as_int(request.form.get("treasury_account_id")) or settlement.treasury_account_id
    settlement.entity_account_id = as_int(request.form.get("entity_account_id")) or settlement.entity_account_id
    settlement.amount = amount
    settlement.settlement_lines = json.dumps(settlement_lines, ensure_ascii=False)
    settlement.notes = request.form.get("notes")
    db.session.commit()
    sync_custody_settlement_journal(settlement)
    flash("تم تحديث مستند العهد بنجاح", "success")
    return redirect(url_for("custody_settlements"))


@app.route("/costs", methods=["GET", "POST"])
def costs():
    projects = Project.query.order_by(Project.code).all()
    boq_items = BOQItem.query.order_by(BOQItem.name).all()
    if request.method == "POST":
        cost_entry = CostEntry(
            project_id=as_int(request.form.get("project_id")),
            boq_item_id=as_int(request.form.get("boq_item_id")),
            cost_type=request.form.get("cost_type"),
            amount=as_float(request.form.get("amount")),
            description=request.form.get("description"),
            cost_center=request.form.get("cost_center"),
        )
        db.session.add(cost_entry)
        db.session.commit()
        flash("تم إضافة قيد تكلفة بنجاح", "success")
        return redirect(url_for("costs"))
    entries = CostEntry.query.order_by(CostEntry.id.desc()).all()
    return render_template("costs.html", entries=entries, projects=projects, boq_items=boq_items)


@app.route("/costs/<int:cost_id>/update", methods=["POST"])
def update_cost_entry(cost_id):
    entry = CostEntry.query.get_or_404(cost_id)
    entry.project_id = as_int(request.form.get("project_id")) or entry.project_id
    entry.boq_item_id = as_int(request.form.get("boq_item_id"))
    entry.cost_type = request.form.get("cost_type")
    entry.amount = as_float(request.form.get("amount"))
    entry.description = request.form.get("description")
    entry.cost_center = request.form.get("cost_center")
    db.session.commit()
    flash("تم تحديث قيد التكلفة بنجاح", "success")
    return redirect(url_for("costs"))


@app.route("/project_report")
def project_report():
    projects = Project.query.order_by(Project.code).all()
    project_summaries = []
    for project in projects:
        total_cost = sum(cost.amount for cost in project.cost_entries)
        project_summaries.append({
            "project": project,
            "total_cost": total_cost,
            "labour_cost": sum(cost.amount for cost in project.cost_entries if cost.cost_type == "عمالة"),
            "material_cost": sum(cost.amount for cost in project.cost_entries if cost.cost_type == "مواد"),
            "equipment_cost": sum(cost.amount for cost in project.cost_entries if cost.cost_type == "معدات"),
            "indirect_cost": sum(cost.amount for cost in project.cost_entries if cost.cost_type == "غير مباشرة"),
            "supervision_cost": sum(cost.amount for cost in project.cost_entries if cost.cost_type == "اشراف"),
            "overhead_cost": sum(cost.amount for cost in project.cost_entries if cost.cost_type == "مصاريف ادارية"),
        })
    boq_items = BOQItem.query.order_by(BOQItem.name).all()
    item_summaries = []
    for item in boq_items:
        item_summaries.append({
            "item": item,
            "total_cost": sum(cost.amount for cost in item.cost_entries),
            "quantity": item.quantity,
            "cost_per_unit": sum(cost.amount for cost in item.cost_entries) / item.quantity if item.quantity else 0,
        })
    return render_template("project_report.html", project_summaries=project_summaries, item_summaries=item_summaries)


@app.route("/labor", methods=["GET", "POST"])
def labor():
    projects = Project.query.order_by(Project.code).all()
    if request.method == "POST":
        labor = LaborEntry(
            project_id=as_int(request.form.get("project_id")),
            date=request.form.get("date") or None,
            description=request.form.get("description"),
            hours=as_float(request.form.get("hours")),
            amount=as_float(request.form.get("amount")),
            advances=as_float(request.form.get("advances")),
            deductions=as_float(request.form.get("deductions")),
        )
        db.session.add(labor)
        db.session.commit()
        flash("تم تسجيل العمالة بنجاح", "success")
        return redirect(url_for("labor"))
    entries = LaborEntry.query.order_by(LaborEntry.date.desc()).all()
    return render_template("labor.html", entries=entries, projects=projects)


@app.route("/driver_compensation", methods=["GET", "POST"])
def driver_compensation():
    sync_journal_related_accounts()
    projects = Project.query.order_by(Project.code).all()
    accounts = ChartOfAccount.query.order_by(ChartOfAccount.category, ChartOfAccount.code).all()
    treasury_accounts = [account for account in accounts if is_treasury_account(account)]

    if request.method == "POST":
        driver_name = (request.form.get("driver_name") or "").strip()
        settlement_basis = request.form.get("settlement_basis") or "يومية"
        units = as_float(request.form.get("units"))
        unit_rate = as_float(request.form.get("unit_rate"))
        paid_amount = as_float(request.form.get("paid_amount"))
        gross_amount = max(units * unit_rate, 0.0)
        treasury_account_id = as_int(request.form.get("treasury_account_id"))

        if not driver_name:
            flash("يرجى إدخال اسم السائق", "danger")
            return redirect(url_for("driver_compensation"))
        if settlement_basis not in ("يومية", "نقلة"):
            settlement_basis = "يومية"
        if units < 0 or unit_rate < 0:
            flash("لا يمكن إدخال قيم سالبة للأيام/النقلات أو السعر", "danger")
            return redirect(url_for("driver_compensation"))
        if paid_amount < 0:
            flash("لا يمكن إدخال قيمة سداد سالبة", "danger")
            return redirect(url_for("driver_compensation"))
        if gross_amount <= 0 and paid_amount <= 0:
            flash("يرجى إدخال استحقاق أو سداد بقيمة أكبر من صفر", "danger")
            return redirect(url_for("driver_compensation"))
        if paid_amount > 0 and not treasury_account_id:
            flash("يرجى اختيار حساب الخزنة عند إدخال مبلغ مسدد", "danger")
            return redirect(url_for("driver_compensation"))

        entry = DriverCompensationEntry(
            date=request.form.get("date") or date.today().isoformat(),
            project_id=as_int(request.form.get("project_id")),
            driver_name=driver_name,
            settlement_basis=settlement_basis,
            units=units,
            unit_rate=unit_rate,
            gross_amount=gross_amount,
            paid_amount=paid_amount,
            treasury_account_id=treasury_account_id,
            reference=(request.form.get("reference") or "").strip() or None,
            notes=request.form.get("notes"),
        )
        db.session.add(entry)
        db.session.commit()
        sync_driver_compensation_journals(entry)
        flash("تم حفظ حركة السائق بنجاح", "success")
        return redirect(url_for("driver_compensation"))

    from_date = (request.args.get("from_date") or "").strip()
    to_date = (request.args.get("to_date") or "").strip()
    driver_filter = (request.args.get("driver_name") or "").strip()

    query = DriverCompensationEntry.query
    if from_date:
        query = query.filter(DriverCompensationEntry.date >= from_date)
    if to_date:
        query = query.filter(DriverCompensationEntry.date <= to_date)
    if driver_filter:
        query = query.filter(DriverCompensationEntry.driver_name == driver_filter)
    entries = query.order_by(DriverCompensationEntry.date.desc(), DriverCompensationEntry.id.desc()).all()

    all_driver_names = sorted({
        (value[0] or "").strip()
        for value in DriverCompensationEntry.query.with_entities(DriverCompensationEntry.driver_name).all()
        if (value[0] or "").strip()
    })

    summary_map = defaultdict(lambda: {
        "driver_name": "",
        "daily_units": 0.0,
        "trip_units": 0.0,
        "gross_total": 0.0,
        "paid_total": 0.0,
        "due_total": 0.0,
    })
    for item in entries:
        row = summary_map[item.driver_name]
        row["driver_name"] = item.driver_name
        if item.settlement_basis == "يومية":
            row["daily_units"] += as_float(item.units)
        else:
            row["trip_units"] += as_float(item.units)
        row["gross_total"] += as_float(item.gross_amount)
        row["paid_total"] += as_float(item.paid_amount)
        row["due_total"] = row["gross_total"] - row["paid_total"]

    summary_rows = sorted(summary_map.values(), key=lambda x: x["driver_name"])
    totals = {
        "gross": sum(row["gross_total"] for row in summary_rows),
        "paid": sum(row["paid_total"] for row in summary_rows),
        "due": sum(row["due_total"] for row in summary_rows),
    }

    return render_template(
        "driver_compensation.html",
        entries=entries,
        summary_rows=summary_rows,
        totals=totals,
        projects=projects,
        treasury_accounts=treasury_accounts,
        all_driver_names=all_driver_names,
        from_date=from_date,
        to_date=to_date,
        driver_filter=driver_filter,
    )


@app.route("/driver_compensation/weekly", methods=["POST"])
def driver_compensation_weekly():
    sync_journal_related_accounts()
    driver_name = (request.form.get("driver_name") or "").strip()
    week_days = as_float(request.form.get("week_days"))
    daily_rate = as_float(request.form.get("daily_rate"))
    paid_amount = as_float(request.form.get("paid_amount"))
    treasury_account_id = as_int(request.form.get("treasury_account_id"))
    gross_amount = max(week_days * daily_rate, 0.0)

    if not driver_name:
        flash("يرجى إدخال اسم السائق للتسوية الأسبوعية", "danger")
        return redirect(url_for("driver_compensation"))
    if week_days <= 0 or daily_rate < 0:
        flash("يرجى إدخال عدد أيام وسعر يومية صحيحين", "danger")
        return redirect(url_for("driver_compensation"))
    if paid_amount < 0:
        flash("لا يمكن إدخال مبلغ سداد سالب", "danger")
        return redirect(url_for("driver_compensation"))
    if paid_amount > 0 and not treasury_account_id:
        flash("يرجى اختيار حساب الخزنة عند إدخال مبلغ مسدد", "danger")
        return redirect(url_for("driver_compensation"))

    entry = DriverCompensationEntry(
        date=request.form.get("date") or date.today().isoformat(),
        project_id=as_int(request.form.get("project_id")),
        driver_name=driver_name,
        settlement_basis="يومية",
        units=week_days,
        unit_rate=daily_rate,
        gross_amount=gross_amount,
        paid_amount=paid_amount,
        treasury_account_id=treasury_account_id,
        reference=(request.form.get("reference") or "").strip() or None,
        notes=(request.form.get("notes") or "").strip() or "تسوية أسبوعية",
    )
    db.session.add(entry)
    db.session.commit()
    sync_driver_compensation_journals(entry)
    flash("تم حفظ التسوية الأسبوعية للسائق بنجاح", "success")
    return redirect(url_for("driver_compensation"))


@app.route("/driver_compensation/<int:entry_id>/update", methods=["POST"])
def update_driver_compensation(entry_id):
    entry = DriverCompensationEntry.query.get_or_404(entry_id)
    driver_name = (request.form.get("driver_name") or "").strip()
    settlement_basis = request.form.get("settlement_basis") or entry.settlement_basis
    units = as_float(request.form.get("units"))
    unit_rate = as_float(request.form.get("unit_rate"))
    paid_amount = as_float(request.form.get("paid_amount"))
    gross_amount = max(units * unit_rate, 0.0)
    treasury_account_id = as_int(request.form.get("treasury_account_id"))

    if not driver_name:
        flash("يرجى إدخال اسم السائق", "danger")
        return redirect(url_for("driver_compensation"))
    if settlement_basis not in ("يومية", "نقلة"):
        settlement_basis = "يومية"
    if units < 0 or unit_rate < 0 or paid_amount < 0:
        flash("الرجاء إدخال قيم صحيحة أكبر أو تساوي صفر", "danger")
        return redirect(url_for("driver_compensation"))
    if paid_amount > 0 and not treasury_account_id:
        flash("يرجى اختيار حساب الخزنة عند إدخال مبلغ مسدد", "danger")
        return redirect(url_for("driver_compensation"))

    entry.date = request.form.get("date") or entry.date
    entry.project_id = as_int(request.form.get("project_id"))
    entry.driver_name = driver_name
    entry.settlement_basis = settlement_basis
    entry.units = units
    entry.unit_rate = unit_rate
    entry.gross_amount = gross_amount
    entry.paid_amount = paid_amount
    entry.treasury_account_id = treasury_account_id
    entry.reference = (request.form.get("reference") or "").strip() or None
    entry.notes = request.form.get("notes")
    db.session.commit()
    sync_driver_compensation_journals(entry)
    flash("تم تحديث حركة السائق بنجاح", "success")
    return redirect(url_for("driver_compensation"))


@app.route("/labor/<int:labor_id>/update", methods=["POST"])
def update_labor_entry(labor_id):
    entry = LaborEntry.query.get_or_404(labor_id)
    entry.project_id = as_int(request.form.get("project_id")) or entry.project_id
    entry.date = request.form.get("date") or None
    entry.description = request.form.get("description")
    entry.hours = as_float(request.form.get("hours"))
    entry.amount = as_float(request.form.get("amount"))
    entry.advances = as_float(request.form.get("advances"))
    entry.deductions = as_float(request.form.get("deductions"))
    db.session.commit()
    flash("تم تحديث سجل العمالة بنجاح", "success")
    return redirect(url_for("labor"))


@app.route("/equipment", methods=["GET", "POST"])
def equipment():
    projects = Project.query.order_by(Project.code).all()
    if request.method == "POST":
        equip = Equipment(
            name=request.form.get("name"),
            purchase_cost=as_float(request.form.get("purchase_cost")),
            operating_cost=as_float(request.form.get("operating_cost")),
            maintenance=as_float(request.form.get("maintenance")),
            hours_used=as_float(request.form.get("hours_used")),
            project_id=as_int(request.form.get("project_id")),
        )
        db.session.add(equip)
        db.session.commit()
        flash("تم حفظ بيانات المعدات بنجاح", "success")
        return redirect(url_for("equipment"))
    entries = Equipment.query.order_by(Equipment.name).all()
    return render_template("equipment.html", entries=entries, projects=projects)


@app.route("/equipment/<int:equipment_id>/update", methods=["POST"])
def update_equipment(equipment_id):
    item = Equipment.query.get_or_404(equipment_id)
    item.name = request.form.get("name")
    item.purchase_cost = as_float(request.form.get("purchase_cost"))
    item.operating_cost = as_float(request.form.get("operating_cost"))
    item.maintenance = as_float(request.form.get("maintenance"))
    item.hours_used = as_float(request.form.get("hours_used"))
    item.project_id = as_int(request.form.get("project_id"))
    db.session.commit()
    flash("تم تحديث بيانات المعدة بنجاح", "success")
    return redirect(url_for("equipment"))


@app.route("/journal", methods=["GET", "POST"])
def journal():
    sync_journal_related_accounts()
    if request.method == "POST":
        entry_action = request.form.get("entry_action") or "draft"
        entry_date = request.form.get("date") or date.today().isoformat()
        reference = (request.form.get("reference") or "").strip()
        description = (request.form.get("description") or "").strip()
        status = "مرحل" if entry_action == "post" else "مسودة"
        line_debit_ids = request.form.getlist("line_debit_account_id")
        line_credit_ids = request.form.getlist("line_credit_account_id")
        line_cost_centers = request.form.getlist("line_cost_center")
        line_amounts = request.form.getlist("line_amount")

        if not description:
            flash("يرجى إدخال وصف القيد", "danger")
            return redirect(url_for("journal"))

        parsed_lines = []
        max_len = max(len(line_debit_ids), len(line_credit_ids), len(line_cost_centers), len(line_amounts))
        for idx in range(max_len):
            debit_id = as_int(line_debit_ids[idx] if idx < len(line_debit_ids) else None) or 0
            credit_id = as_int(line_credit_ids[idx] if idx < len(line_credit_ids) else None) or 0
            cost_center = (line_cost_centers[idx] if idx < len(line_cost_centers) else "").strip() or None
            amount = as_float(line_amounts[idx] if idx < len(line_amounts) else 0)
            if debit_id <= 0 and credit_id <= 0 and amount <= 0:
                continue
            if debit_id <= 0 or credit_id <= 0:
                flash(f"يرجى اختيار الحساب المدين والدائن في البند رقم {idx + 1}", "danger")
                return redirect(url_for("journal"))
            if debit_id == credit_id:
                flash(f"لا يمكن تكرار نفس الحساب مدينًا ودائنًا في البند رقم {idx + 1}", "danger")
                return redirect(url_for("journal"))
            if amount <= 0:
                flash(f"يرجى إدخال مبلغ أكبر من صفر في البند رقم {idx + 1}", "danger")
                return redirect(url_for("journal"))
            parsed_lines.append({
                "debit_account_id": debit_id,
                "credit_account_id": credit_id,
                "cost_center": cost_center,
                "amount": amount,
            })

        if not parsed_lines:
            flash("يرجى إضافة بند قيد واحد على الأقل", "danger")
            return redirect(url_for("journal"))

        for line in parsed_lines:
            entry = JournalEntry(
                date=entry_date,
                reference=reference,
                journal_name="يومية عامة",
                branch=None,
                stock_move=None,
                status=status,
                description=description,
                debit_account_id=line["debit_account_id"],
                credit_account_id=line["credit_account_id"],
                amount=line["amount"],
                project_id=None,
                cost_center=line["cost_center"],
            )
            db.session.add(entry)

        db.session.commit()
        flash("تم حفظ القيد بنجاح", "success")
        return redirect(url_for("journal"))

    status_filter = request.args.get("status") or ""
    query = JournalEntry.query
    if status_filter in ("مسودة", "مرحل"):
        query = query.filter_by(status=status_filter)
    entries = query.order_by(JournalEntry.date.desc(), JournalEntry.id.desc()).all()
    accounts = ChartOfAccount.query.order_by(ChartOfAccount.code, ChartOfAccount.name).all()
    treasury_accounts = [account for account in accounts if is_operational_treasury_account(account)]
    treasury_ids = {account.id for account in treasury_accounts}
    non_treasury_accounts = [account for account in accounts if account.id not in treasury_ids]
    account_balances = build_account_balances(accounts)
    treasury_balance = get_treasury_balance(accounts, account_balances)
    projects = Project.query.order_by(Project.code).all()
    equipments = Equipment.query.order_by(Equipment.name).all()
    cost_centers = [f"مشروع - {project.display_name}" for project in projects]
    cost_centers += [f"معدة - {equipment.name}" for equipment in equipments if (equipment.name or "").strip()]
    # Keep order stable and unique.
    cost_centers = list(dict.fromkeys(cost_centers))
    latest_entry = JournalEntry.query.order_by(JournalEntry.id.desc()).first()
    next_journal_serial = (latest_entry.id + 1) if latest_entry else 1
    return render_template(
        "journal.html",
        entries=entries,
        accounts=accounts,
        treasury_accounts=treasury_accounts,
        non_treasury_accounts=non_treasury_accounts,
        account_balances=account_balances,
        treasury_balance=treasury_balance,
        projects=projects,
        journal_options=JOURNAL_OPTIONS,
        status_filter=status_filter,
        today_date=date.today().isoformat(),
        cost_centers=cost_centers,
        next_journal_serial=next_journal_serial,
    )


@app.route("/journal/<int:entry_id>")
def journal_entry_detail(entry_id):
    sync_journal_related_accounts()
    entry = JournalEntry.query.get_or_404(entry_id)
    accounts = ChartOfAccount.query.order_by(ChartOfAccount.code, ChartOfAccount.name).all()
    account_balances = build_account_balances(accounts)
    projects = Project.query.order_by(Project.code).all()
    return render_template("journal_entry_detail.html", entry=entry, accounts=accounts, account_balances=account_balances, projects=projects, journal_options=JOURNAL_OPTIONS)


@app.route("/journal/<int:entry_id>/update", methods=["POST"])
def update_journal_entry(entry_id):
    entry = JournalEntry.query.get_or_404(entry_id)

    debit_account_id = int(request.form.get("debit_account_id") or 0)
    credit_account_id = int(request.form.get("credit_account_id") or 0)
    amount = float(request.form.get("amount") or 0)
    entry_date = request.form.get("date") or entry.date
    entry.reference = (request.form.get("reference") or "").strip() or None
    entry.journal_name = (request.form.get("journal_name") or "يومية عامة").strip()
    entry.branch = (request.form.get("branch") or "").strip() or None
    entry.stock_move = (request.form.get("stock_move") or "").strip() or None
    entry.status = normalize_journal_status(request.form.get("status"))
    description = (request.form.get("description") or "").strip()
    project_id = request.form.get("project_id") or None

    if not description:
        flash("يرجى إدخال وصف القيد", "danger")
        return redirect(url_for("journal_entry_detail", entry_id=entry.id))
    if amount <= 0:
        flash("يرجى إدخال مبلغ أكبر من صفر", "danger")
        return redirect(url_for("journal_entry_detail", entry_id=entry.id))
    if debit_account_id <= 0 or credit_account_id <= 0:
        flash("يرجى اختيار الحساب المدين والدائن", "danger")
        return redirect(url_for("journal_entry_detail", entry_id=entry.id))
    if debit_account_id == credit_account_id:
        flash("لا يمكن أن يكون الحساب المدين هو نفسه الحساب الدائن", "danger")
        return redirect(url_for("journal_entry_detail", entry_id=entry.id))

    entry.date = entry_date
    entry.description = description
    entry.debit_account_id = debit_account_id
    entry.credit_account_id = credit_account_id
    entry.amount = amount
    entry.project_id = project_id
    db.session.commit()
    flash("تم تحديث القيد بنجاح", "success")
    return redirect(url_for("journal_entry_detail", entry_id=entry.id))


@app.route("/journal/<int:entry_id>/copy", methods=["POST"])
def copy_journal_entry(entry_id):
    source = JournalEntry.query.get_or_404(entry_id)
    copied_entry = JournalEntry(
        date=date.today().isoformat(),
        reference=f"COPY-{source.id:06d}",
        journal_name=source.journal_name,
        branch=source.branch,
        stock_move=source.stock_move,
        status="مسودة",
        description=f"نسخ من قيد رقم {source.id}: {source.description}",
        debit_account_id=source.debit_account_id,
        credit_account_id=source.credit_account_id,
        amount=source.amount,
        project_id=source.project_id,
    )
    db.session.add(copied_entry)
    db.session.commit()
    flash("تم نسخ القيد بنجاح", "success")
    return redirect(url_for("journal"))


@app.route("/journal/<int:entry_id>/reverse", methods=["POST"])
def reverse_journal_entry(entry_id):
    source = JournalEntry.query.get_or_404(entry_id)
    reversed_entry = JournalEntry(
        date=date.today().isoformat(),
        reference=f"REV-{source.id:06d}",
        journal_name=source.journal_name,
        branch=source.branch,
        stock_move=source.stock_move,
        status="مرحل",
        description=f"عكس قيد رقم {source.id}: {source.description}",
        debit_account_id=source.credit_account_id,
        credit_account_id=source.debit_account_id,
        amount=source.amount,
        project_id=source.project_id,
    )
    db.session.add(reversed_entry)
    db.session.commit()
    flash("تم إنشاء قيد عكسي بنجاح", "success")
    return redirect(url_for("journal"))


@app.route("/journal/<int:entry_id>/delete", methods=["POST"])
def delete_journal_entry(entry_id):
    entry = JournalEntry.query.get_or_404(entry_id)
    db.session.delete(entry)
    db.session.commit()
    flash("تم حذف القيد بنجاح", "success")
    return redirect(url_for("journal"))


@app.route("/journal/<int:entry_id>/post", methods=["POST"])
def post_journal_entry(entry_id):
    entry = JournalEntry.query.get_or_404(entry_id)
    entry.status = "مرحل"
    db.session.commit()
    flash("تم ترحيل القيد بنجاح", "success")
    return redirect(url_for("journal"))


@app.route("/journal/<int:entry_id>/unpost", methods=["POST"])
def unpost_journal_entry(entry_id):
    entry = JournalEntry.query.get_or_404(entry_id)
    entry.status = "مسودة"
    db.session.commit()
    flash("تم إرجاع القيد إلى مسودة", "success")
    return redirect(url_for("journal"))
