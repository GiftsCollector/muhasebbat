import os
import tempfile

import pytest

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix=".db")
os.close(_DB_FD)
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["DATABASE_URL"] = "sqlite:///" + _DB_PATH.replace("\\", "/")
os.environ.pop("RENDER", None)
os.environ["FLASK_ENV"] = "testing"

from app import app as flask_app
from models import (
    ROLE_ADMIN, ROLE_DATA_ENTRY, AccountingPeriod, ActivityLog, ChartOfAccount, Estimation, Equipment, JournalEntry,
    ProgressPayment, ProgressPaymentItem, Project, Subcontractor, Supplier, User, db, PurchaseOrder,
    InventoryTransaction, ClientReceipt, SubcontractorPayment, SupplierPayment, Employee,
    PayrollSlip, EmployeeAttendance, EmployeeSalaryPayment, SubcontractorRetentionRelease, SalesTrip,
    DriverCompensationEntry,
)
from services.accounting import (
    build_account_balances, build_project_cost_breakdown, build_subcontractor_statement,
    generate_progress_payment_number, get_account_by_code, get_or_create_employee_account,
    get_subcontractor_outstanding_advances, get_subcontractor_outstanding_retention,
    previous_executed_quantity, progress_net_remaining, project_revenue_and_result,
    sync_equipment_journals, sync_journal_related_accounts,
)


@pytest.fixture(autouse=True)
def _db():
    flask_app.config["TESTING"] = True
    flask_app.config["WTF_CSRF_ENABLED"] = False
    with flask_app.app_context():
        db.drop_all()
        db.create_all()
        sync_journal_related_accounts()
        admin = User(username="admin", full_name="مدير النظام", role=ROLE_ADMIN, is_active=True)
        admin.set_password("secret12")
        db.session.add(admin)
        db.session.commit()
        yield
        db.session.remove()


@pytest.fixture()
def client():
    test_client = flask_app.test_client()
    with flask_app.app_context():
        admin = User.query.filter_by(username="admin").first()
        admin_id = admin.id
    with test_client.session_transaction() as sess:
        sess["user_id"] = admin_id
        sess["admin_amounts_visible"] = True
    return test_client


def _ids():
    treasury = ChartOfAccount.query.filter_by(code="TRS-MAIN").first()
    expense = ChartOfAccount.query.filter_by(code="EXP-ELC").first() or ChartOfAccount.query.filter_by(category="المصروفات").first()
    return treasury.id, expense.id


def test_client_sales_exclude_subcontractor_ipc(client):
    with flask_app.app_context():
        project = Project(
            code="PRJ-T1", project_name="برج", client_name="عميل أ",
            contract_type="مقاولات عامة", contract_value=1000000,
        )
        sub = Subcontractor(name="مقاول اختبار")
        db.session.add_all([project, sub])
        db.session.commit()
        pid, sid = project.id, sub.id

    client.post("/progress_payments", data={
        "project_id": str(pid),
        "subcontractor_id": str(sid),
        "date": "2026-08-01",
        "retention_percentage": "0",
        "tax_percentage": "0",
        "description": ["خرسانة"],
        "unit": ["متر مكعب"],
        "quantity": ["10"],
        "unit_price": ["1000"],
    }, follow_redirects=True)

    client.post("/client_progress_payments", data={
        "project_id": str(pid),
        "date": "2026-08-02",
        "retention_percentage": "0",
        "tax_percentage": "0",
        "description": ["أعمال للعميل"],
        "unit": ["مقطوعية"],
        "quantity": ["1"],
        "unit_price": ["50000"],
    }, follow_redirects=True)

    home = client.get("/")
    body = home.get_data(as_text=True)
    assert "50000" in body.replace(",", "").replace(".", "") or "50.000" in body
    with flask_app.app_context():
        assert ProgressPayment.query.filter_by(subcontractor_id=sid).count() == 1
        assert ProgressPayment.query.filter(ProgressPayment.subcontractor_id.is_(None)).count() == 1


def test_client_progress_payment_computes_line_total_and_net(client):
    with flask_app.app_context():
        project = Project(
            code="PRJ-CLI-TOT", project_name="نقل رمل", client_name="عميل إجمالي",
            contract_type="مقاولات عامة", contract_value=1,
        )
        db.session.add(project)
        db.session.commit()
        pid = project.id

    page = client.get("/client_progress_payments")
    body = page.get_data(as_text=True)
    assert "الإجمالي المستحق" in body
    assert "line-total" in body
    assert "ipc-line" in body
    assert "صافي المستحق على العميل (كشف الحساب)" in body

    client.post("/client_progress_payments", data={
        "project_id": str(pid),
        "date": "2026-08-04",
        "retention_percentage": "10",
        "tax_percentage": "0",
        "description": ["نقلة رمل", "يومية سائق"],
        "unit": ["نقلة", "يومية"],
        "quantity": ["5", "2"],
        "unit_price": ["1000", "500"],
    }, follow_redirects=True)

    with flask_app.app_context():
        payment = ProgressPayment.query.filter(ProgressPayment.subcontractor_id.is_(None)).one()
        items = ProgressPaymentItem.query.filter_by(progress_payment_id=payment.id).all()
        assert sorted(round(item.value, 2) for item in items) == [1000.0, 5000.0]
        assert round(payment.total_value, 2) == 6000.0
        assert round(payment.discount_insurance, 2) == 600.0
        assert round(payment.net_value, 2) == 5400.0


def test_estimation_does_not_post_revenue(client):
    with flask_app.app_context():
        project = Project(
            code="PRJ-T2", project_name="فيلا", client_name="عميل ب",
            contract_type="مقاولات عامة", contract_value=200000,
        )
        db.session.add(project)
        db.session.commit()
        pid = project.id

    client.post("/estimations", data={
        "client_name": "عميل ب",
        "project_id": str(pid),
        "date": "2026-08-01",
        "discount_percentage": "0",
        "admin_percentage": "0",
        "admin_mode": "إضافة",
        "item_description": ["بند"],
        "item_unit": ["مقطوعية"],
        "item_quantity": ["1"],
        "item_unit_price": ["80000"],
        "item_discount_percentage": ["0"],
    }, follow_redirects=True)

    with flask_app.app_context():
        estimation = Estimation.query.first()
        est_id = estimation.id

    client.post(f"/estimations/{est_id}/status", data={"status": "معتمدة"}, follow_redirects=True)
    with flask_app.app_context():
        autos = JournalEntry.query.filter(JournalEntry.description.like("%EST-AUTO:%")).all()
        assert autos == []
        revenue = ChartOfAccount.query.filter_by(code="REV-WRK").first()
        balances = build_account_balances()
        assert round(balances.get(revenue.id, 0), 2) == 0.0


def test_purchase_order_does_not_double_inventory(client):
    with flask_app.app_context():
        project = Project(
            code="PRJ-T3", project_name="مخزن", client_name="عميل ج",
            contract_type="مقاولات عامة", contract_value=1,
        )
        supplier = Supplier(name="مورد حديد")
        db.session.add_all([project, supplier])
        db.session.commit()
        pid, sid = project.id, supplier.id

    client.post("/purchase_orders", data={
        "project_id": str(pid),
        "supplier_id": str(sid),
        "item_name": "حديد",
        "warehouse_name": "رئيسي",
        "quantity": "10",
        "unit_price": "100",
        "discount": "0",
        "date": "2026-08-01",
        "status": "مفتوح",
    }, follow_redirects=True)

    with flask_app.app_context():
        order = PurchaseOrder.query.first()
        assert order is not None
        tx = InventoryTransaction.query.filter(InventoryTransaction.notes.like(f"%PO-AUTO:{order.id}%")).first()
        po_journals = JournalEntry.query.filter(JournalEntry.description.like(f"%PO-JRN-AUTO:{order.id}%")).all()
        assert tx is None
        assert po_journals == []
        oid = order.id

    client.post(f"/purchase_orders/{oid}/update", data={
        "project_id": str(pid),
        "supplier_id": str(sid),
        "item_name": "حديد",
        "warehouse_name": "رئيسي",
        "quantity": "10",
        "unit_price": "100",
        "discount": "0",
        "date": "2026-08-01",
        "status": "مغلق",
    }, follow_redirects=True)

    with flask_app.app_context():
        tx = InventoryTransaction.query.filter(InventoryTransaction.notes.like(f"%PO-AUTO:{oid}%")).first()
        assert tx is not None
        assert round(tx.quantity, 2) == 10
        inv_journals = JournalEntry.query.filter(JournalEntry.description.like(f"%INV-AUTO:{tx.id}%")).all()
        po_journals = JournalEntry.query.filter(JournalEntry.description.like(f"%PO-JRN-AUTO:{oid}%")).all()
        assert po_journals and round(po_journals[0].amount, 2) == 1000.0
        assert inv_journals == []


def test_inventory_transfer_and_stock_guard(client):
    with flask_app.app_context():
        project = Project(
            code="PRJ-T3B", project_name="مخزن ب", client_name="عميل",
            contract_type="مقاولات عامة", contract_value=1,
        )
        supplier = Supplier(name="مورد أسمنت")
        db.session.add_all([project, supplier])
        db.session.commit()
        pid, sid = project.id, supplier.id

    client.post("/purchase_orders", data={
        "project_id": str(pid),
        "supplier_id": str(sid),
        "item_name": ["أسمنت", "رمل"],
        "quantity": ["20", "5"],
        "unit_price": ["50", "10"],
        "discount": ["0", "0"],
        "warehouse_name": "رئيسي",
        "date": "2026-08-01",
        "status": "مغلق",
    }, follow_redirects=True)

    over_issue = client.post("/inventory", data={
        "project_id": str(pid),
        "material_name": "أسمنت",
        "warehouse_name": "رئيسي",
        "quantity": "21",
        "unit_cost": "50",
        "transaction_type": "سحب",
        "date": "2026-08-02",
    }, follow_redirects=True)
    assert "غير متاحة" in over_issue.get_data(as_text=True)

    client.post("/inventory", data={
        "project_id": str(pid),
        "material_name": "أسمنت",
        "warehouse_name": "رئيسي",
        "destination_warehouse": "موقع",
        "quantity": "8",
        "transaction_type": "تحويل",
        "date": "2026-08-03",
    }, follow_redirects=True)

    with flask_app.app_context():
        from services.accounting import available_stock_qty
        assert round(available_stock_qty(pid, "رئيسي", "أسمنت"), 2) == 12
        assert round(available_stock_qty(pid, "موقع", "أسمنت"), 2) == 8
        order = PurchaseOrder.query.first()
        oid = order.id

    deleted = client.post(f"/documents/purchase_order/{oid}/delete", follow_redirects=True)
    assert "لا يمكن تخفيض استلام" in deleted.get_data(as_text=True)
    with flask_app.app_context():
        assert PurchaseOrder.query.get(oid) is not None
        assert InventoryTransaction.query.filter_by(transaction_type="تحويل").count() == 1


def test_inventory_can_transfer_main_treasury_cash_to_another_treasury(client):
    with flask_app.app_context():
        project = Project(
            code="PRJ-TRS", project_name="تحويل خزنة", client_name="عميل",
            contract_type="مقاولات عامة", contract_value=1,
        )
        main = ChartOfAccount.query.filter_by(code="TRS-MAIN").first()
        site = ChartOfAccount.query.filter_by(code="TRS-SITE").first()
        assert main is not None and site is not None
        main.opening_balance = 8000
        db.session.add(project)
        db.session.commit()
        pid, main_id, site_id = project.id, main.id, site.id

    page = client.get("/inventory")
    body = page.get_data(as_text=True)
    assert page.status_code == 200
    assert f"acc:{main_id}" in body
    assert "الخزنة الرئيسية" in body
    assert "خزنة الموقع" in body

    over = client.post("/inventory", data={
        "project_id": str(pid),
        "warehouse_name": f"acc:{main_id}",
        "destination_warehouse": f"acc:{site_id}",
        "quantity": "9000",
        "transaction_type": "تحويل",
        "date": "2026-09-01",
    }, follow_redirects=True)
    assert over.status_code == 200
    assert "لا يكفي" in over.get_data(as_text=True)

    saved = client.post("/inventory", data={
        "project_id": str(pid),
        "warehouse_name": f"acc:{main_id}",
        "destination_warehouse": f"acc:{site_id}",
        "quantity": "2500",
        "transaction_type": "تحويل",
        "date": "2026-09-01",
        "material_name": "تحويل خزنة",
    }, follow_redirects=True)
    assert saved.status_code == 200
    assert "تم تسجيل حركة المخزون" in saved.get_data(as_text=True)

    with flask_app.app_context():
        from services.accounting import available_stock_qty, build_account_balances
        tx = InventoryTransaction.query.filter_by(transaction_type="تحويل", project_id=pid).first()
        assert tx is not None
        assert round(tx.quantity, 2) == 2500
        assert round(available_stock_qty(pid, "الخزنة الرئيسية", "تحويل خزنة"), 2) == 0
        balances = build_account_balances()
        assert round(balances.get(main_id, 0.0), 2) == 5500
        assert round(balances.get(site_id, 0.0), 2) == 2500
        journal = JournalEntry.query.filter(JournalEntry.description.like(f"%INV-AUTO:{tx.id}%")).first()
        assert journal is not None
        assert journal.debit_account_id == site_id
        assert journal.credit_account_id == main_id
        assert round(journal.amount, 2) == 2500


def test_inventory_treasury_to_other_account_posts_gl_not_stock(client):
    with flask_app.app_context():
        project = Project(
            code="PRJ-CASH", project_name="تحويل لحساب", client_name="عميل",
            contract_type="مقاولات عامة", contract_value=1,
        )
        main = ChartOfAccount.query.filter_by(code="TRS-MAIN").first()
        dest = (
            ChartOfAccount.query.filter_by(code="EXP-ELC").first()
            or ChartOfAccount.query.filter_by(category="المصروفات").first()
        )
        assert main is not None and dest is not None
        main.opening_balance = 5000
        db.session.add(project)
        db.session.commit()
        pid, main_id, dest_id = project.id, main.id, dest.id

    saved = client.post("/inventory", data={
        "project_id": str(pid),
        "warehouse_name": f"acc:{main_id}",
        "destination_warehouse": f"acc:{dest_id}",
        "quantity": "1200",
        "transaction_type": "تحويل",
        "date": "2026-09-02",
        "material_name": "تحويل خزنة",
    }, follow_redirects=True)
    assert saved.status_code == 200
    assert "تم تسجيل حركة المخزون" in saved.get_data(as_text=True)

    with flask_app.app_context():
        from services.accounting import available_stock_qty, build_account_balances
        tx = InventoryTransaction.query.filter_by(transaction_type="تحويل", project_id=pid).first()
        assert tx is not None
        assert round(available_stock_qty(pid, "الخزنة الرئيسية", "تحويل خزنة"), 2) == 0
        dest_account = ChartOfAccount.query.get(dest_id)
        assert dest_account is not None
        assert round(available_stock_qty(pid, dest_account.name, "تحويل خزنة"), 2) == 0
        balances = build_account_balances()
        assert round(balances.get(main_id, 0.0), 2) == 3800
        assert round(balances.get(dest_id, 0.0), 2) == 1200
        journal = JournalEntry.query.filter(JournalEntry.description.like(f"%INV-AUTO:{tx.id}%")).first()
        assert journal is not None
        assert journal.debit_account_id == dest_id
        assert journal.credit_account_id == main_id
        assert round(journal.amount, 2) == 1200


def test_inventory_label_picker_and_line_amount_post_money(client):
    with flask_app.app_context():
        project = Project(
            code="PRJ-CASH2", project_name="تحويل بالتسمية", client_name="عميل",
            contract_type="مقاولات عامة", contract_value=1,
        )
        main = ChartOfAccount.query.filter_by(code="TRS-MAIN").first()
        dest = (
            ChartOfAccount.query.filter_by(code="EXP-ELC").first()
            or ChartOfAccount.query.filter_by(category="المصروفات").first()
        )
        assert main is not None and dest is not None
        main.opening_balance = 8000
        db.session.add(project)
        db.session.commit()
        pid, main_id, dest_id = project.id, main.id, dest.id
        dest_label = f"{dest.code} — {dest.name}"

    saved = client.post("/inventory", data={
        "project_id": str(pid),
        "warehouse_name": "TRS-MAIN — الخزنة الرئيسية",
        "destination_warehouse": dest_label,
        "quantity": "10",
        "unit_cost": "0",
        "line_amount": "1500",
        "transaction_type": "تحويل",
        "date": "2026-09-03",
        "material_name": "",
    }, follow_redirects=True)
    assert saved.status_code == 200
    assert "تم تسجيل حركة المخزون" in saved.get_data(as_text=True)

    with flask_app.app_context():
        from services.accounting import available_stock_qty, build_account_balances
        tx = InventoryTransaction.query.filter_by(project_id=pid).first()
        assert tx is not None
        assert round(available_stock_qty(pid, "الخزنة الرئيسية", "تحويل خزنة"), 2) == 0
        balances = build_account_balances()
        assert round(balances.get(main_id, 0.0), 2) == 6500
        assert round(balances.get(dest_id, 0.0), 2) == 1500
        journal = JournalEntry.query.filter(JournalEntry.description.like(f"%INV-AUTO:{tx.id}%")).first()
        assert journal is not None
        assert round(journal.amount, 2) == 1500
        assert journal.credit_account_id == main_id
        assert journal.debit_account_id == dest_id


def test_inventory_stock_add_uses_line_amount_not_quantity(client):
    with flask_app.app_context():
        project = Project(
            code="PRJ-AMT", project_name="قيمة المخزون", client_name="عميل",
            contract_type="مقاولات عامة", contract_value=1,
        )
        db.session.add(project)
        db.session.commit()
        pid = project.id

    saved = client.post("/inventory", data={
        "project_id": str(pid),
        "warehouse_name": "رئيسي",
        "quantity": "10",
        "unit_cost": "0",
        "line_amount": "500",
        "transaction_type": "إضافة",
        "date": "2026-09-04",
        "material_name": "أسمنت",
    }, follow_redirects=True)
    assert saved.status_code == 200
    assert "تم تسجيل حركة المخزون" in saved.get_data(as_text=True)

    with flask_app.app_context():
        from services.accounting import build_account_balances, resolve_stock_account
        tx = InventoryTransaction.query.filter_by(project_id=pid).first()
        assert tx is not None
        assert round(as_float_safe := (tx.quantity or 0), 2) == 10
        assert round(tx.unit_cost, 2) == 50
        warehouse = resolve_stock_account(tx.warehouse_name, tx.warehouse_account_id)
        journal = JournalEntry.query.filter(JournalEntry.description.like(f"%INV-AUTO:{tx.id}%")).first()
        assert journal is not None
        assert round(journal.amount, 2) == 500
        assert journal.debit_account_id == warehouse.id
        balances = build_account_balances()
        assert round(balances.get(warehouse.id, 0.0), 2) == 500


def test_old_inventory_moves_backfill_cost_on_chart(client):
    with flask_app.app_context():
        project = Project(
            code="PRJ-OLDINV", project_name="حركات قديمة", client_name="عميل",
            contract_type="مقاولات عامة", contract_value=1,
        )
        main = ChartOfAccount.query.filter_by(code="TRS-MAIN").first()
        dest = (
            ChartOfAccount.query.filter_by(code="EXP-ELC").first()
            or ChartOfAccount.query.filter_by(category="المصروفات").first()
        )
        db.session.add(project)
        db.session.commit()
        pid, main_id, dest_id = project.id, main.id, dest.id
        dest_name = dest.name
        stock = InventoryTransaction(
            project_id=pid,
            warehouse_name="رئيسي",
            material_name="حديد قديم",
            quantity=8,
            unit_cost=25,
            transaction_type="إضافة",
            date="2026-07-01",
        )
        cash = InventoryTransaction(
            project_id=pid,
            warehouse_name="الخزنة الرئيسية",
            destination_warehouse=dest_name,
            material_name="تحويل خزنة",
            quantity=400,
            unit_cost=0,
            transaction_type="تحويل",
            date="2026-07-02",
        )
        db.session.add_all([stock, cash])
        db.session.commit()
        stock_id, cash_id = stock.id, cash.id

    page = client.get("/inventory")
    assert page.status_code == 200

    with flask_app.app_context():
        from services.accounting import build_account_balances, resolve_stock_account
        stock = InventoryTransaction.query.get(stock_id)
        cash = InventoryTransaction.query.get(cash_id)
        warehouse = resolve_stock_account(stock.warehouse_name, stock.warehouse_account_id)
        stock_journal = JournalEntry.query.filter(JournalEntry.description.like(f"%INV-AUTO:{stock_id}%")).first()
        cash_journal = JournalEntry.query.filter(JournalEntry.description.like(f"%INV-AUTO:{cash_id}%")).first()
        assert stock_journal is not None
        assert round(stock_journal.amount, 2) == 200
        assert stock_journal.debit_account_id == warehouse.id
        assert cash_journal is not None
        assert round(cash_journal.amount, 2) == 400
        assert cash_journal.credit_account_id == main_id
        assert cash_journal.debit_account_id == dest_id
        balances = build_account_balances()
        assert round(balances.get(warehouse.id, 0.0), 2) == 200
        assert round(balances.get(dest_id, 0.0), 2) == 400
        assert round(cash.unit_cost or 0, 2) == 1


def test_inventory_in_out_types_post_qty_and_cost(client):
    page = client.get("/inventory")
    body = page.get_data(as_text=True)
    assert page.status_code == 200
    assert 'value="وارد"' in body
    assert 'value="منصرف"' in body
    assert 'value="إضافة"' not in body
    assert 'value="سحب"' not in body
    assert 'value="تحويل"' not in body

    with flask_app.app_context():
        project = Project(
            code="PRJ-INOUT", project_name="وارد منصرف", client_name="عميل",
            contract_type="مقاولات عامة", contract_value=1,
        )
        dest = (
            ChartOfAccount.query.filter_by(code="EXP-ELC").first()
            or ChartOfAccount.query.filter_by(category="المصروفات").first()
        )
        db.session.add(project)
        db.session.commit()
        pid, dest_id = project.id, dest.id

    incoming = client.post("/inventory", data={
        "project_id": str(pid),
        "warehouse_name": "رئيسي",
        "quantity": "10",
        "unit_cost": "40",
        "transaction_type": "وارد",
        "date": "2026-09-10",
        "material_name": "أسمنت",
        "unit": "شيكارة",
    }, follow_redirects=True)
    assert incoming.status_code == 200
    assert "تم تسجيل حركة المخزون" in incoming.get_data(as_text=True)

    missing_dest = client.post("/inventory", data={
        "project_id": str(pid),
        "warehouse_name": "رئيسي",
        "quantity": "4",
        "unit_cost": "40",
        "transaction_type": "منصرف",
        "date": "2026-09-11",
        "material_name": "أسمنت",
        "unit": "شيكارة",
    }, follow_redirects=True)
    assert "الوجهة" in missing_dest.get_data(as_text=True)

    outgoing = client.post("/inventory", data={
        "project_id": str(pid),
        "warehouse_name": "رئيسي",
        "destination_warehouse": f"acc:{dest_id}",
        "quantity": "4",
        "unit_cost": "40",
        "transaction_type": "منصرف",
        "date": "2026-09-11",
        "material_name": "أسمنت",
        "unit": "شيكارة",
    }, follow_redirects=True)
    assert outgoing.status_code == 200
    assert "تم تسجيل حركة المخزون" in outgoing.get_data(as_text=True)

    with flask_app.app_context():
        from services.accounting import available_stock_qty, build_account_balances, build_account_statement, resolve_stock_account
        inbound = InventoryTransaction.query.filter_by(project_id=pid, transaction_type="وارد").first()
        outbound = InventoryTransaction.query.filter_by(project_id=pid, transaction_type="منصرف").first()
        assert inbound is not None and outbound is not None
        warehouse = resolve_stock_account(inbound.warehouse_name, inbound.warehouse_account_id)
        assert warehouse is not None
        assert round(available_stock_qty(pid, "رئيسي", "أسمنت"), 2) == 6
        inbound_journal = JournalEntry.query.filter(JournalEntry.description.like(f"%INV-AUTO:{inbound.id}%")).first()
        outbound_journal = JournalEntry.query.filter(JournalEntry.description.like(f"%INV-AUTO:{outbound.id}%")).first()
        assert inbound_journal is not None
        assert round(inbound_journal.amount, 2) == 400
        assert inbound_journal.debit_account_id == warehouse.id
        assert outbound_journal is not None
        assert round(outbound_journal.amount, 2) == 160
        assert outbound_journal.debit_account_id == dest_id
        assert outbound_journal.credit_account_id == warehouse.id
        assert "4" in (outbound_journal.description or "")
        balances = build_account_balances()
        assert round(balances.get(warehouse.id, 0.0), 2) == 240
        assert round(balances.get(dest_id, 0.0), 2) == 160
        dest = ChartOfAccount.query.get(dest_id)
        statement = build_account_statement(dest)
        charged = [row for row in statement["movements"] if row["entry_id"] == outbound_journal.id]
        assert charged
        assert round(charged[0]["quantity"], 2) == 4
        assert "4" in (charged[0]["quantity_label"] or "")
        assert round(charged[0]["debit"], 2) == 160

    dest_page = client.get(f"/accounts/{dest_id}/statement")
    dest_body = dest_page.get_data(as_text=True)
    assert dest_page.status_code == 200
    assert "الكمية" in dest_body
    assert "4 شيكارة" in dest_body or ">4<" in dest_body


def test_data_entry_can_open_backup(client):
    with flask_app.app_context():
        clerk = User(username="backupclerk", full_name="مدخل نسخ", role=ROLE_DATA_ENTRY, is_active=True)
        clerk.set_password("secret12")
        db.session.add(clerk)
        db.session.commit()
        clerk_id = clerk.id

    probe = flask_app.test_client()
    with probe.session_transaction() as sess:
        sess["user_id"] = clerk_id
    home = probe.get("/")
    home_body = home.get_data(as_text=True)
    assert home.status_code == 200
    assert "النسخ الاحتياطي" in home_body
    restore = probe.get("/admin/restore")
    assert restore.status_code == 200
    assert "النسخ الاحتياطي" in restore.get_data(as_text=True)
    backup = probe.get("/admin/backup")
    assert backup.status_code == 200
    assert backup.headers.get("Content-Disposition", "").startswith("attachment")


def test_printed_statements_show_progress_item_and_po_item(client):
    with flask_app.app_context():
        project = Project(
            code="PRJ-STMT", project_name="كشف منظم", client_name="عميل كشف",
            contract_type="مقاولات عامة", contract_value=1,
        )
        sub = Subcontractor(name="مقاول بنود")
        supplier = Supplier(name="مورد أصناف")
        db.session.add_all([project, sub, supplier])
        db.session.commit()
        pid, sid, supplier_id = project.id, sub.id, supplier.id

    client.post("/progress_payments", data={
        "project_id": str(pid),
        "subcontractor_id": str(sid),
        "date": "2026-09-12",
        "retention_percentage": "0",
        "tax_percentage": "0",
        "description": ["ميول خرسانية"],
        "unit": ["متر مسطح"],
        "quantity": ["12"],
        "unit_price": ["100"],
    }, follow_redirects=True)

    client.post("/purchase_orders", data={
        "project_id": str(pid),
        "supplier_id": str(supplier_id),
        "item_name": "حديد تسليح",
        "warehouse_name": "رئيسي",
        "quantity": "8",
        "unit_price": "50",
        "discount": "0",
        "date": "2026-09-12",
        "status": "مغلق",
    }, follow_redirects=True)

    with flask_app.app_context():
        from services.accounting import (
            build_account_statement, build_subcontractor_statement,
            get_or_create_subcontractor_account, get_or_create_supplier_account,
        )
        sub = Subcontractor.query.get(sid)
        supplier = Supplier.query.get(supplier_id)
        party = get_or_create_subcontractor_account(sub)
        supplier_account = get_or_create_supplier_account(supplier)
        db.session.commit()
        party_id, supplier_account_id = party.id, supplier_account.id
        sc_statement = build_subcontractor_statement(sub)
        assert any("ميول خرسانية" in (row.get("description") or "") for row in sc_statement["movements"])
        party_statement = build_account_statement(party)
        assert any("ميول خرسانية" in (row.get("description") or "") for row in party_statement["movements"])
        po_statement = build_account_statement(supplier_account)
        assert any("حديد" in (row.get("description") or "") for row in po_statement["movements"])

    screen = client.get(f"/accounts/{party_id}/statement")
    assert screen.status_code == 200
    assert "ميول خرسانية" in screen.get_data(as_text=True)

    printed = client.get(f"/print/account/{party_id}")
    print_body = printed.get_data(as_text=True)
    assert printed.status_code == 200
    assert "ميول خرسانية" in print_body
    assert "كشف حساب" in print_body
    assert "company-logo-desktop.png" in print_body
    assert "PP-AUTO:" not in print_body

    sc_print = client.get(f"/print/statement/{sid}")
    assert sc_print.status_code == 200
    assert "ميول خرسانية" in sc_print.get_data(as_text=True)

    supplier_page = client.get(f"/suppliers/{supplier_id}/statement")
    assert supplier_page.status_code == 200
    assert "حديد" in supplier_page.get_data(as_text=True)

    supplier_print = client.get(f"/print/account/{supplier_account_id}")
    supplier_print_body = supplier_print.get_data(as_text=True)
    assert supplier_print.status_code == 200
    assert "حديد" in supplier_print_body
    assert "PO-JRN-AUTO:" not in supplier_print_body


def test_delete_unused_purchase_order_clears_stock(client):
    with flask_app.app_context():
        project = Project(
            code="PRJ-T3C", project_name="مخزن ج", client_name="عميل",
            contract_type="مقاولات عامة", contract_value=1,
        )
        supplier = Supplier(name="مورد رمل")
        db.session.add_all([project, supplier])
        db.session.commit()
        pid, sid = project.id, supplier.id

    client.post("/purchase_orders", data={
        "project_id": str(pid),
        "supplier_id": str(sid),
        "item_name": "رمل",
        "warehouse_name": "رئيسي",
        "quantity": "4",
        "unit_price": "20",
        "discount": "0",
        "date": "2026-08-01",
        "status": "مغلق",
    }, follow_redirects=True)

    with flask_app.app_context():
        order = PurchaseOrder.query.first()
        oid = order.id
        assert InventoryTransaction.query.filter(InventoryTransaction.notes.like(f"%PO-AUTO:{oid}%")).first() is not None

    deleted = client.post(f"/documents/purchase_order/{oid}/delete", follow_redirects=True)
    assert deleted.status_code == 200
    with flask_app.app_context():
        assert PurchaseOrder.query.get(oid) is None
        assert InventoryTransaction.query.filter(InventoryTransaction.notes.like(f"%PO-AUTO:{oid}%")).all() == []
        assert JournalEntry.query.filter(JournalEntry.description.like(f"%PO-JRN-AUTO:{oid}%")).all() == []


def test_warehouse_picker_lists_registered_warehouses_and_all_accounts(client):
    with flask_app.app_context():
        extra = ChartOfAccount(code="WHS-WRSH", name="مخزن - ورشة غرب", category="المخازن", opening_balance=0)
        db.session.add(extra)
        db.session.commit()
        inv_mat = ChartOfAccount.query.filter_by(code="INV-MAT").first()
        assert inv_mat is not None
        inv_mat_id = inv_mat.id

    po_html = client.get("/purchase_orders").get_data(as_text=True)
    inv_html = client.get("/inventory").get_data(as_text=True)
    assert "المخازن المسجلة" in po_html
    assert "كل الحسابات" in po_html
    assert "ورشة غرب" in po_html
    assert f"acc:{inv_mat_id}" in po_html
    assert "INV-MAT" in po_html
    assert "المخازن المسجلة" in inv_html
    assert "ورشة غرب" in inv_html
    assert f"acc:{inv_mat_id}" in inv_html
    assert "INV-MAT" in inv_html
    assert "المستلم" in inv_html
    assert "أمين المخزن" in inv_html
    assert "اكتب اسم الصنف" in inv_html


def test_purchase_order_debits_selected_account(client):
    with flask_app.app_context():
        project = Project(
            code="PRJ-WH1", project_name="حساب مخزن", client_name="عميل",
            contract_type="مقاولات عامة", contract_value=1,
        )
        supplier = Supplier(name="مورد حساب")
        target = ChartOfAccount.query.filter_by(code="EXP-MAT").first()
        db.session.add_all([project, supplier])
        db.session.commit()
        pid, sid, acc_id = project.id, supplier.id, target.id

    client.post("/purchase_orders", data={
        "project_id": str(pid),
        "supplier_id": str(sid),
        "item_name": "أسمنت",
        "warehouse_name": f"acc:{acc_id}",
        "quantity": "2",
        "unit_price": "150",
        "discount": "0",
        "date": "2026-08-01",
        "status": "مغلق",
    }, follow_redirects=True)

    with flask_app.app_context():
        order = PurchaseOrder.query.filter_by(project_id=pid).first()
        assert order is not None
        assert order.warehouse_account_id == acc_id
        journal = JournalEntry.query.filter(JournalEntry.description.like(f"%PO-JRN-AUTO:{order.id}%")).first()
        assert journal is not None
        assert journal.debit_account_id == acc_id
        assert round(journal.amount, 2) == 300.0
        tx = InventoryTransaction.query.filter(InventoryTransaction.notes.like(f"%PO-AUTO:{order.id}%")).first()
        assert tx is not None
        assert tx.warehouse_account_id == acc_id


def test_named_warehouse_posts_to_its_stock_account(client):
    with flask_app.app_context():
        project = Project(
            code="PRJ-WH2", project_name="مخزن مسمى", client_name="عميل",
            contract_type="مقاولات عامة", contract_value=1,
        )
        supplier = Supplier(name="مورد ورشة")
        db.session.add_all([project, supplier])
        db.session.commit()
        pid, sid = project.id, supplier.id

    client.post("/purchase_orders", data={
        "project_id": str(pid),
        "supplier_id": str(sid),
        "item_name": "رمل",
        "warehouse_name": "ورشة غرب",
        "quantity": "4",
        "unit_price": "25",
        "discount": "0",
        "date": "2026-08-01",
        "status": "مغلق",
    }, follow_redirects=True)

    with flask_app.app_context():
        order = PurchaseOrder.query.filter_by(project_id=pid).first()
        stock = ChartOfAccount.query.filter_by(name="مخزن - ورشة غرب", category="المخازن").first()
        assert order is not None
        assert stock is not None
        assert order.warehouse_account_id == stock.id
        journal = JournalEntry.query.filter(JournalEntry.description.like(f"%PO-JRN-AUTO:{order.id}%")).first()
        assert journal is not None
        assert journal.debit_account_id == stock.id
        inv_mat = ChartOfAccount.query.filter_by(code="INV-MAT").first()
        assert journal.debit_account_id != inv_mat.id


def test_inventory_add_debits_selected_account(client):
    with flask_app.app_context():
        project = Project(
            code="PRJ-WH3", project_name="إضافة مخزن", client_name="عميل",
            contract_type="مقاولات عامة", contract_value=1,
        )
        supplier = Supplier(name="مورد يدوي")
        target = ChartOfAccount.query.filter_by(code="INV-MAT").first()
        db.session.add_all([project, supplier])
        db.session.commit()
        pid, sid, acc_id = project.id, supplier.id, target.id

    client.post("/inventory", data={
        "project_id": str(pid),
        "supplier_id": str(sid),
        "material_name": "حديد",
        "warehouse_name": f"acc:{acc_id}",
        "quantity": "3",
        "unit_cost": "40",
        "transaction_type": "إضافة",
        "date": "2026-08-01",
    }, follow_redirects=True)

    with flask_app.app_context():
        tx = InventoryTransaction.query.filter_by(project_id=pid, material_name="حديد").first()
        assert tx is not None
        assert tx.warehouse_account_id == acc_id
        journal = JournalEntry.query.filter(JournalEntry.description.like(f"%INV-AUTO:{tx.id}%")).first()
        assert journal is not None
        assert journal.debit_account_id == acc_id
        assert round(journal.amount, 2) == 120.0


def test_inventory_saves_recipient_storekeeper_and_manual_item(client):
    with flask_app.app_context():
        project = Project(
            code="PRJ-WH4", project_name="مستلم وأمين", client_name="عميل",
            contract_type="مقاولات عامة", contract_value=1,
        )
        db.session.add(project)
        db.session.commit()
        pid = project.id

    client.post("/inventory", data={
        "project_id": str(pid),
        "material_name": "بلوك يدوي",
        "warehouse_name": "رئيسي",
        "recipient_name": "محمد علي",
        "storekeeper_name": "حسن أمين",
        "quantity": "1",
        "unit_cost": "10",
        "transaction_type": "إضافة",
        "date": "2026-08-01",
    }, follow_redirects=True)

    with flask_app.app_context():
        tx = InventoryTransaction.query.filter_by(project_id=pid, material_name="بلوك يدوي").first()
        assert tx is not None
        assert tx.recipient_name == "محمد علي"
        assert tx.storekeeper_name == "حسن أمين"


def test_project_cost_matches_profitability_source(client):
    with flask_app.app_context():
        project = Project(
            code="PRJ-T4", project_name="طريق", client_name="عميل د",
            contract_type="مقاولات عامة", contract_value=1, admin_percentage=10,
        )
        sub = Subcontractor(name="باطن")
        db.session.add_all([project, sub])
        db.session.commit()
        from models import ProgressPayment, ProgressPaymentItem
        payment = ProgressPayment(project_id=project.id, subcontractor_id=sub.id, date="2026-08-01", total_value=0)
        db.session.add(payment)
        db.session.commit()
        db.session.add(ProgressPaymentItem(
            progress_payment_id=payment.id, description="أعمال", unit="م3",
            quantity=2, unit_price=1000, value=2000,
        ))
        from services.accounting import recalculate_progress_payment, sync_progress_payment_journals
        recalculate_progress_payment(payment)
        db.session.commit()
        breakdown = build_project_cost_breakdown(project)
        assert breakdown["subcontractor_cost"] == 2000.0
        assert breakdown["admin_allocation"] == 200.0
        assert breakdown["total_cost"] == 2200.0


def test_client_receipt_posts_to_receivable_and_treasury(client):
    with flask_app.app_context():
        project = Project(
            code="PRJ-T5", project_name="تحصيل", client_name="عميل هـ",
            contract_type="مقاولات عامة", contract_value=1,
        )
        db.session.add(project)
        db.session.commit()
        pid = project.id
        treasury_id, _ = _ids()

    client.post("/client_receipts", data={
        "client_name": "عميل هـ",
        "project_id": str(pid),
        "amount": "1500",
        "payment_method": "نقدي",
        "treasury_account_id": str(treasury_id),
        "date": "2026-08-10",
    }, follow_redirects=True)

    with flask_app.app_context():
        receipt = ClientReceipt.query.first()
        assert receipt is not None
        journals = JournalEntry.query.filter(JournalEntry.description.like(f"%REC-AUTO:{receipt.id}%")).all()
        assert journals and round(journals[0].amount, 2) == 1500.0
        client_account = ChartOfAccount.query.filter_by(name="عميل - عميل هـ").first()
        treasury = ChartOfAccount.query.filter_by(code="TRS-MAIN").first()
        assert journals[0].debit_account_id == treasury.id
        assert journals[0].credit_account_id == client_account.id


def test_closed_period_blocks_journal_post(client):
    with flask_app.app_context():
        period = AccountingPeriod(name="أغسطس", from_date="2026-08-01", to_date="2026-08-31", status="مغلقة")
        db.session.add(period)
        db.session.commit()
        treasury_id, expense_id = _ids()

    response = client.post("/journal", data={
        "date": "2026-08-15",
        "entry_action": "post",
        "line_description": ["قيد تجريبي"],
        "line_debit_account_id": [str(expense_id)],
        "line_credit_account_id": [str(treasury_id)],
        "line_amount": ["100"],
    }, follow_redirects=True)
    body = response.get_data(as_text=True)
    assert "مغلقة" in body
    with flask_app.app_context():
        assert JournalEntry.query.count() == 0


def test_data_entry_cannot_open_accounts(client):
    with flask_app.app_context():
        clerk = User(username="clerk", full_name="مدخل بيانات", role=ROLE_DATA_ENTRY, is_active=True)
        clerk.set_password("secret12")
        db.session.add(clerk)
        db.session.commit()
        clerk_id = clerk.id

    probe = flask_app.test_client()
    with probe.session_transaction() as sess:
        sess["user_id"] = clerk_id
    response = probe.get("/accounts", follow_redirects=True)
    assert "ليست لديك صلاحية" in response.get_data(as_text=True)


def test_posted_documents_delete_clears_both_accounts(client):
    with flask_app.app_context():
        project = Project(
            code="PRJ-T6", project_name="حذف", client_name="عميل و",
            contract_type="مقاولات عامة", contract_value=1,
        )
        db.session.add(project)
        db.session.commit()
        pid = project.id

    client.post("/client_progress_payments", data={
        "project_id": str(pid),
        "date": "2026-08-03",
        "retention_percentage": "0",
        "tax_percentage": "0",
        "description": ["بند"],
        "unit": ["مقطوعية"],
        "quantity": ["1"],
        "unit_price": ["9000"],
    }, follow_redirects=True)

    with flask_app.app_context():
        payment = ProgressPayment.query.filter(ProgressPayment.subcontractor_id.is_(None)).first()
        payment_id = payment.id
        journals = JournalEntry.query.filter(JournalEntry.description.like(f"%PP-AUTO:{payment_id}%")).all()
        assert len(journals) == 1
        debit_id = journals[0].debit_account_id
        credit_id = journals[0].credit_account_id

    response = client.post(f"/documents/progress_payment/{payment_id}/delete", follow_redirects=True)
    body = response.get_data(as_text=True)
    assert "الحسابين" in body
    with flask_app.app_context():
        assert ProgressPayment.query.get(payment_id) is None
        leftover = JournalEntry.query.filter(
            JournalEntry.description.like(f"%PP-AUTO:{payment_id}%")
        ).count()
        assert leftover == 0
        assert JournalEntry.query.filter(
            (JournalEntry.debit_account_id == debit_id) | (JournalEntry.credit_account_id == debit_id),
            JournalEntry.description.like("%بند%"),
        ).count() == 0
        assert JournalEntry.query.filter(
            (JournalEntry.debit_account_id == credit_id) | (JournalEntry.credit_account_id == credit_id),
            JournalEntry.description.like("%بند%"),
        ).count() == 0


def test_supplier_payment_edit_and_delete_syncs_both_accounts(client):
    with flask_app.app_context():
        supplier = Supplier(name="مورد سداد متزامن")
        db.session.add(supplier)
        db.session.commit()
        sync_journal_related_accounts()
        supplier_id = supplier.id
        treasury = ChartOfAccount.query.filter_by(code="TRS-MAIN").first()
        treasury_id = treasury.id
        party = ChartOfAccount.query.filter_by(code=f"SUP-{supplier.id:04d}").first()
        party_id = party.id

    create = client.post("/supplier_payments", data={
        "supplier_id": str(supplier_id),
        "date": "2026-09-05",
        "amount": "500",
        "payment_method": "نقدي",
        "treasury_account_id": str(treasury_id),
        "reference": "PAY-SYNC",
    }, follow_redirects=True)
    assert create.status_code == 200

    with flask_app.app_context():
        payment = SupplierPayment.query.filter_by(supplier_id=supplier_id).first()
        payment_id = payment.id
        journal = JournalEntry.query.filter(JournalEntry.description.like(f"%PAY-AUTO:{payment_id}%")).first()
        assert journal is not None
        assert journal.debit_account_id == party_id
        assert journal.credit_account_id == treasury_id
        assert round(journal.amount, 2) == 500

    client.post(f"/supplier_payments/{payment_id}/update", data={
        "supplier_id": str(supplier_id),
        "date": "2026-09-05",
        "amount": "350",
        "payment_method": "نقدي",
        "treasury_account_id": str(treasury_id),
        "reference": "PAY-SYNC",
    }, follow_redirects=True)

    with flask_app.app_context():
        payment = SupplierPayment.query.get(payment_id)
        journal = JournalEntry.query.filter(JournalEntry.description.like(f"%PAY-AUTO:{payment_id}%")).first()
        assert round(payment.amount, 2) == 350
        assert journal is not None
        assert round(journal.amount, 2) == 350
        assert journal.debit_account_id == party_id
        assert journal.credit_account_id == treasury_id

    party_stmt = client.get(f"/accounts/{party_id}/statement").get_data(as_text=True)
    treasury_stmt = client.get(f"/accounts/{treasury_id}/statement").get_data(as_text=True)
    assert "350" in party_stmt.replace(",", "")
    assert "350" in treasury_stmt.replace(",", "")

    client.post(f"/documents/supplier_payment/{payment_id}/delete", follow_redirects=True)
    with flask_app.app_context():
        assert SupplierPayment.query.get(payment_id) is None
        assert JournalEntry.query.filter(JournalEntry.description.like(f"%PAY-AUTO:{payment_id}%")).count() == 0

    party_stmt = client.get(f"/accounts/{party_id}/statement").get_data(as_text=True)
    treasury_stmt = client.get(f"/accounts/{treasury_id}/statement").get_data(as_text=True)
    assert "PAY-SYNC" not in party_stmt
    assert "PAY-SYNC" not in treasury_stmt


def test_cannot_delete_account_or_posted_journal(client):
    with flask_app.app_context():
        treasury_id, expense_id = _ids()

    client.post("/journal", data={
        "date": "2026-09-05",
        "entry_action": "post",
        "line_description": ["قيد يُحذف من الحسابين"],
        "line_debit_account_id": [str(expense_id)],
        "line_credit_account_id": [str(treasury_id)],
        "line_amount": ["40"],
    }, follow_redirects=True)

    with flask_app.app_context():
        entry = JournalEntry.query.filter(JournalEntry.description == "قيد يُحذف من الحسابين").first()
        entry_id = entry.id

    delete_account = client.post(f"/accounts/{expense_id}/delete", follow_redirects=True)
    assert "لا يمكن حذف الحسابات" in delete_account.get_data(as_text=True)

    create_unused = client.post("/accounts", data={
        "code": "TST-DEL",
        "name": "حساب للتجربة",
        "category": "المصروفات",
        "opening_balance": "0",
    }, follow_redirects=True)
    assert create_unused.status_code == 200
    with flask_app.app_context():
        unused = ChartOfAccount.query.filter_by(code="TST-DEL").first()
        unused_id = unused.id
    deleted_unused = client.post(f"/accounts/{unused_id}/delete", follow_redirects=True)
    assert "تم حذف الحساب TST-DEL" in deleted_unused.get_data(as_text=True)
    with flask_app.app_context():
        assert ChartOfAccount.query.filter_by(code="TST-DEL").first() is None

    delete_journal = client.post(f"/journal/{entry_id}/delete", follow_redirects=True)
    assert "الحساب" in delete_journal.get_data(as_text=True)
    with flask_app.app_context():
        assert ChartOfAccount.query.get(expense_id) is not None
        assert JournalEntry.query.get(entry_id) is None


def test_client_name_spelling_reuses_same_account(client):
    with flask_app.app_context():
        from services.accounting import get_or_create_client_account
        first = get_or_create_client_account("شركة النور")
        db.session.commit()
        first_id = first.id
        second = get_or_create_client_account("  شركة   النور  ")
        db.session.commit()
        assert second.id == first_id
        matches = [
            item for item in ChartOfAccount.query.filter_by(category="العملاء").all()
            if "النور" in (item.name or "")
        ]
        assert len(matches) == 1


def test_change_password_page_available(client):
    response = client.get("/account/password")
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "حسابي" in body
    assert "تغيير كلمة السر" in body
    assert "تغيير باسورد الـ PIN" in body
    assert "123456" in body


def test_manual_journal_to_subcontractor_creates_advance(client):
    with flask_app.app_context():
        project = Project(
            code="PRJ-J1", project_name="ربط قيود", client_name="عميل ز",
            contract_type="مقاولات عامة", contract_value=500000,
        )
        sub = Subcontractor(name="مقاول يومية")
        db.session.add_all([project, sub])
        db.session.commit()
        sync_journal_related_accounts()
        sub_id = sub.id
        project_id = project.id
        sub_account = ChartOfAccount.query.filter_by(code=f"SUB-{sub.id:04d}").first()
        treasury = ChartOfAccount.query.filter_by(code="TRS-MAIN").first()
        sub_account_id = sub_account.id
        treasury_id = treasury.id

    client.post("/journal", data={
        "date": "2026-08-12",
        "entry_action": "post",
        "line_description": ["صرف لمقاول يومية"],
        "line_debit_account_id": [str(sub_account_id)],
        "line_credit_account_id": [str(treasury_id)],
        "line_amount": ["2500"],
    }, follow_redirects=True)

    with flask_app.app_context():
        advance = SubcontractorPayment.query.filter_by(subcontractor_id=sub_id).first()
        assert advance is not None
        assert round(advance.amount, 2) == 2500.0
        assert "JRN-SRC:" in (advance.notes or "")
        assert get_subcontractor_outstanding_advances(sub_id) == 2500.0
        statement = build_subcontractor_statement(Subcontractor.query.get(sub_id))
        assert statement["total_paid"] == 2500.0
        auto_dupes = JournalEntry.query.filter(JournalEntry.description.like("%SPAY-AUTO:%")).count()
        assert auto_dupes == 0

    client.post("/progress_payments", data={
        "project_id": str(project_id),
        "subcontractor_id": str(sub_id),
        "date": "2026-08-13",
        "retention_percentage": "0",
        "tax_percentage": "0",
        "advance_deduction": "2500",
        "description": ["أعمال"],
        "unit": ["متر مكعب"],
        "quantity": ["10"],
        "unit_price": ["400"],
    }, follow_redirects=True)

    with flask_app.app_context():
        payment = ProgressPayment.query.filter_by(subcontractor_id=sub_id).first()
        assert payment is not None
        assert round(payment.advance_deduction, 2) == 2500.0
        assert round(payment.net_value, 2) == 1500.0
        statement = build_subcontractor_statement(Subcontractor.query.get(sub_id))
        assert statement["total_works"] == 4000.0
        assert statement["total_paid"] == 2500.0
        assert statement["net_due"] == 1500.0


def test_manual_journal_to_supplier_creates_payment(client):
    with flask_app.app_context():
        supplier = Supplier(name="مورد يومية")
        db.session.add(supplier)
        db.session.commit()
        sync_journal_related_accounts()
        supplier_id = supplier.id
        sup_account = ChartOfAccount.query.filter_by(code=f"SUP-{supplier.id:04d}").first()
        treasury = ChartOfAccount.query.filter_by(code="TRS-MAIN").first()
        sup_account_id = sup_account.id
        treasury_id = treasury.id

    client.post("/journal", data={
        "date": "2026-08-12",
        "entry_action": "post",
        "line_description": ["سداد مورد"],
        "line_debit_account_id": [str(sup_account_id)],
        "line_credit_account_id": [str(treasury_id)],
        "line_amount": ["800"],
    }, follow_redirects=True)

    with flask_app.app_context():
        payment = SupplierPayment.query.filter_by(supplier_id=supplier_id).first()
        assert payment is not None
        assert round(payment.amount, 2) == 800.0
        assert JournalEntry.query.filter(JournalEntry.description.like("%PAY-AUTO:%")).count() == 0


def test_account_statement_lists_posted_moves(client):
    with flask_app.app_context():
        treasury_id, expense_id = _ids()
        treasury_name = ChartOfAccount.query.get(treasury_id).name

    listing = client.get("/accounts")
    assert listing.status_code == 200
    assert "كشف حساب" in listing.get_data(as_text=True)

    client.post("/journal", data={
        "date": "2026-08-20",
        "entry_action": "post",
        "line_description": ["صرف من الخزنة"],
        "line_debit_account_id": [str(expense_id)],
        "line_credit_account_id": [str(treasury_id)],
        "line_amount": ["250"],
    }, follow_redirects=True)

    response = client.get(f"/accounts/{treasury_id}/statement")
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "كشف حساب" in body
    assert treasury_name in body
    assert "250" in body.replace(",", "").replace(".", "")
    assert "صرف من الخزنة" in body


def test_journal_stamps_actor_and_writes_activity_log(client):
    listing = client.get("/journal")
    assert listing.status_code == 200
    assert "المستخدم" in listing.get_data(as_text=True)

    with flask_app.app_context():
        treasury_id, expense_id = _ids()

    client.post("/journal", data={
        "date": "2026-08-21",
        "entry_action": "post",
        "line_description": ["صرف تجريبي للتتبع"],
        "line_debit_account_id": [str(expense_id)],
        "line_credit_account_id": [str(treasury_id)],
        "line_amount": ["75"],
    }, follow_redirects=True)

    with flask_app.app_context():
        entry = JournalEntry.query.order_by(JournalEntry.id.desc()).first()
        assert entry is not None
        assert entry.created_by_name == "مدير النظام"
        assert entry.created_at
        log = ActivityLog.query.filter_by(entity_type="JournalEntry", entity_id=entry.id).first()
        assert log is not None
        assert log.user_name == "مدير النظام"
        assert log.action == "إضافة"
        assert log.summary

    page = client.get("/activity-log")
    body = page.get_data(as_text=True)
    assert page.status_code == 200
    assert "سجل حركات المستخدمين" in body
    assert "مدير النظام" in body
    assert "قيد يومية" in body


def test_data_entry_cannot_open_activity_log(client):
    with flask_app.app_context():
        clerk = User(username="clerk2", full_name="مدخل بيانات ٢", role=ROLE_DATA_ENTRY, is_active=True)
        clerk.set_password("secret12")
        db.session.add(clerk)
        db.session.commit()
        clerk_id = clerk.id

    probe = flask_app.test_client()
    with probe.session_transaction() as sess:
        sess["user_id"] = clerk_id
    response = probe.get("/activity-log", follow_redirects=True)
    body = response.get_data(as_text=True)
    assert "سجل حركات المستخدمين" not in body
    assert "ليست لديك صلاحية" in body or "متاح لمدير النظام فقط" in body


def test_account_rename_does_not_change_posted_amount(client):
    with flask_app.app_context():
        treasury_id, expense_id = _ids()
        expense = ChartOfAccount.query.get(expense_id)
        original_amount_account_id = expense.id
        original_opening = expense.opening_balance or 0

    client.post("/journal", data={
        "date": "2026-09-05",
        "entry_action": "post",
        "line_description": ["صرف ثابت"],
        "line_debit_account_id": [str(expense_id)],
        "line_credit_account_id": [str(treasury_id)],
        "line_amount": ["120"],
    }, follow_redirects=True)

    client.post(f"/accounts/{expense_id}/update", data={
        "code": "EXP-RENAMED",
        "name": "مصروف معاد تسميته",
        "category": "المصروفات",
        "opening_balance": str(original_opening),
    }, follow_redirects=True)

    with flask_app.app_context():
        entry = JournalEntry.query.filter(JournalEntry.description == "صرف ثابت").first()
        assert entry is not None
        assert round(entry.amount, 2) == 120
        assert entry.debit_account_id == original_amount_account_id
        account = ChartOfAccount.query.get(expense_id)
        assert account.name == "مصروف معاد تسميته"
        assert account.code == "EXP-RENAMED"
        assert account.category == "المصروفات"


def test_journal_account_picker_has_word_search(client):
    page = client.get("/journal")
    body = page.get_data(as_text=True)
    script = client.get("/static/searchable-select.js").get_data(as_text=True)
    assert page.status_code == 200
    assert "js-searchable-select" in body
    assert "ابحث بالكلمة" not in script
    assert "select.js-searchable-select" in script
    assert "searchable-select-input" in script
    assert "searchable-select-menu" in script


def test_employee_display_name_includes_tag(client):
    response = client.post("/employees", data={
        "name": "علي مهران",
        "tag": "مورد",
        "basic_salary": "1000",
    }, follow_redirects=True)
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "علي مهران (مورد)" in body
    with flask_app.app_context():
        employee = Employee.query.filter_by(name="علي مهران").first()
        assert employee is not None
        assert employee.display_name == "علي مهران (مورد)"
        account = ChartOfAccount.query.filter_by(code=f"EMP-{employee.id:04d}").first()
        assert account is not None
        assert account.category == "الموظفين"
        assert "علي مهران (مورد)" in account.name


def test_payroll_accrual_stays_on_expense_when_cash_is_paid(client):
    client.post("/employees", data={
        "name": "علي مهران",
        "tag": "مورد",
        "basic_salary": "1000",
    }, follow_redirects=True)

    with flask_app.app_context():
        employee = Employee.query.filter_by(name="علي مهران").first()
        employee_id = employee.id
        treasury_id = ChartOfAccount.query.filter_by(code="TRS-MAIN").first().id

    client.post("/hr", data={
        "action": "post_payroll",
        "period_month": "2026-09",
        "posting_date": "2026-09-30",
        "employee_id": [str(employee_id)],
        "work_days": ["30"],
        "vacation_days": ["0"],
        "basic_salary": ["1000"],
        "overtime": ["0"],
        "incentives": ["0"],
        "delay_deduction": ["0"],
        "permission_deduction": ["0"],
        "other_deductions": ["0"],
        "advances": ["0"],
        "payroll_notes": [""],
    }, follow_redirects=True)

    with flask_app.app_context():
        expense = get_account_by_code("EXP-SAL")
        employee = Employee.query.get(employee_id)
        emp_account = get_or_create_employee_account(employee)
        balances = build_account_balances()
        assert expense is not None
        assert round(balances.get(expense.id, 0.0), 2) == 1000
        assert round(balances.get(emp_account.id, 0.0), 2) == -1000
        slip = PayrollSlip.query.filter_by(employee_id=employee_id, period_month="2026-09").first()
        assert slip is not None
        assert round(slip.net_salary, 2) == 1000
        journal = JournalEntry.query.filter(JournalEntry.description.like(f"%PAYROLL-AUTO:{slip.id}%")).first()
        assert journal is not None
        assert journal.debit_account_id == expense.id
        assert journal.credit_account_id == emp_account.id

    client.post("/hr", data={
        "action": "pay_salary",
        "period_month": "2026-09",
        "employee_id": str(employee_id),
        "date": "2026-09-30",
        "amount": "500",
        "payment_method": "نقدي",
        "treasury_account_id": str(treasury_id),
    }, follow_redirects=True)

    with flask_app.app_context():
        expense = get_account_by_code("EXP-SAL")
        employee = Employee.query.get(employee_id)
        emp_account = get_or_create_employee_account(employee)
        treasury = ChartOfAccount.query.filter_by(code="TRS-MAIN").first()
        balances = build_account_balances()
        assert round(balances.get(expense.id, 0.0), 2) == 1000
        assert round(balances.get(emp_account.id, 0.0), 2) == -500
        assert round(balances.get(treasury.id, 0.0), 2) == -500
        payment = EmployeeSalaryPayment.query.filter_by(employee_id=employee_id).first()
        assert payment is not None
        pay_journal = JournalEntry.query.filter(JournalEntry.description.like(f"%ESAL-AUTO:{payment.id}%")).first()
        assert pay_journal is not None
        assert pay_journal.debit_account_id == emp_account.id
        assert pay_journal.credit_account_id == treasury.id

    page = client.get("/reports/profit_loss")
    body = page.get_data(as_text=True)
    assert "مصروف المرتبات" in body
    assert "1.000" in body


def test_excel_import_adds_supplier_and_employee(client):
    from io import BytesIO
    from openpyxl import Workbook
    from services.workbook import SHEET_EMPLOYEES, SHEET_SUPPLIERS

    workbook = Workbook()
    workbook.remove(workbook.active)
    suppliers = workbook.create_sheet(SHEET_SUPPLIERS)
    suppliers.append(["الاسم", "الكود", "التصنيف", "الاتصال", "ملاحظات"])
    suppliers.append(["مورد الإكسل", "", "مورد توريد مواد بناء", "", ""])
    employees = workbook.create_sheet(SHEET_EMPLOYEES)
    employees.append(["الاسم", "الميزة", "المرتب الأساسي", "الاتصال", "ملاحظات"])
    employees.append(["أحمد فني", "فني", 2500, "", ""])
    buffer = BytesIO()
    workbook.save(buffer)
    buffer.seek(0)

    page = client.post(
        "/import-workbook",
        data={"workbook": (buffer, "shoghl.xlsx")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert page.status_code == 200
    assert "تم النقل" in page.get_data(as_text=True)
    with flask_app.app_context():
        assert Supplier.query.filter_by(name="مورد الإكسل").first() is not None
        employee = Employee.query.filter_by(name="أحمد فني").first()
        assert employee is not None
        assert employee.display_name == "أحمد فني (فني)"
        assert ChartOfAccount.query.filter_by(code=f"EMP-{employee.id:04d}").first() is not None
        assert ChartOfAccount.query.filter_by(code=f"SUP-{Supplier.query.filter_by(name='مورد الإكسل').first().id:04d}").first() is not None


def test_desktop_shortcut_uses_current_host(client):
    page = client.get("/desktop-shortcut")
    assert page.status_code == 200
    assert "اختصار لاب إبراهيم" in page.get_data(as_text=True)
    file_resp = client.get("/desktop-shortcut/url")
    assert file_resp.status_code == 200
    body = file_resp.get_data(as_text=True)
    assert "[InternetShortcut]" in body
    assert "URL=" in body


def test_home_hides_laptop_shortcut_and_uses_account_picker(client):
    home = client.get("/")
    body = home.get_data(as_text=True)
    assert "اختصار لاب إبراهيم" not in body
    assert "اختصار اللاب" not in body
    assert "انقل شغلك من الإكسل" not in body
    assert "النسخ الاحتياطي" in body
    assert 'name="account_id"' in body
    assert "js-searchable-select" in body
    with flask_app.app_context():
        account = ChartOfAccount.query.filter_by(code="TRS-MAIN").first()
        assert account is not None
        account_id = account.id
    lookup = client.get(f"/accounts/lookup?account_id={account_id}")
    assert lookup.status_code == 302
    assert f"/accounts/{account_id}/statement" in lookup.headers["Location"]


def test_zero_advance_deduction_is_respected(client):
    with flask_app.app_context():
        project = Project(
            code="PRJ-ADV0", project_name="سلفة صفر", client_name="عميل س",
            contract_type="مقاولات عامة", contract_value=1,
        )
        sub = Subcontractor(name="مقاول سلفة")
        db.session.add_all([project, sub])
        db.session.commit()
        pid, sid = project.id, sub.id
        treasury_id, _ = _ids()

    client.post("/subcontractor_payments", data={
        "subcontractor_id": str(sid),
        "project_id": str(pid),
        "amount": "2000",
        "payment_kind": "تحت الحساب",
        "payment_method": "نقدي",
        "treasury_account_id": str(treasury_id),
        "date": "2026-08-01",
    }, follow_redirects=True)

    client.post("/progress_payments", data={
        "project_id": str(pid),
        "subcontractor_id": str(sid),
        "date": "2026-08-02",
        "retention_percentage": "0",
        "tax_percentage": "0",
        "advance_deduction": "0",
        "description": ["أعمال"],
        "unit": ["متر مكعب"],
        "quantity": ["10"],
        "unit_price": ["300"],
    }, follow_redirects=True)

    with flask_app.app_context():
        payment = ProgressPayment.query.filter_by(subcontractor_id=sid).first()
        assert round(payment.advance_deduction, 2) == 0
        assert round(payment.net_value, 2) == 3000.0
        assert get_subcontractor_outstanding_advances(sid) == 2000.0


def test_settlement_does_not_count_as_outstanding_advance(client):
    with flask_app.app_context():
        project = Project(
            code="PRJ-SET", project_name="صرف صافي", client_name="عميل ص",
            contract_type="مقاولات عامة", contract_value=1,
        )
        sub = Subcontractor(name="مقاول صافي")
        db.session.add_all([project, sub])
        db.session.commit()
        pid, sid = project.id, sub.id
        treasury_id, _ = _ids()

    client.post("/progress_payments", data={
        "project_id": str(pid),
        "subcontractor_id": str(sid),
        "date": "2026-08-01",
        "retention_percentage": "10",
        "tax_percentage": "0",
        "advance_deduction": "0",
        "description": ["أعمال"],
        "unit": ["متر مكعب"],
        "quantity": ["10"],
        "unit_price": ["1000"],
    }, follow_redirects=True)

    with flask_app.app_context():
        first = ProgressPayment.query.filter_by(subcontractor_id=sid).first()
        assert round(first.net_value, 2) == 9000.0
        first_id = first.id

    client.post("/subcontractor_payments", data={
        "subcontractor_id": str(sid),
        "project_id": str(pid),
        "progress_payment_id": str(first_id),
        "payment_kind": "صرف مستخلص",
        "amount": "9000",
        "payment_method": "نقدي",
        "treasury_account_id": str(treasury_id),
        "date": "2026-08-03",
        "redirect_to": "progress_payment_detail",
    }, follow_redirects=True)

    with flask_app.app_context():
        assert get_subcontractor_outstanding_advances(sid) == 0
        assert progress_net_remaining(ProgressPayment.query.get(first_id)) == 0

    listing = client.get("/progress_payments").get_data(as_text=True)
    assert "9.000" in listing
    assert "تعديل" in listing

    client.post("/progress_payments", data={
        "project_id": str(pid),
        "subcontractor_id": str(sid),
        "date": "2026-08-10",
        "retention_percentage": "0",
        "tax_percentage": "0",
        "advance_deduction": "0",
        "description": ["أعمال تالية"],
        "unit": ["متر مكعب"],
        "quantity": ["5"],
        "unit_price": ["1000"],
    }, follow_redirects=True)

    with flask_app.app_context():
        second = ProgressPayment.query.filter_by(subcontractor_id=sid).order_by(ProgressPayment.id.desc()).first()
        assert round(second.advance_deduction, 2) == 0
        assert round(second.net_value, 2) == 5000.0
        statement = build_subcontractor_statement(Subcontractor.query.get(sid))
        assert statement["total_works"] == 15000.0
        assert statement["total_paid"] == 9000.0
        assert statement["net_due"] == 5000.0


def test_unlinked_advance_shows_in_progress_register(client):
    with flask_app.app_context():
        project = Project(
            code="PRJ-PAY", project_name="دفعات ظاهرة", client_name="عميل",
            contract_type="مقاولات عامة", contract_value=1,
        )
        sub = Subcontractor(name="مقاول دفعات")
        db.session.add_all([project, sub])
        db.session.commit()
        pid, sid = project.id, sub.id
        treasury_id, _ = _ids()

    client.post("/subcontractor_payments", data={
        "subcontractor_id": str(sid),
        "project_id": str(pid),
        "amount": "2500",
        "payment_kind": "تحت الحساب",
        "payment_method": "نقدي",
        "treasury_account_id": str(treasury_id),
        "date": "2026-08-01",
    }, follow_redirects=True)

    client.post("/progress_payments", data={
        "project_id": str(pid),
        "subcontractor_id": str(sid),
        "date": "2026-08-02",
        "retention_percentage": "0",
        "tax_percentage": "0",
        "advance_deduction": "0",
        "description": ["أعمال"],
        "unit": ["متر مكعب"],
        "quantity": ["5"],
        "unit_price": ["1000"],
    }, follow_redirects=True)

    listing = client.get("/progress_payments").get_data(as_text=True)
    assert "2.500" in listing

    with flask_app.app_context():
        from services.accounting import build_account_balances, get_or_create_subcontractor_account
        payment = SubcontractorPayment.query.filter_by(subcontractor_id=sid).first()
        ipc = ProgressPayment.query.filter_by(subcontractor_id=sid).first()
        journal = JournalEntry.query.filter(JournalEntry.description.like(f"%SPAY-AUTO:{payment.id}%")).first()
        assert journal is not None
        assert round(journal.amount, 2) == 2500
        party = get_or_create_subcontractor_account(Subcontractor.query.get(sid))
        treasury = ChartOfAccount.query.filter_by(code="TRS-MAIN").first()
        balances = build_account_balances()
        assert journal.debit_account_id == party.id
        assert journal.credit_account_id == treasury.id
        assert round(balances.get(treasury.id, 0.0), 2) == -2500
        assert round(balances.get(party.id, 0.0), 2) == -2500
        assert round(ipc.net_value, 2) == 5000
        from services.accounting import build_progress_paid_map, build_progress_remaining_map
        paid_map = build_progress_paid_map([ipc])
        remaining_map = build_progress_remaining_map([ipc])
        assert paid_map[ipc.id] == 2500
        assert remaining_map[ipc.id] == 2500


def test_update_subcontractor_payment_from_progress_and_statement(client):
    with flask_app.app_context():
        project = Project(
            code="PRJ-EDT", project_name="تعديل دفعة", client_name="عميل",
            contract_type="مقاولات عامة", contract_value=1,
        )
        sub = Subcontractor(name="مقاول تعديل")
        db.session.add_all([project, sub])
        db.session.commit()
        pid, sid = project.id, sub.id
        treasury_id, _ = _ids()

    client.post("/subcontractor_payments", data={
        "subcontractor_id": str(sid),
        "project_id": str(pid),
        "amount": "1000",
        "payment_kind": "تحت الحساب",
        "payment_method": "نقدي",
        "treasury_account_id": str(treasury_id),
        "date": "2026-08-01",
    }, follow_redirects=True)

    with flask_app.app_context():
        payment = SubcontractorPayment.query.filter_by(subcontractor_id=sid).first()
        pay_id = payment.id

    updated = client.post(f"/subcontractor_payments/{pay_id}/update", data={
        "amount": "1750",
        "date": "2026-08-02",
        "payment_method": "نقدي",
        "treasury_account_id": str(treasury_id),
        "project_id": str(pid),
        "reference": "تعديل-1",
        "redirect_to": "subcontractor_statement",
    }, follow_redirects=True)
    assert updated.status_code == 200
    body = updated.get_data(as_text=True)
    assert "تم تعديل الدفعة" in body
    assert "1.750" in body

    with flask_app.app_context():
        payment = SubcontractorPayment.query.get(pay_id)
        assert round(payment.amount, 2) == 1750
        journal = JournalEntry.query.filter(JournalEntry.description.like(f"%SPAY-AUTO:{pay_id}%")).first()
        assert journal is not None
        assert round(journal.amount, 2) == 1750


def test_journal_register_has_edit_and_updates_accounts(client):
    with flask_app.app_context():
        treasury_id, expense_id = _ids()

    created = client.post("/journal", data={
        "date": "2026-08-20",
        "entry_action": "post",
        "line_description": ["مصروف تجريبي"],
        "line_debit_account_id": [str(expense_id)],
        "line_credit_account_id": [str(treasury_id)],
        "line_amount": ["300"],
    }, follow_redirects=True)
    assert created.status_code == 200

    listing = client.get("/journal").get_data(as_text=True)
    assert "تعديل" in listing
    assert "js-edit-journal" in listing

    with flask_app.app_context():
        entry = JournalEntry.query.filter(JournalEntry.description.like("%مصروف تجريبي%")).first()
        entry_id = entry.id

    updated = client.post(f"/journal/{entry_id}/update", data={
        "description": "مصروف تجريبي معدل",
        "amount": "450",
        "date": "2026-08-21",
        "debit_account_id": str(expense_id),
        "credit_account_id": str(treasury_id),
        "status": "مرحل",
        "redirect_to": "journal",
    }, follow_redirects=True)
    assert updated.status_code == 200
    assert "تم تحديث القيد" in updated.get_data(as_text=True)

    with flask_app.app_context():
        from services.accounting import build_account_balances
        entry = JournalEntry.query.get(entry_id)
        assert entry is not None
        assert round(entry.amount, 2) == 450
        balances = build_account_balances()
        assert round(balances.get(expense_id, 0.0), 2) == 450


def test_closed_period_does_not_save_progress_payment(client):
    with flask_app.app_context():
        period = AccountingPeriod(name="أغسطس", from_date="2026-08-01", to_date="2026-08-31", status="مغلقة")
        project = Project(
            code="PRJ-CL", project_name="فترة مغلقة", client_name="عميل ط",
            contract_type="مقاولات عامة", contract_value=1,
        )
        sub = Subcontractor(name="مقاول مغلق")
        db.session.add_all([period, project, sub])
        db.session.commit()
        pid, sid = project.id, sub.id

    response = client.post("/progress_payments", data={
        "project_id": str(pid),
        "subcontractor_id": str(sid),
        "date": "2026-08-15",
        "description": ["أعمال"],
        "unit": ["متر مكعب"],
        "quantity": ["1"],
        "unit_price": ["100"],
    }, follow_redirects=True)
    assert "مغلقة" in response.get_data(as_text=True) or "فترة" in response.get_data(as_text=True)
    with flask_app.app_context():
        assert ProgressPayment.query.filter_by(subcontractor_id=sid).count() == 0


def test_progress_number_does_not_reuse_existing(client):
    with flask_app.app_context():
        project = Project(
            code="PRJ-NUM", project_name="أرقام", client_name="عميل ع",
            contract_type="مقاولات عامة", contract_value=1,
        )
        sub = Subcontractor(name="مقاول أرقام")
        db.session.add_all([project, sub])
        db.session.commit()
        first = ProgressPayment(
            project_id=project.id, subcontractor_id=sub.id, payment_number="1", date="2026-08-01",
        )
        third = ProgressPayment(
            project_id=project.id, subcontractor_id=sub.id, payment_number="3", date="2026-08-02",
        )
        db.session.add_all([first, third])
        db.session.commit()
        assert generate_progress_payment_number(project.id, sub.id) == "4"


def test_retention_percent_zero_clears_manual_amount_when_submitted_zero(client):
    with flask_app.app_context():
        project = Project(
            code="PRJ-RET0", project_name="ضمان صفر", client_name="عميل غ",
            contract_type="مقاولات عامة", contract_value=1,
        )
        sub = Subcontractor(name="مقاول ضمان")
        db.session.add_all([project, sub])
        db.session.commit()
        pid, sid = project.id, sub.id

    client.post("/progress_payments", data={
        "project_id": str(pid),
        "subcontractor_id": str(sid),
        "date": "2026-08-01",
        "retention_percentage": "0",
        "discount_insurance": "0",
        "tax_percentage": "0",
        "description": ["أعمال"],
        "unit": ["متر مكعب"],
        "quantity": ["10"],
        "unit_price": ["100"],
    }, follow_redirects=True)

    with flask_app.app_context():
        payment = ProgressPayment.query.filter_by(subcontractor_id=sid).first()
        assert round(payment.discount_insurance or 0, 2) == 0


def test_retention_release_posts_and_reduces_held_amount(client):
    with flask_app.app_context():
        project = Project(
            code="PRJ-REL", project_name="إفراج", client_name="عميل ف",
            contract_type="مقاولات عامة", contract_value=1,
        )
        sub = Subcontractor(name="مقاول إفراج")
        db.session.add_all([project, sub])
        db.session.commit()
        pid, sid = project.id, sub.id

    client.post("/progress_payments", data={
        "project_id": str(pid),
        "subcontractor_id": str(sid),
        "date": "2026-08-01",
        "retention_percentage": "10",
        "description": ["أعمال"],
        "unit": ["متر مكعب"],
        "quantity": ["10"],
        "unit_price": ["1000"],
    }, follow_redirects=True)

    with flask_app.app_context():
        assert get_subcontractor_outstanding_retention(sid) == 1000.0

    client.post("/retention_releases", data={
        "subcontractor_id": str(sid),
        "project_id": str(pid),
        "amount": "400",
        "date": "2026-08-20",
    }, follow_redirects=True)

    with flask_app.app_context():
        assert get_subcontractor_outstanding_retention(sid) == 600.0
        assert SubcontractorRetentionRelease.query.filter_by(subcontractor_id=sid).count() == 1
        statement = build_subcontractor_statement(Subcontractor.query.get(sid))
        assert statement["total_retention_released"] == 400.0
        assert statement["retention_outstanding"] == 600.0
        assert JournalEntry.query.filter(JournalEntry.description.like("%RETREL-AUTO:%")).count() == 1


def test_previous_executed_quantity_excludes_current_certificate(client):
    with flask_app.app_context():
        project = Project(
            code="PRJ-QTY", project_name="كميات", client_name="عميل ك",
            contract_type="مقاولات عامة", contract_value=1,
        )
        sub = Subcontractor(name="مقاول كميات")
        db.session.add_all([project, sub])
        db.session.commit()
        pid, sid = project.id, sub.id

    client.post("/progress_payments", data={
        "project_id": str(pid),
        "subcontractor_id": str(sid),
        "date": "2026-08-01",
        "description": ["ميول خرسانية"],
        "unit": ["متر مسطح"],
        "quantity": ["20"],
        "unit_price": ["50"],
    }, follow_redirects=True)

    with flask_app.app_context():
        first = ProgressPayment.query.filter_by(subcontractor_id=sid).first()
        assert previous_executed_quantity(pid, sid, None, "ميول خرسانية", "متر مسطح", first.id) == 0
        assert previous_executed_quantity(pid, sid, None, "ميول خرسانية", "متر مسطح") == 20.0


def test_can_delete_user_employee_and_unused_parties(client):
    create_user = client.post("/users", data={
        "username": "tempuser",
        "full_name": "مستخدم مؤقت",
        "password": "secret12",
        "role": ROLE_DATA_ENTRY,
        "is_active": "on",
    }, follow_redirects=True)
    assert create_user.status_code == 200
    with flask_app.app_context():
        temp = User.query.filter_by(username="tempuser").first()
        temp_id = temp.id
        admin_id = User.query.filter_by(username="admin").first().id

    self_delete = client.post(f"/users/{admin_id}/delete", follow_redirects=True)
    assert "لا يمكن حذف حسابك" in self_delete.get_data(as_text=True)
    with flask_app.app_context():
        assert User.query.get(admin_id) is not None

    deleted_user = client.post(f"/users/{temp_id}/delete", follow_redirects=True)
    assert "تم حذف المستخدم tempuser" in deleted_user.get_data(as_text=True)
    with flask_app.app_context():
        assert User.query.filter_by(username="tempuser").first() is None

    client.post("/employees", data={
        "name": "موظف مؤقت",
        "tag": "موظف",
        "basic_salary": "0",
    }, follow_redirects=True)
    with flask_app.app_context():
        emp = Employee.query.filter_by(name="موظف مؤقت").first()
        emp_id = emp.id
        emp_account = ChartOfAccount.query.filter_by(code=f"EMP-{emp_id:04d}").first()
        emp_account_id = emp_account.id if emp_account else None

    deleted_emp = client.post(f"/employees/{emp_id}/delete", follow_redirects=True)
    assert "تم حذف الموظف" in deleted_emp.get_data(as_text=True)
    with flask_app.app_context():
        assert Employee.query.get(emp_id) is None
        if emp_account_id:
            assert ChartOfAccount.query.get(emp_account_id) is None

    client.post("/suppliers", data={"name": "مورد مؤقت", "contact_info": "", "notes": ""}, follow_redirects=True)
    with flask_app.app_context():
        supplier = Supplier.query.filter_by(name="مورد مؤقت").first()
        supplier_id = supplier.id
    deleted_supplier = client.post(f"/suppliers/{supplier_id}/delete", follow_redirects=True)
    assert "تم حذف المورد" in deleted_supplier.get_data(as_text=True)
    with flask_app.app_context():
        assert Supplier.query.get(supplier_id) is None

    client.post("/subcontractors", data={"name": "مقاول مؤقت", "entity_kind": "مقاول تنفيذي (مصنعية ومعدات/عمالة)"}, follow_redirects=True)
    with flask_app.app_context():
        sub = Subcontractor.query.filter_by(name="مقاول مؤقت").first()
        sub_id = sub.id
    deleted_sub = client.post(f"/subcontractors/{sub_id}/delete", follow_redirects=True)
    assert "تم حذف جهة التعامل" in deleted_sub.get_data(as_text=True)
    with flask_app.app_context():
        assert Subcontractor.query.get(sub_id) is None


def test_admin_assigns_custom_user_permissions(client):
    from services.authz import ALL, effective_perms

    with flask_app.app_context():
        clerk = User(username="permclerk", full_name="مدخل صلاحيات", role=ROLE_DATA_ENTRY, is_active=True)
        clerk.set_password("secret12")
        db.session.add(clerk)
        db.session.commit()
        clerk_id = clerk.id
        admin_id = User.query.filter_by(username="admin").first().id

    probe = flask_app.test_client()
    with probe.session_transaction() as sess:
        sess["user_id"] = clerk_id
    blocked = probe.get("/accounts", follow_redirects=True)
    assert "ليست لديك صلاحية" in blocked.get_data(as_text=True)

    granted = client.post(f"/users/{clerk_id}/permissions", data={
        "perm": ["accounts", "accounts.create", "accounts.update"],
    }, follow_redirects=True)
    assert "تم تحديث صلاحيات" in granted.get_data(as_text=True)

    listing = probe.get("/accounts")
    assert listing.status_code == 200
    assert "إضافة حساب" in listing.get_data(as_text=True)

    with flask_app.app_context():
        extra = ChartOfAccount(code="PERM-DEL", name="حساب صلاحيات", category="المصروفات", opening_balance=0)
        db.session.add(extra)
        db.session.commit()
        extra_id = extra.id
    probe.post(f"/accounts/{extra_id}/delete", follow_redirects=True)
    with flask_app.app_context():
        assert ChartOfAccount.query.get(extra_id) is not None

    locked = client.post(f"/users/{admin_id}/permissions", data={"perm": ["accounts"]}, follow_redirects=True)
    assert "لا يمكن تقييدها" in locked.get_data(as_text=True)
    with flask_app.app_context():
        admin = User.query.get(admin_id)
        assert ALL in effective_perms(admin)

    denied = probe.post(f"/users/{admin_id}/permissions", data={"perm": ["accounts"]}, follow_redirects=True)
    assert "مدير النظام فقط" in denied.get_data(as_text=True) or "ليست لديك صلاحية" in denied.get_data(as_text=True)


def test_mustafa_gets_purchase_orders_and_unit_list_renders(client):
    from services.authz import dump_permissions, repair_missing_purchase_order_permissions, sanitize_permissions

    with flask_app.app_context():
        clerk = User(username="mustafa.mahmoud", full_name="مصطفى محمود", role=ROLE_DATA_ENTRY, is_active=True)
        clerk.set_password("secret12")
        clerk.permissions_json = dump_permissions(sanitize_permissions(
            ["dashboard", "accounts", "journal.view", "journal.create"], clerk
        ))
        project = Project(
            code="PRJ-MUST", project_name="مشروع مصطفى", client_name="عميل",
            contract_type="مقاولات عامة", contract_value=1,
        )
        supplier = Supplier(name="مورد مصطفى")
        db.session.add_all([clerk, project, supplier])
        db.session.commit()
        clerk_id, pid, sid = clerk.id, project.id, supplier.id
        repair_missing_purchase_order_permissions()

    probe = flask_app.test_client()
    with probe.session_transaction() as sess:
        sess["user_id"] = clerk_id
        sess["admin_amounts_visible"] = True

    listing = probe.get("/purchase_orders")
    body = listing.get_data(as_text=True)
    assert listing.status_code == 200
    assert "أمر شراء جديد" in body
    assert "أوامر الشراء" in body
    assert 'name="unit"' in body
    assert "متر مسطح" in body
    assert "data-allow-custom" in body

    script = probe.get("/static/searchable-select.js").get_data(as_text=True)
    assert "searchable-select-menu-portal" in script
    assert 'document.body.appendChild(menu)' in script

    saved = probe.post("/purchase_orders", data={
        "project_id": str(pid),
        "supplier_id": str(sid),
        "item_name": "حديد",
        "unit": "طن",
        "warehouse_name": "رئيسي",
        "quantity": "2",
        "unit_price": "100",
        "discount": "0",
        "date": "2026-09-28",
        "status": "مفتوح",
    }, follow_redirects=True)
    assert saved.status_code == 200
    with flask_app.app_context():
        order = PurchaseOrder.query.filter_by(project_id=pid).first()
        assert order is not None


def test_permission_screen_covers_each_work_screen(client):
    page = client.get("/users")
    body = page.get_data(as_text=True)
    assert page.status_code == 200
    for key in (
        "inventory.view", "inventory.create", "labor.view", "labor.create",
        "sales.view", "sales.create", "receipts.view", "receipts.update",
        "supplier_payments.view", "custody.view", "custody.create",
        "drivers.view", "equipment.view", "purchase_orders.view",
        "estimations.update", "accounts.view",
    ):
        assert f'value="{key}"' in body


def test_legacy_single_toggle_still_opens_and_saves_inventory(client):
    from services.authz import dump_permissions

    with flask_app.app_context():
        clerk = User(username="invclerk", full_name="مدخل مخزن", role=ROLE_DATA_ENTRY, is_active=True)
        clerk.set_password("secret12")
        clerk.permissions_json = dump_permissions(["dashboard", "inventory"])
        project = Project(
            code="PRJ-INVPERM", project_name="مخزن صلاحيات", client_name="عميل",
            contract_type="مقاولات عامة", contract_value=1,
        )
        db.session.add_all([clerk, project])
        db.session.commit()
        clerk_id, pid = clerk.id, project.id

    probe = flask_app.test_client()
    with probe.session_transaction() as sess:
        sess["user_id"] = clerk_id
        sess["admin_amounts_visible"] = True

    listing = probe.get("/inventory")
    assert listing.status_code == 200
    assert "إدارة المخازن" in listing.get_data(as_text=True)

    saved = probe.post("/inventory", data={
        "project_id": str(pid),
        "material_name": "أسمنت صلاحيات",
        "warehouse_name": "رئيسي",
        "transaction_type": "إضافة",
        "quantity": "3",
        "unit_cost": "10",
        "date": "2026-09-28",
    }, follow_redirects=True)
    assert saved.status_code == 200
    with flask_app.app_context():
        from models import InventoryTransaction
        row = InventoryTransaction.query.filter_by(material_name="أسمنت صلاحيات").first()
        assert row is not None


def test_view_only_inventory_cannot_post(client):
    from services.authz import dump_permissions

    with flask_app.app_context():
        clerk = User(username="invview", full_name="عارض مخزن", role=ROLE_DATA_ENTRY, is_active=True)
        clerk.set_password("secret12")
        clerk.permissions_json = dump_permissions(["dashboard", "inventory.view"])
        db.session.add(clerk)
        db.session.commit()
        clerk_id = clerk.id

    probe = flask_app.test_client()
    with probe.session_transaction() as sess:
        sess["user_id"] = clerk_id

    listing = probe.get("/inventory")
    assert listing.status_code == 200
    blocked = probe.post("/inventory", data={
        "project_id": "1",
        "material_name": "ممنوع",
        "warehouse_name": "رئيسي",
        "transaction_type": "إضافة",
        "quantity": "1",
        "unit_cost": "1",
        "date": "2026-09-28",
    }, follow_redirects=True)
    assert "ليست لديك صلاحية" in blocked.get_data(as_text=True)


def test_open_purchase_order_excluded_from_project_cost(client):
    with flask_app.app_context():
        project = Project(
            code="PRJ-OPENPO", project_name="مفتوح", client_name="عميل مواد",
            contract_type="مقاولات عامة", contract_value=1,
        )
        supplier = Supplier(name="مورد أوامر مفتوحة")
        db.session.add_all([project, supplier])
        db.session.commit()
        pid, sid = project.id, supplier.id

    client.post("/purchase_orders", data={
        "project_id": str(pid),
        "supplier_id": str(sid),
        "item_name": "أسمنت",
        "warehouse_name": "رئيسي",
        "quantity": "10",
        "unit_price": "50",
        "discount": "0",
        "date": "2026-09-01",
        "status": "مفتوح",
    }, follow_redirects=True)

    with flask_app.app_context():
        breakdown = build_project_cost_breakdown(Project.query.get(pid))
        assert breakdown["material_cost"] == 0.0
        order = PurchaseOrder.query.filter_by(project_id=pid).first()
        oid = order.id

    client.post(f"/purchase_orders/{oid}/update", data={
        "project_id": str(pid),
        "supplier_id": str(sid),
        "item_name": "أسمنت",
        "warehouse_name": "رئيسي",
        "quantity": "10",
        "unit_price": "50",
        "discount": "0",
        "date": "2026-09-01",
        "status": "مغلق",
    }, follow_redirects=True)

    with flask_app.app_context():
        breakdown = build_project_cost_breakdown(Project.query.get(pid))
        assert breakdown["material_cost"] == 500.0


def test_journal_reverse_blocked_when_source_period_closed(client):
    with flask_app.app_context():
        treasury_id, expense_id = _ids()

    client.post("/journal", data={
        "date": "2026-08-15",
        "entry_action": "post",
        "line_description": ["قيد للعكس"],
        "line_debit_account_id": [str(expense_id)],
        "line_credit_account_id": [str(treasury_id)],
        "line_amount": ["100"],
    }, follow_redirects=True)

    with flask_app.app_context():
        entry = JournalEntry.query.first()
        assert entry is not None
        eid = entry.id
        db.session.add(AccountingPeriod(
            name="أغسطس عكس", from_date="2026-08-01", to_date="2026-08-31", status="مغلقة",
        ))
        db.session.commit()

    response = client.post(f"/journal/{eid}/reverse", follow_redirects=True)
    assert "مغلقة" in response.get_data(as_text=True)
    with flask_app.app_context():
        assert JournalEntry.query.filter(JournalEntry.description.like("%عكس قيد%")).count() == 0


def test_purchase_order_update_blocked_on_original_closed_date(client):
    with flask_app.app_context():
        project = Project(
            code="PRJ-POCLOSE", project_name="إقفال أمر", client_name="عميل إقفال",
            contract_type="مقاولات عامة", contract_value=1,
        )
        supplier = Supplier(name="مورد إقفال أمر")
        db.session.add_all([project, supplier])
        db.session.commit()
        pid, sid = project.id, supplier.id

    client.post("/purchase_orders", data={
        "project_id": str(pid),
        "supplier_id": str(sid),
        "item_name": "رمل",
        "warehouse_name": "رئيسي",
        "quantity": "2",
        "unit_price": "30",
        "discount": "0",
        "date": "2026-08-10",
        "status": "مغلق",
    }, follow_redirects=True)

    with flask_app.app_context():
        order = PurchaseOrder.query.filter_by(project_id=pid).first()
        oid = order.id
        original_qty = order.total_value
        db.session.add(AccountingPeriod(
            name="أغسطس أوامر", from_date="2026-08-01", to_date="2026-08-31", status="مغلقة",
        ))
        db.session.commit()

    response = client.post(f"/purchase_orders/{oid}/update", data={
        "project_id": str(pid),
        "supplier_id": str(sid),
        "item_name": "رمل",
        "warehouse_name": "رئيسي",
        "quantity": "9",
        "unit_price": "30",
        "discount": "0",
        "date": "2026-09-10",
        "status": "مغلق",
    }, follow_redirects=True)
    assert "مغلقة" in response.get_data(as_text=True)
    with flask_app.app_context():
        order = PurchaseOrder.query.get(oid)
        assert order.date == "2026-08-10"
        assert round(order.total_value, 2) == round(original_qty, 2)


def test_equipment_journal_keeps_original_date(client):
    with flask_app.app_context():
        project = Project(
            code="PRJ-EQP", project_name="معدات", client_name="عميل معدات",
            contract_type="مقاولات عامة", contract_value=1,
        )
        db.session.add(project)
        db.session.commit()
        pid = project.id

    client.post("/equipment", data={
        "name": "لودر اختبار",
        "purchase_cost": "10000",
        "operating_cost": "250",
        "maintenance": "40",
        "hours_used": "5",
        "project_id": str(pid),
    }, follow_redirects=True)

    with flask_app.app_context():
        item = Equipment.query.filter_by(name="لودر اختبار").first()
        assert item is not None
        journals = JournalEntry.query.filter(JournalEntry.description.like(f"%EQP-AUTO:{item.id}%")).all()
        assert journals
        for journal in journals:
            journal.date = "2026-01-20"
        db.session.commit()
        sync_equipment_journals(item)
        refreshed = JournalEntry.query.filter(JournalEntry.description.like(f"%EQP-AUTO:{item.id}%")).all()
        assert refreshed
        assert all(journal.date == "2026-01-20" for journal in refreshed)


def test_dashboard_outstanding_counts_subcontractor_certificates_only(client):
    with flask_app.app_context():
        project = Project(
            code="PRJ-OUT", project_name="مستحقات", client_name="عميل مستحقات",
            contract_type="مقاولات عامة", contract_value=1,
        )
        sub = Subcontractor(name="مقاول مستحقات")
        db.session.add_all([project, sub])
        db.session.commit()
        pid, sid = project.id, sub.id

    client.post("/progress_payments", data={
        "project_id": str(pid),
        "subcontractor_id": str(sid),
        "date": "2026-09-01",
        "description": ["أعمال"],
        "unit": ["مقطوعية"],
        "quantity": ["1"],
        "unit_price": ["800"],
    }, follow_redirects=True)
    client.post("/client_progress_payments", data={
        "project_id": str(pid),
        "date": "2026-09-01",
        "description": ["فاتورة عميل"],
        "unit": ["مقطوعية"],
        "quantity": ["1"],
        "unit_price": ["9000"],
    }, follow_redirects=True)

    with flask_app.app_context():
        sub_count = ProgressPayment.query.filter(
            ProgressPayment.subcontractor_id.isnot(None),
            ProgressPayment.net_value > 0,
        ).count()
        client_count = ProgressPayment.query.filter(
            ProgressPayment.subcontractor_id.is_(None),
            ProgressPayment.net_value > 0,
        ).count()
        assert sub_count == 1
        assert client_count == 1


def test_data_entry_cannot_delete_client_progress_payment(client):
    with flask_app.app_context():
        project = Project(
            code="PRJ-DELIPC", project_name="حذف مستخلص", client_name="عميل حذف",
            contract_type="مقاولات عامة", contract_value=1,
        )
        clerk = User(username="ipcclerk", full_name="مدخل مستخلص", role=ROLE_DATA_ENTRY, is_active=True)
        clerk.set_password("secret12")
        db.session.add_all([project, clerk])
        db.session.commit()
        pid, clerk_id = project.id, clerk.id

    client.post("/client_progress_payments", data={
        "project_id": str(pid),
        "date": "2026-09-01",
        "description": ["بند عميل"],
        "unit": ["مقطوعية"],
        "quantity": ["1"],
        "unit_price": ["1200"],
    }, follow_redirects=True)

    with flask_app.app_context():
        ipc = ProgressPayment.query.filter(
            ProgressPayment.project_id == pid,
            ProgressPayment.subcontractor_id.is_(None),
        ).first()
        assert ipc is not None
        ipc_id = ipc.id

    probe = flask_app.test_client()
    with probe.session_transaction() as sess:
        sess["user_id"] = clerk_id
    response = probe.post(f"/documents/progress_payment/{ipc_id}/delete", follow_redirects=True)
    assert "ليست لديك صلاحية" in response.get_data(as_text=True)
    with flask_app.app_context():
        assert ProgressPayment.query.get(ipc_id) is not None


def test_login_shows_company_brand():
    guest = flask_app.test_client()
    page = guest.get("/login")
    body = page.get_data(as_text=True)
    assert page.status_code == 200
    assert "شركة الشيخ للمقاولات العمومية" in body
    assert "company-logo-desktop.png" in body
    assert "تسجيل الدخول" in body


def test_problem_screens_include_required_fields(client):
    accounts = client.get("/accounts")
    accounts_body = accounts.get_data(as_text=True)
    assert "expense_class" in accounts_body
    assert "js-expense-class-wrap" in accounts_body
    assert "بند أعمال (اختياري)" not in accounts_body
    assert 'name="boq_item_id"' not in accounts_body
    assert 'name="stage"' not in accounts_body
    assert "نوع الحساب" in accounts_body
    assert ">الفئة<" not in accounts_body
    receipts = client.get("/client_receipts")
    receipts_body = receipts.get_data(as_text=True)
    assert "editRecNotes" in receipts_body
    assert "InstaPay" in receipts_body
    assert "محفظة إلكترونية" in receipts_body
    labor = client.get("/labor")
    assert labor.status_code == 200
    assert "إدارة العمالة" in labor.get_data(as_text=True)
    equipment = client.get("/equipment")
    assert equipment.status_code == 200
    estimations = client.get("/estimations")
    body = estimations.get_data(as_text=True)
    assert "est-line-total" in body
    assert "ipc-line" in body
    inventory = client.get("/inventory")
    assert "invLineTotal" in inventory.get_data(as_text=True)
    hr = client.get("/hr")
    assert hr.status_code == 200
    hr_body = hr.get_data(as_text=True)
    assert 'name="attendance_from"' in hr_body
    assert "إجازة مرضية" in hr_body
    assert "حفظ مسودة" in hr_body
    assert "طباعة كشف الحضور" in hr_body
    assert "js-hr-name-filter" in hr_body
    assert "js-hr-pick" in hr_body
    assert "خصم باليومية" in hr_body
    assert "خصم بالفلوس" in hr_body
    assert 'name="daily_deduction_days"' in hr_body
    payments = client.get("/supplier_payments")
    assert "js-searchable-select" in payments.get_data(as_text=True)
    projects = client.get("/projects")
    assert projects.status_code == 200
    journal = client.get("/journal")
    assert "journal-line-row" in journal.get_data(as_text=True)


def test_admin_can_hide_dashboard_from_user(client):
    with flask_app.app_context():
        clerk = User(username="nodash", full_name="بدون رئيسية", role=ROLE_DATA_ENTRY, is_active=True)
        clerk.set_password("secret12")
        db.session.add(clerk)
        db.session.commit()
        clerk_id = clerk.id

    granted = client.post(f"/users/{clerk_id}/permissions", data={
        "perm": ["journal", "journal.view", "journal.create", "hr", "hr.create"],
    }, follow_redirects=True)
    assert "تم تحديث صلاحيات" in granted.get_data(as_text=True)

    probe = flask_app.test_client()
    with probe.session_transaction() as sess:
        sess["user_id"] = clerk_id
    home = probe.get("/", follow_redirects=True)
    body = home.get_data(as_text=True)
    assert "القيود اليومية" in body
    assert "ليست لديك صلاحية" in body
    journal = probe.get("/journal")
    assert journal.status_code == 200
    users_page = client.get("/users")
    assert "الشاشة الرئيسية" in users_page.get_data(as_text=True)


def test_payroll_sheet_matches_payslip_shape_and_prints(client):
    client.post("/employees", data={
        "name": "محمود الزين",
        "tag": "موظف",
        "job_title": "مندوب",
        "hometown": "الجيزة",
        "site": "القاهرة",
        "basic_salary": "7500",
    }, follow_redirects=True)
    page = client.get("/hr")
    body = page.get_data(as_text=True)
    assert "الراتب الأساسى" in body
    assert "موصلات" in body
    assert "الصافى النقدى" in body
    assert "تصدير Excel" in body

    with flask_app.app_context():
        employee_id = Employee.query.filter_by(name="محمود الزين").first().id

    saved = client.post("/hr", data={
        "action": "save_payroll",
        "period_month": "2026-09",
        "posting_date": "2026-09-20",
        "employee_id": [str(employee_id)],
        "work_days": ["30"],
        "vacation_days": ["0"],
        "basic_salary": ["7500"],
        "daily_rate": ["250"],
        "earned_salary": ["7500"],
        "transport": ["0"],
        "overtime": ["0"],
        "incentives": ["0"],
        "delay_deduction": ["0"],
        "permission_deduction": ["0"],
        "other_deductions": ["0"],
        "advances": ["0"],
        "payroll_notes": [""],
    }, follow_redirects=True)
    assert saved.status_code == 200

    with flask_app.app_context():
        slip = PayrollSlip.query.filter_by(employee_id=employee_id, period_month="2026-09").first()
        assert slip is not None
        assert round(slip.net_salary, 2) == 7500
        slip_id = slip.id

    payslip = client.get(f"/hr/payroll/{slip_id}/print")
    payslip_body = payslip.get_data(as_text=True)
    assert payslip.status_code == 200
    assert "قسيمة الراتب الشهرى" in payslip_body
    assert "محمود الزين" in payslip_body
    assert "مندوب" in payslip_body
    assert "IBRAHIM ABD ALKREEM" in payslip_body
    assert "شركة السيد الشيخ للمقاولات العمومية" in payslip_body
    assert "المعصرة" not in payslip_body
    assert "شارع العشرين" not in payslip_body
    assert "30 / 30" in payslip_body

    sheet = client.get("/hr/payroll/print?period_month=2026-09")
    sheet_body = sheet.get_data(as_text=True)
    assert sheet.status_code == 200
    assert "كشف المرتبات" in sheet_body
    assert "محمود الزين" in sheet_body
    assert "شركة السيد الشيخ للمقاولات العمومية" in sheet_body
    assert "7.500" in sheet_body


def test_payroll_absent_days_reduce_salary_automatically(client):
    client.post("/employees", data={
        "name": "سامي حضور",
        "tag": "موظف",
        "basic_salary": "3000",
    }, follow_redirects=True)
    with flask_app.app_context():
        employee_id = Employee.query.filter_by(name="سامي حضور").first().id

    saved = client.post("/hr", data={
        "action": "post_payroll",
        "period_month": "2026-09",
        "posting_date": "2026-09-30",
        "employee_id": [str(employee_id)],
        "work_days": ["25"],
        "vacation_days": ["0"],
        "basic_salary": ["3000"],
        "daily_rate": ["0"],
        "earned_salary": ["3000"],
        "transport": ["0"],
        "overtime": ["0"],
        "incentives": ["0"],
        "delay_deduction": ["0"],
        "permission_deduction": ["0"],
        "other_deductions": ["0"],
        "advances": ["0"],
        "payroll_notes": [""],
    }, follow_redirects=True)
    assert saved.status_code == 200

    with flask_app.app_context():
        slip = PayrollSlip.query.filter_by(employee_id=employee_id, period_month="2026-09").first()
        assert slip is not None
        assert round(slip.work_days, 2) == 25
        assert round(slip.daily_rate, 2) == 100
        assert round(slip.earned_salary, 2) == 2500
        assert round(slip.net_salary, 2) == 2500
        expense = get_account_by_code("EXP-SAL")
        balances = build_account_balances()
        assert round(balances.get(expense.id, 0.0), 2) == 2500
        slip_id = slip.id

    payslip = client.get(f"/hr/payroll/{slip_id}/print")
    body = payslip.get_data(as_text=True)
    assert payslip.status_code == 200
    assert "25 / 30" in body
    assert "2.500" in body
    assert "شركة السيد الشيخ للمقاولات العمومية" in body
    assert "print-watermark" in body


def test_attendance_range_sheet_posts_into_payroll(client):
    client.post("/employees", data={
        "name": "حسين فترة",
        "tag": "موظف",
        "basic_salary": "3000",
    }, follow_redirects=True)
    with flask_app.app_context():
        employee_id = Employee.query.filter_by(name="حسين فترة").first().id

    page = client.get("/hr?period_month=2026-09")
    body = page.get_data(as_text=True)
    assert "من تاريخ" in body
    assert "إجازة مرضية" in body
    assert "ترحيل إلى كشف المرتبات" in body

    draft = client.post("/hr", data={
        "action": "save_attendance",
        "period_month": "2026-09",
        "attendance_from": "2026-09-01",
        "attendance_to": "2026-09-05",
        "att_employee_id": [str(employee_id)],
        "att_status": ["حضور"],
        "att_from": ["2026-09-01"],
        "att_to": ["2026-09-05"],
        "att_check_in": [""],
        "att_check_out": [""],
        "att_notes": [""],
    }, follow_redirects=True)
    assert draft.status_code == 200
    draft_body = draft.get_data(as_text=True)
    assert "5 حضور" in draft_body
    assert "مسودة" in draft_body
    assert "طباعة كشف الحضور" in draft_body
    with flask_app.app_context():
        rows = EmployeeAttendance.query.filter_by(employee_id=employee_id).all()
        assert len(rows) == 5
        assert all(row.record_status == "مسودة" for row in rows)
        assert PayrollSlip.query.filter_by(employee_id=employee_id, period_month="2026-09").first() is None

    printed = client.get("/hr/attendance/print?period_month=2026-09")
    print_body = printed.get_data(as_text=True)
    assert printed.status_code == 200
    assert "حسين فترة" in print_body
    assert "5" in print_body
    assert "print-watermark" in print_body

    exported = client.get("/export/attendance?period_month=2026-09")
    assert exported.status_code == 200
    assert "spreadsheetml" in (exported.mimetype or "")

    posted = client.post("/hr", data={
        "action": "post_attendance",
        "period_month": "2026-09",
        "attendance_from": "2026-09-01",
        "attendance_to": "2026-09-05",
        "att_employee_id": [str(employee_id)],
        "att_status": ["إجازة مرضية"],
        "att_from": ["2026-09-01"],
        "att_to": ["2026-09-05"],
        "att_check_in": [""],
        "att_check_out": [""],
        "att_notes": ["مرض"],
    }, follow_redirects=True)
    assert posted.status_code == 200
    with flask_app.app_context():
        rows = EmployeeAttendance.query.filter_by(employee_id=employee_id).all()
        assert len(rows) == 5
        assert all(row.status == "إجازة مرضية" for row in rows)
        assert all(row.record_status == "مرحل" for row in rows)
        slip = PayrollSlip.query.filter_by(employee_id=employee_id, period_month="2026-09").first()
        assert slip is not None
        assert round(slip.work_days, 2) == 5
        assert round(slip.vacation_days, 2) == 5
        assert round(slip.earned_salary, 2) == 500
    assert "المعصرة" not in body


def test_attendance_month_end_posts_existing_drafts(client):
    client.post("/employees", data={
        "name": "سامي مسودة",
        "tag": "موظف",
        "basic_salary": "3000",
    }, follow_redirects=True)
    with flask_app.app_context():
        employee_id = Employee.query.filter_by(name="سامي مسودة").first().id

    client.post("/hr", data={
        "action": "save_attendance",
        "period_month": "2026-09",
        "att_employee_id": [str(employee_id)],
        "att_status": ["حضور"],
        "att_from": ["2026-09-01"],
        "att_to": ["2026-09-03"],
        "att_check_in": [""],
        "att_check_out": [""],
        "att_notes": [""],
    }, follow_redirects=True)

    posted = client.post("/hr", data={
        "action": "post_attendance",
        "period_month": "2026-09",
        "att_employee_id": [str(employee_id)],
        "att_status": [""],
        "att_from": ["2026-09-01"],
        "att_to": ["2026-09-03"],
        "att_check_in": [""],
        "att_check_out": [""],
        "att_notes": [""],
    }, follow_redirects=True)
    assert posted.status_code == 200
    with flask_app.app_context():
        rows = EmployeeAttendance.query.filter_by(employee_id=employee_id).all()
        assert len(rows) == 3
        assert all(row.status == "حضور" for row in rows)
        assert all(row.record_status == "مرحل" for row in rows)
        slip = PayrollSlip.query.filter_by(employee_id=employee_id, period_month="2026-09").first()
        assert slip is not None
        assert round(slip.work_days, 2) == 3


def test_employee_code_autofills_and_rejects_duplicates(client):
    first = client.post("/employees", data={
        "name": "موظف كود أول",
        "tag": "موظف",
    }, follow_redirects=True)
    assert first.status_code == 200
    with flask_app.app_context():
        first_emp = Employee.query.filter_by(name="موظف كود أول").first()
        assert first_emp.code
        taken_code = first_emp.code

    duplicate = client.post("/employees", data={
        "name": "موظف كود مكرر",
        "tag": "موظف",
        "code": taken_code,
    }, follow_redirects=True)
    body = duplicate.get_data(as_text=True)
    assert "مسجّل على الموظف" in body
    with flask_app.app_context():
        assert Employee.query.filter_by(name="موظف كود مكرر").first() is None

    second = client.post("/employees", data={
        "name": "موظف كود ثاني",
        "tag": "موظف",
    }, follow_redirects=True)
    assert second.status_code == 200
    with flask_app.app_context():
        second_emp = Employee.query.filter_by(name="موظف كود ثاني").first()
        assert second_emp is not None
        assert second_emp.code
        assert second_emp.code != taken_code
        assert first_emp.code == "EMP-0001"
        assert second_emp.code == "EMP-0002"


def test_employee_codes_rebuild_and_clear_continues_sequence(client):
    client.post("/employees", data={"name": "زيد قديم", "tag": "موظف", "code": "ZZ-9"}, follow_redirects=True)
    client.post("/employees", data={"name": "عمر قديم", "tag": "موظف", "code": "AA-1"}, follow_redirects=True)
    with flask_app.app_context():
        from services.accounting import EMPLOYEE_CODE_RESET_MARK, rebuild_employee_codes_once
        ActivityLog.query.filter_by(entity_label=EMPLOYEE_CODE_RESET_MARK).delete()
        db.session.commit()
        rebuild_employee_codes_once()
        db.session.commit()
        rows = Employee.query.order_by(Employee.id).all()
        assert [row.code for row in rows] == ["EMP-0001", "EMP-0002"]
        first_id = rows[0].id

    cleared = client.post(f"/employees/{first_id}/update", data={
        "name": "زيد قديم",
        "tag": "موظف",
        "code": "",
        "basic_salary": "0",
        "is_active": "on",
    }, follow_redirects=True)
    assert cleared.status_code == 200
    with flask_app.app_context():
        first = Employee.query.get(first_id)
        assert first.code == "EMP-0003"


def test_employee_report_screen_and_print(client):
    created = client.post("/employees", data={
        "name": "سعد تقرير",
        "tag": "موظف",
        "job_title": "كاتب",
        "hometown": "الأقصر",
        "site": "توشكى",
        "basic_salary": "4500",
    }, follow_redirects=True)
    assert created.status_code == 200
    with flask_app.app_context():
        employee_id = Employee.query.filter_by(name="سعد تقرير").first().id
        db.session.add(EmployeeAttendance(
            employee_id=employee_id,
            date="2026-09-02",
            status="حضور",
            record_status="مرحل",
        ))
        db.session.commit()

    list_page = client.get("/employees")
    assert "تقرير الموظف" in list_page.get_data(as_text=True)

    report = client.get(f"/employees/{employee_id}/report?from_date=2026-09-01&to_date=2026-09-30")
    body = report.get_data(as_text=True)
    assert report.status_code == 200
    assert "سعد تقرير" in body
    assert "كاتب" in body
    assert "طباعة / PDF" in body
    assert "سجل الحضور" in body
    assert "كشف الحساب" in body
    assert "2026-09-02" in body

    printed = client.get(f"/employees/{employee_id}/report/print?from_date=2026-09-01&to_date=2026-09-30")
    print_body = printed.get_data(as_text=True)
    assert printed.status_code == 200
    assert "سعد تقرير" in print_body
    assert "window.print" in print_body
    assert "طباعة / حفظ PDF" in print_body
    assert "print-watermark" in print_body
    assert "شركة السيد الشيخ" in print_body


def test_account_code_autosequence_and_rejects_duplicates(client):
    page = client.get("/accounts")
    assert "تلقائي إن تُرك فارغًا" in page.get_data(as_text=True)
    first = client.post("/accounts", data={
        "name": "حساب تسلسلي",
        "category": "المصروفات",
        "opening_balance": "0",
    }, follow_redirects=True)
    assert first.status_code == 200
    second = client.post("/accounts", data={
        "name": "حساب تسلسلي ٢",
        "category": "المصروفات",
        "opening_balance": "0",
    }, follow_redirects=True)
    assert second.status_code == 200
    with flask_app.app_context():
        one = ChartOfAccount.query.filter_by(name="حساب تسلسلي").first()
        two = ChartOfAccount.query.filter_by(name="حساب تسلسلي ٢").first()
        assert one.code == "EXP-0001"
        assert two.code == "EXP-0002"
        first_id = one.id

    duplicate = client.post("/accounts", data={
        "name": "حساب مكرر كود",
        "category": "المصروفات",
        "code": "EXP-0001",
        "opening_balance": "0",
    }, follow_redirects=True)
    assert "مسجّل على الحساب" in duplicate.get_data(as_text=True)

    cleared = client.post(f"/accounts/{first_id}/update", data={
        "name": "حساب تسلسلي",
        "category": "المصروفات",
        "code": "",
        "opening_balance": "0",
    }, follow_redirects=True)
    assert cleared.status_code == 200
    with flask_app.app_context():
        one = ChartOfAccount.query.get(first_id)
        assert one.code == "EXP-0003"


def test_expense_class_saved_only_for_expense_accounts(client):
    sneak = client.post("/accounts", data={
        "name": "أصل مع تبويب",
        "category": "الأصول",
        "opening_balance": "0",
        "expense_class": "مباشرة",
    }, follow_redirects=True)
    assert sneak.status_code == 200
    created = client.post("/accounts", data={
        "name": "مصروف مباشر تجريبي",
        "category": "المصروفات",
        "opening_balance": "0",
        "expense_class": "مباشرة",
    }, follow_redirects=True)
    assert created.status_code == 200
    with flask_app.app_context():
        asset = ChartOfAccount.query.filter_by(name="أصل مع تبويب").first()
        expense = ChartOfAccount.query.filter_by(name="مصروف مباشر تجريبي").first()
        assert asset is not None and asset.expense_class is None
        assert expense is not None and expense.expense_class == "مباشرة"
        expense_id = expense.id
        expense_code = expense.code

    changed = client.post(f"/accounts/{expense_id}/update", data={
        "name": "مصروف مباشر تجريبي",
        "category": "الأصول",
        "code": expense_code,
        "opening_balance": "0",
        "expense_class": "مباشرة",
    }, follow_redirects=True)
    assert changed.status_code == 200
    with flask_app.app_context():
        expense = ChartOfAccount.query.get(expense_id)
        assert expense.category == "الأصول"
        assert expense.expense_class is None


def test_data_entry_screens_export_excel(client):
    export = client.get("/export/employees")
    assert export.status_code == 200
    assert "spreadsheetml" in (export.mimetype or "")
    payroll = client.get("/export/payroll?period_month=2026-09")
    assert payroll.status_code == 200
    journal = client.get("/export/journal")
    assert journal.status_code == 200
    hr_page = client.get("/hr")
    assert "/export/payroll" in hr_page.get_data(as_text=True)
    employees_page = client.get("/employees")
    assert "/export/employees" in employees_page.get_data(as_text=True)


def test_sales_trip_sheet_totals_feed_driver_account(client):
    page = client.get("/sales")
    body = page.get_data(as_text=True)
    assert page.status_code == 200
    assert "رقم البون" in body
    assert "صافي الكمية" in body
    assert "تصدير Excel" in body
    assert "طباعة تقرير" in body
    sales_setup = client.get("/reports/print-setup?screen=sales")
    sales_setup_body = sales_setup.get_data(as_text=True)
    assert sales_setup.status_code == 200
    assert "تقرير المبيعات" in sales_setup_body
    assert "العميل" in sales_setup_body
    assert "شجرة الحسابات" in sales_setup_body
    assert 'name="driver_name"' not in sales_setup_body
    assert "ابحث واختر العميل" in body
    assert "ابحث واختر السائق" in body
    assert "رقم القلاب" in body
    assert "رقم المقطورة" in body
    assert "تكعيب القلاب" in body
    assert "تكعيب المقطورة" in body
    assert "إجمالي الكمية" in body
    assert "رقم المعدة" not in body
    assert "name=\"period_label\"" not in body
    assert "line_period_label" not in body

    saved = client.post("/sales", data={
        "line_date": ["2026-09-15", "2026-09-16"],
        "line_voucher_number": ["58927", "58928"],
        "line_trip_type": ["سيارة", "قلاب"],
        "line_client_name": ["الكتيبة", "الكتيبة"],
        "line_notes": ["كسارة القاهرة - الكورنيش", "كسارة القاهرة - المعادي"],
        "line_distance_km": ["110", "40"],
        "line_driver_name": ["شعبان محمد", "كريم علي"],
        "line_tractor_number": ["1412/3673", ""],
        "line_cubage": ["64", "20"],
        "line_discount": ["6", "0"],
        "line_unit_price": ["185", "150"],
        "line_advances": ["0", "0"],
    }, follow_redirects=True)
    assert saved.status_code == 200
    saved_body = saved.get_data(as_text=True)
    assert "58927" in saved_body
    assert "58928" in saved_body
    assert "شعبان محمد" in saved_body
    assert "كريم علي" in saved_body
    assert "10.730" in saved_body

    with flask_app.app_context():
        trip = SalesTrip.query.filter_by(voucher_number="58927").first()
        assert trip is not None
        assert round(trip.net_quantity, 2) == 58
        assert round(trip.total_amount, 2) == 10730
        assert round(trip.remaining, 2) == 10730
        revenue = get_account_by_code("REV-TRP")
        driver_account = ChartOfAccount.query.filter_by(name="سائق - شعبان محمد", category="السواقين").first()
        balances = build_account_balances()
        assert revenue is not None
        assert round(balances.get(revenue.id, 0.0), 2) == -13730
        assert driver_account is not None
        journal = JournalEntry.query.filter(JournalEntry.description.like(f"%SALE-AUTO:{trip.id}%")).first()
        assert journal is not None
        assert journal.credit_account_id == revenue.id
        assert round(balances.get(journal.debit_account_id, 0.0), 2) == 13730
        driver_account_id = driver_account.id

    drivers = client.get("/driver_compensation")
    drivers_body = drivers.get_data(as_text=True)
    assert "شعبان محمد" in drivers_body
    assert "نقلات المبيعات" in drivers_body
    assert "58927" in drivers_body

    driver_statement = client.get(f"/accounts/{driver_account_id}/statement")
    driver_statement_body = driver_statement.get_data(as_text=True)
    assert driver_statement.status_code == 200
    assert "نقلات السائق" in driver_statement_body
    assert "58927" in driver_statement_body
    assert "كسارة القاهرة - الكورنيش" in driver_statement_body
    assert "إجمالي قيمة النقلات" not in driver_statement_body
    assert "10.730" not in driver_statement_body

    printed = client.get("/sales/print")
    printed_body = printed.get_data(as_text=True)
    assert printed.status_code == 200
    assert "شركة السيد الشيخ للمقاولات العمومية" in printed_body
    assert "رقم البون" in printed_body
    assert "10.730" in printed_body
    assert "company-logo-desktop.png" in printed_body
    assert "تقرير المبيعات" in printed_body

    export = client.get("/export/sales")
    assert export.status_code == 200
    assert "spreadsheetml" in (export.mimetype or "")


def test_sales_split_cubage_and_advances_post_to_client_not_driver(client):
    with flask_app.app_context():
        db.session.add_all([
            Equipment(name="قلاب 1412"),
            Equipment(name="مقطورة 8891"),
        ])
        db.session.commit()

    page = client.get("/sales")
    body = page.get_data(as_text=True)
    assert "قلاب 1412" in body
    assert "مقطورة 8891" in body

    saved = client.post("/sales", data={
        "line_date": ["2026-09-20"],
        "line_voucher_number": ["99001"],
        "line_trip_type": ["قلاب"],
        "line_client_name": ["شركة النور"],
        "line_notes": ["الكسارة - الموقع"],
        "line_distance_km": ["80"],
        "line_driver_name": ["أحمد سائق"],
        "line_tractor_number": ["قلاب 1412"],
        "line_trailer_number": ["مقطورة 8891"],
        "line_dump_cubage": ["30"],
        "line_trailer_cubage": ["30"],
        "line_discount": ["5"],
        "line_unit_price": ["200"],
        "line_advances": ["3000"],
    }, follow_redirects=True)
    assert saved.status_code == 200
    saved_body = saved.get_data(as_text=True)
    assert "99001" in saved_body
    assert "8.000" in saved_body
    assert "قلاب 1412" in saved_body
    assert "مقطورة 8891" in saved_body

    with flask_app.app_context():
        trip = SalesTrip.query.filter_by(voucher_number="99001").first()
        assert trip is not None
        assert trip.tractor_number == "قلاب 1412"
        assert trip.trailer_number == "مقطورة 8891"
        assert round(trip.dump_cubage, 2) == 30
        assert round(trip.trailer_cubage, 2) == 30
        assert round(trip.cubage, 2) == 60
        assert round(trip.net_quantity, 2) == 55
        assert round(trip.total_amount, 2) == 11000
        assert round(trip.remaining, 2) == 8000
        revenue = get_account_by_code("REV-TRP")
        treasury = get_account_by_code("TRS-MAIN")
        client_account = ChartOfAccount.query.filter(ChartOfAccount.name.contains("شركة النور")).first()
        driver_account = ChartOfAccount.query.filter_by(name="سائق - أحمد سائق", category="السواقين").first()
        assert client_account is not None
        assert driver_account is not None
        balances = build_account_balances()
        sale_journal = JournalEntry.query.filter(JournalEntry.description.like(f"%SALE-AUTO:{trip.id}%")).first()
        adv_journal = JournalEntry.query.filter(JournalEntry.description.like(f"%SALE-ADV-AUTO:{trip.id}%")).first()
        assert sale_journal is not None
        assert sale_journal.debit_account_id == client_account.id
        assert sale_journal.credit_account_id == revenue.id
        assert round(sale_journal.amount, 2) == 11000
        assert adv_journal is not None
        assert adv_journal.debit_account_id == treasury.id
        assert adv_journal.credit_account_id == client_account.id
        assert round(adv_journal.amount, 2) == 3000
        assert round(balances.get(client_account.id, 0.0), 2) == 8000
        assert round(balances.get(revenue.id, 0.0), 2) == -11000
        assert driver_account is not None
        assert round(balances.get(driver_account.id, 0.0), 2) == 0
        driver_account_id = driver_account.id

    drivers = client.get("/driver_compensation")
    drivers_body = drivers.get_data(as_text=True)
    assert "أحمد سائق" in drivers_body
    assert "99001" in drivers_body
    assert "الكسارة - الموقع" in drivers_body
    assert "تكعيب المبيعات" not in drivers_body
    assert "قيمة النقلات" not in drivers_body
    assert "11.000" not in drivers_body
    assert "3.000" not in drivers_body

    driver_statement = client.get(f"/accounts/{driver_account_id}/statement")
    driver_statement_body = driver_statement.get_data(as_text=True)
    assert driver_statement.status_code == 200
    assert "99001" in driver_statement_body
    assert "الكسارة - الموقع" in driver_statement_body
    assert "إجمالي قيمة النقلات" not in driver_statement_body
    assert "11.000" not in driver_statement_body
    assert "3.000" not in driver_statement_body
    assert "SALE-AUTO" not in driver_statement_body
    assert "SALE-ADV-AUTO" not in driver_statement_body


def test_admin_can_hide_main_treasury_from_user(client):
    with flask_app.app_context():
        main = ChartOfAccount.query.filter_by(code="TRS-MAIN").first()
        site = ChartOfAccount.query.filter_by(code="TRS-SITE").first()
        expense = ChartOfAccount.query.filter_by(code="EXP-ELC").first() or ChartOfAccount.query.filter_by(category="المصروفات").first()
        main_id, site_id, expense_id = main.id, site.id, expense.id
        clerk = User(username="mustafa", full_name="مصطفى", role=ROLE_DATA_ENTRY, is_active=True)
        clerk.set_password("secret12")
        db.session.add(clerk)
        db.session.commit()
        clerk_id = clerk.id

    users_page = client.get("/users")
    users_body = users_page.get_data(as_text=True)
    assert users_page.status_code == 200
    assert "الخزن الظاهرة للمستخدم" in users_body
    assert "TRS-MAIN" in users_body
    assert "modal-dialog-scrollable" in users_body

    saved = client.post(f"/users/{clerk_id}/permissions", data={
        "perm": ["dashboard", "accounts", "accounts.create", "journal", "journal.view", "journal.create"],
        "treasury_scope": "1",
        "visible_treasury": [str(site_id)],
    }, follow_redirects=True)
    assert "تم تحديث صلاحيات" in saved.get_data(as_text=True)

    client.post("/journal", data={
        "date": "2026-09-20",
        "entry_action": "post",
        "line_description": ["صرف من الخزنة الرئيسية للشيخ ابراهيم"],
        "line_reference": ["TRS-HIDE"],
        "line_debit_account_id": [str(expense_id)],
        "line_credit_account_id": [str(main_id)],
        "line_amount": ["2500"],
    }, follow_redirects=True)

    probe = flask_app.test_client()
    with probe.session_transaction() as sess:
        sess["user_id"] = clerk_id

    accounts_body = probe.get("/accounts").get_data(as_text=True)
    assert "TRS-MAIN" not in accounts_body
    assert "الخزنة الرئيسية" not in accounts_body

    blocked = probe.get(f"/accounts/{main_id}/statement", follow_redirects=True)
    assert "ليست لديك صلاحية" in blocked.get_data(as_text=True)

    journal_body = probe.get("/journal").get_data(as_text=True)
    assert "صرف من الخزنة الرئيسية للشيخ ابراهيم" not in journal_body
    assert "TRS-HIDE" not in journal_body

    rejected = probe.post("/journal", data={
        "date": "2026-09-21",
        "entry_action": "draft",
        "line_description": ["محاولة استخدام خزنة محجوبة"],
        "line_debit_account_id": [str(expense_id)],
        "line_credit_account_id": [str(main_id)],
        "line_amount": ["100"],
    }, follow_redirects=True)
    assert "ليست لديك صلاحية استخدام هذه الخزنة" in rejected.get_data(as_text=True)


def test_main_treasury_requires_permission_even_if_listed(client):
    with flask_app.app_context():
        main = ChartOfAccount.query.filter_by(code="TRS-MAIN").first()
        site = ChartOfAccount.query.filter_by(code="TRS-SITE").first()
        bank = ChartOfAccount.query.filter_by(code="TRS-BANK").first()
        main_id, site_id, bank_id = main.id, site.id, bank.id
        clerk = User(username="treasclerk", full_name="مدخل خزنة", role=ROLE_DATA_ENTRY, is_active=True)
        clerk.set_password("secret12")
        db.session.add(clerk)
        db.session.commit()
        clerk_id = clerk.id

    users_body = client.get("/users").get_data(as_text=True)
    assert "استخدام الخزنة الرئيسية" in users_body

    client.post(f"/users/{clerk_id}/permissions", data={
        "perm": ["dashboard", "accounts", "accounts.view", "journal", "journal.view", "journal.create"],
        "treasury_scope": "1",
        "visible_treasury": [str(main_id), str(site_id), str(bank_id)],
    }, follow_redirects=True)

    probe = flask_app.test_client()
    with probe.session_transaction() as sess:
        sess["user_id"] = clerk_id

    journal_body = probe.get("/journal").get_data(as_text=True)
    assert "TRS-MAIN" not in journal_body
    assert "الخزنة الرئيسية" not in journal_body
    assert "TRS-SITE" in journal_body

    client.post(f"/users/{clerk_id}/permissions", data={
        "perm": ["dashboard", "accounts", "accounts.view", "journal", "journal.view", "journal.create", "treasury.main"],
        "treasury_scope": "1",
        "visible_treasury": [str(main_id), str(site_id), str(bank_id)],
    }, follow_redirects=True)

    granted = probe.get("/journal").get_data(as_text=True)
    assert "TRS-MAIN" in granted
    assert "الخزنة الرئيسية" in granted


def test_pin_eye_only_on_main_treasury_and_bank(client):
    accounts_body = client.get("/accounts").get_data(as_text=True)
    assert "wallet-eye-btn" in accounts_body
    assert "TRS-MAIN" in accounts_body
    assert "TRS-BANK" in accounts_body
    assert "TRS-SITE" in accounts_body
    site_idx = accounts_body.find("TRS-SITE")
    site_chunk = accounts_body[site_idx:site_idx + 800]
    assert "wallet-eye-btn" not in site_chunk

    home_body = client.get("/").get_data(as_text=True)
    assert "wallet-eye-btn" in home_body
    assert "رصيد الخزنة" in home_body
    assert "مستحقات للصرف" in home_body


def test_purchase_orders_period_report_prints_with_logo(client):
    with flask_app.app_context():
        project = Project(
            code="PRJ-PO", project_name="تقرير مشتريات", client_name="عميل",
            contract_type="مقاولات عامة", contract_value=1,
        )
        supplier = Supplier(name="مورد تقرير")
        db.session.add_all([project, supplier])
        db.session.commit()
        pid, sid = project.id, supplier.id

    client.post("/purchase_orders", data={
        "project_id": str(pid),
        "supplier_id": str(sid),
        "item_name": "أسمنت",
        "warehouse_name": "رئيسي",
        "quantity": "5",
        "unit_price": "200",
        "discount": "0",
        "date": "2026-09-10",
        "status": "مفتوح",
        "order_number": "PO-REP-1",
    }, follow_redirects=True)

    listing = client.get("/purchase_orders?from_date=2026-09-01&to_date=2026-09-30")
    body = listing.get_data(as_text=True)
    assert listing.status_code == 200
    assert "تقرير المشتريات حسب الفترة" in body
    assert "PO-REP-1" in body
    assert "طباعة تقرير" in body

    empty_period = client.get("/purchase_orders?from_date=2026-01-01&to_date=2026-01-31").get_data(as_text=True)
    assert "PO-REP-1" not in empty_period

    printed = client.get("/purchase_orders/print?from_date=2026-09-01&to_date=2026-09-30")
    print_body = printed.get_data(as_text=True)
    assert printed.status_code == 200
    assert "PO-REP-1" in print_body
    assert "company-logo-desktop.png" in print_body
    assert "تقرير المشتريات" in print_body
    assert "print-watermark" in print_body


def test_driver_sheet_matches_statement_and_prints(client):
    page = client.get("/driver_compensation")
    body = page.get_data(as_text=True)
    assert page.status_code == 200
    assert "حساب سواقين الجرارات و القلابات" in body
    assert "حساب كمسيون النقل" in body
    assert "اجمالى حساب سواق" in body
    assert "مصاريف النقل" in body
    assert "اجمالى النثريات" in body
    assert "صافى حساب السائق" in body
    assert "تعتيق" in body
    assert "مستلزمات (مانديل-معطر-فوط)" in body
    assert "رقم السيارة" in body
    assert "المحاسب" in body
    assert "محاسب اول" not in body
    assert "طباعة تقرير" in body
    assert "النسخ الاحتياطي" in body
    assert ">نقل من الإكسل<" not in body

    saved = client.post("/driver_compensation", data={
        "date": "2026-09-02",
        "driver_code": "3",
        "driver_name": "اسماعيل علي اسماعيل",
        "national_id": "29509022104216",
        "phone": "01003803405",
        "vehicle_type": "جرار",
        "tractor_number": "1412",
        "period_from": "2026-08-26",
        "period_to": "2026-09-01",
        "accountant_name": "مصطفى محمود",
        "accounts_officer": "محمد احمد الدمرداش",
        "sheet_commission": "9250",
        "sheet_seller_fee": "3150",
        "sheet_daily_expense": "2700",
        "sheet_scales": "11690",
        "sheet_cards": "1245",
        "sheet_towing": "200",
        "sheet_loading": "9450",
        "sheet_filters": "350",
        "sheet_spare_parts": "150",
        "sheet_fines": "800",
        "sheet_cash_fees": "555",
        "sheet_other_sundries": "300",
        "sheet_custody": "30000",
    }, follow_redirects=True)
    saved_body = saved.get_data(as_text=True)
    assert saved.status_code == 200
    assert "اسماعيل علي اسماعيل" in saved_body
    assert "39.840" in saved_body
    assert "9.840" in saved_body

    with flask_app.app_context():
        entry = DriverCompensationEntry.query.filter_by(driver_name="اسماعيل علي اسماعيل").first()
        assert entry is not None
        assert round(entry.driver_account_total, 2) == 15100
        assert round(entry.transport_total, 2) == 22585
        assert round(entry.sundries_total, 2) == 2155
        assert round(entry.entitlements_total, 2) == 39840
        assert round(entry.deductions_total, 2) == 30000
        assert round(entry.net_amount, 2) == 9840
        driver_account = ChartOfAccount.query.filter_by(name="سائق - اسماعيل علي اسماعيل", category="السواقين").first()
        expense = ChartOfAccount.query.filter_by(code="EXP-DRV").first()
        work = JournalEntry.query.filter(JournalEntry.description.like(f"%DRV-WORK-AUTO:{entry.id}%")).first()
        ded = JournalEntry.query.filter(JournalEntry.description.like(f"%DRV-DED-AUTO:{entry.id}%")).first()
        assert driver_account is not None
        assert expense is not None
        assert work is not None
        assert round(work.amount, 2) == 39840
        assert work.debit_account_id == expense.id
        assert work.credit_account_id == driver_account.id
        assert ded is not None
        assert round(ded.amount, 2) == 30000
        assert ded.debit_account_id == driver_account.id
        balances = build_account_balances()
        assert round(balances.get(driver_account.id, 0.0), 2) == -9840
        entry_id = entry.id
        treasury_id = ChartOfAccount.query.filter_by(code="TRS-MAIN").first().id

    paid = client.post(f"/driver_compensation/{entry_id}/update", data={
        "date": "2026-09-02",
        "driver_code": "3",
        "driver_name": "اسماعيل علي اسماعيل",
        "national_id": "29509022104216",
        "phone": "01003803405",
        "vehicle_type": "جرار",
        "tractor_number": "1412",
        "period_from": "2026-08-26",
        "period_to": "2026-09-01",
        "accountant_name": "مصطفى محمود",
        "sheet_commission": "9250",
        "sheet_seller_fee": "3150",
        "sheet_daily_expense": "2700",
        "sheet_scales": "11690",
        "sheet_cards": "1245",
        "sheet_towing": "200",
        "sheet_loading": "9450",
        "sheet_filters": "350",
        "sheet_spare_parts": "150",
        "sheet_fines": "800",
        "sheet_cash_fees": "555",
        "sheet_other_sundries": "300",
        "sheet_custody": "30000",
        "paid_amount": "2000",
        "treasury_account_id": str(treasury_id),
    }, follow_redirects=True)
    assert paid.status_code == 200
    assert "تم تحديث كشف حساب السائق" in paid.get_data(as_text=True)

    with flask_app.app_context():
        pay = JournalEntry.query.filter(JournalEntry.description.like(f"%DRV-PAY-AUTO:{entry_id}%")).first()
        driver_account = ChartOfAccount.query.filter_by(name="سائق - اسماعيل علي اسماعيل", category="السواقين").first()
        assert pay is not None
        assert pay.debit_account_id == driver_account.id
        assert pay.credit_account_id == treasury_id
        assert round(pay.amount, 2) == 2000
        balances = build_account_balances()
        assert round(balances.get(driver_account.id, 0.0), 2) == -7840
        assert round(balances.get(treasury_id, 0.0), 2) == -2000

    sheet = client.get(f"/driver_compensation/{entry_id}/print")
    sheet_body = sheet.get_data(as_text=True)
    assert sheet.status_code == 200
    assert "حساب سواقين الجرارات و القلابات" in sheet_body
    assert "اسماعيل علي اسماعيل" in sheet_body
    assert "company-logo-desktop.png" in sheet_body
    assert "ج.م." in sheet_body
    assert "مصطفى محمود" in sheet_body
    assert "المحاسب" in sheet_body
    assert "رقم السيارة" in sheet_body

    setup = client.get("/reports/print-setup?screen=drivers")
    setup_body = setup.get_data(as_text=True)
    assert setup.status_code == 200
    assert "تقرير محاسبة السواقين" in setup_body
    assert "تقرير شامل" in setup_body
    assert "شجرة الحسابات" in setup_body
    assert "السائق" in setup_body

    report = client.get("/reports/print?screen=drivers&from_date=2026-09-01&to_date=2026-09-30", follow_redirects=True)
    report_body = report.get_data(as_text=True)
    assert report.status_code == 200
    assert "تقرير محاسبة السواقين" in report_body
    assert "اسماعيل علي اسماعيل" in report_body
    assert "company-logo-desktop.png" in report_body
    assert "print-watermark" in report_body

    empty_period = client.get("/driver_compensation?from_date=2026-01-01&to_date=2026-01-31").get_data(as_text=True)
    assert "39.840" not in empty_period
    assert "لا توجد كشوف" in empty_period

    export = client.get("/export/drivers")
    assert export.status_code == 200
    assert "spreadsheetml" in (export.mimetype or "")


def test_subcontractor_progress_payment_can_be_edited(client):
    with flask_app.app_context():
        project = Project(
            code="PRJ-EDIT", project_name="تعديل مستخلص", client_name="عميل",
            contract_type="مقاولات عامة", contract_value=1,
        )
        sub = Subcontractor(name="مقاول تعديل")
        db.session.add_all([project, sub])
        db.session.commit()
        pid, sid = project.id, sub.id

    created = client.post("/progress_payments", data={
        "project_id": str(pid),
        "subcontractor_id": str(sid),
        "date": "2026-09-05",
        "retention_percentage": "0",
        "tax_percentage": "0",
        "advance_deduction": "0",
        "description": ["أعمال حفر"],
        "unit": ["متر مكعب"],
        "quantity": ["10"],
        "unit_price": ["100"],
    }, follow_redirects=True)
    assert created.status_code == 200

    with flask_app.app_context():
        payment = ProgressPayment.query.filter_by(subcontractor_id=sid).first()
        assert payment is not None
        assert round(payment.total_value, 2) == 1000
        assert round(payment.net_value, 2) == 1000
        payment_id = payment.id

    detail = client.get(f"/progress_payments/{payment_id}")
    detail_body = detail.get_data(as_text=True)
    assert detail.status_code == 200
    assert "ppEditForm" in detail_body
    assert "حفظ التعديل" in detail_body

    updated = client.post(f"/progress_payments/{payment_id}/update", data={
        "project_id": str(pid),
        "subcontractor_id": str(sid),
        "date": "2026-09-05",
        "retention_percentage": "0",
        "tax_percentage": "0",
        "advance_deduction": "0",
        "description": ["أعمال حفر"],
        "unit": ["متر مكعب"],
        "quantity": ["12"],
        "unit_price": ["150"],
        "boq_item_id": [""],
    }, follow_redirects=True)
    assert updated.status_code == 200
    assert "تم تعديل المستخلص" in updated.get_data(as_text=True)

    with flask_app.app_context():
        payment = ProgressPayment.query.get(payment_id)
        assert round(payment.total_value, 2) == 1800
        assert round(payment.net_value, 2) == 1800
        items = ProgressPaymentItem.query.filter_by(progress_payment_id=payment_id).all()
        assert len(items) == 1
        assert round(items[0].quantity, 2) == 12
        journal = JournalEntry.query.filter(JournalEntry.description.like(f"%PP-AUTO:{payment_id}%")).first()
        assert journal is not None
        assert round(journal.amount, 2) == 1800


def test_admin_amount_eye_needs_pin(client):
    locked_home = client.get("/")
    locked_body = locked_home.get_data(as_text=True)
    assert locked_home.status_code == 200
    assert "••••" in locked_body
    assert "js-secret-amount" in locked_body
    assert "wallet-eye-btn" in locked_body
    assert "walletPinModal" in locked_body

    accounts_page = client.get("/accounts")
    accounts_body = accounts_page.get_data(as_text=True)
    assert "wallet-eye-btn" in accounts_body
    assert "TRS-MAIN" in accounts_body

    wrong = client.post("/account/pin/unlock", data={"pin": "غلط"})
    assert wrong.status_code == 403

    unlocked = client.post("/account/pin/unlock", data={"pin": "123456"})
    assert unlocked.status_code == 200
    assert unlocked.get_json()["ok"] is True
    still_masked = client.get("/").get_data(as_text=True)
    assert "••••" in still_masked
    assert "js-secret-amount" in still_masked

    changed = client.post("/account/pin", data={
        "current_pin": "123456",
        "new_pin": "778899",
        "confirm_pin": "778899",
    }, follow_redirects=True)
    assert "تم حفظ الرقم السري" in changed.get_data(as_text=True)
    assert client.post("/account/pin/unlock", data={"pin": "123456"}).status_code == 403
    assert client.post("/account/pin/unlock", data={"pin": "778899"}).status_code == 200


def test_regular_user_has_own_treasury_pin(client):
    with flask_app.app_context():
        clerk = User(username="pinclerk", full_name="مدخل عين", role=ROLE_DATA_ENTRY, is_active=True)
        clerk.set_password("secret12")
        db.session.add(clerk)
        db.session.commit()
        clerk_id = clerk.id

    probe = flask_app.test_client()
    with probe.session_transaction() as sess:
        sess["user_id"] = clerk_id

    home = probe.get("/")
    home_body = home.get_data(as_text=True)
    assert home.status_code == 200
    assert "wallet-eye-btn" in home_body
    assert "حسابي" in home_body
    assert probe.post("/account/pin/unlock", data={"pin": "123456"}).status_code == 200
    assert probe.post("/account/pin/unlock", data={"pin": "ابراهيم"}).status_code == 403

    account_page = probe.get("/account/password")
    assert "تغيير باسورد الـ PIN" in account_page.get_data(as_text=True)
    saved = probe.post("/account/pin", data={
        "current_pin": "123456",
        "new_pin": "445566",
        "confirm_pin": "445566",
    }, follow_redirects=True)
    assert "تم حفظ الرقم السري" in saved.get_data(as_text=True)
    assert probe.post("/account/pin/unlock", data={"pin": "123456"}).status_code == 403
    assert probe.post("/account/pin/unlock", data={"pin": "445566"}).status_code == 200
    assert client.post("/account/pin/unlock", data={"pin": "123456"}).status_code == 200


def test_live_alerts_cover_ready_cases_only(client):
    from datetime import date, timedelta
    from services.alerts import collect_raw_alerts, visible_alerts_for

    today = date.today()
    month = today.strftime("%Y-%m")
    created = client.post("/employees", data={
        "name": "غائب تنبيه",
        "tag": "موظف",
        "basic_salary": "3000",
    }, follow_redirects=True)
    assert created.status_code == 200
    with flask_app.app_context():
        employee = Employee.query.filter_by(name="غائب تنبيه").first()
        employee_id = employee.id
        for day in range(1, 5):
            db.session.add(EmployeeAttendance(
                employee_id=employee_id,
                date=f"{month}-{day:02d}",
                status="غياب",
                record_status="مرحل",
            ))
        db.session.commit()
        admin = User.query.filter_by(username="admin").first()
        kinds = {item["kind"] for item in collect_raw_alerts(admin, today=today)}
        assert "absence" not in kinds

        db.session.add(EmployeeAttendance(
            employee_id=employee_id,
            date=f"{month}-06",
            status="غياب",
            record_status="مرحل",
        ))
        db.session.add(EmployeeAttendance(
            employee_id=employee_id,
            date=f"{month}-07",
            status="غياب",
            record_status="مرحل",
        ))
        old = (today - timedelta(days=40)).isoformat()
        customer = ChartOfAccount(code="AR-ALERT", name="عميل متأخر تنبيه", category="العملاء")
        revenue = ChartOfAccount.query.filter_by(code="REV-WRK").first()
        db.session.add(customer)
        db.session.flush()
        db.session.add(JournalEntry(
            date=old,
            description="مستحق متأخر",
            debit_account_id=customer.id,
            credit_account_id=revenue.id,
            amount=8500,
            status="مرحل",
        ))
        project = Project(
            code="PRJ-ALERT",
            project_name="عقد منتهي",
            client_name="عميل عقد",
            contract_type="مقاولات عامة",
            contract_value=1000,
            end_date=(today - timedelta(days=2)).isoformat(),
        )
        db.session.add(project)
        db.session.flush()
        db.session.add(PurchaseOrder(
            project_id=project.id,
            item_name="خرسانة",
            date=old,
            status="مدفوع",
            total_value=2500,
        ))
        db.session.add(Equipment(name="لودر صيانة فقط", maintenance=900, hours_used=120))
        db.session.commit()
        customer_id = customer.id
        admin = User.query.filter_by(username="admin").first()
        raw = collect_raw_alerts(admin, today=today)
        kinds = {item["kind"] for item in raw}
        assert "absence" in kinds
        assert "receivable_overdue" in kinds
        assert "contract_ended" in kinds
        assert "cost_overrun" in kinds
        assert "equipment" not in kinds
        assert not any("صيانة" in item["title"] for item in raw)
        assert any("غائب تنبيه" in item["title"] for item in raw)

    inbox = client.get("/alerts")
    body = inbox.get_data(as_text=True)
    assert inbox.status_code == 200
    assert "غائب تنبيه" in body
    assert "عميل متأخر تنبيه" in body
    assert "عقد منتهي" in body
    home = client.get("/")
    home_body = home.get_data(as_text=True)
    assert "تنبيهات تحتاج تصرف" in home_body
    assert "alert-bell" in home_body

    with flask_app.app_context():
        admin = User.query.filter_by(username="admin").first()
        absence = next(item for item in visible_alerts_for(admin, today=today) if item["kind"] == "absence")
        key = absence["key"]
    hidden = client.post("/alerts/dismiss", data={"alert_key": key, "next": "/alerts"}, follow_redirects=True)
    assert hidden.status_code == 200
    assert "غائب تنبيه" not in hidden.get_data(as_text=True)


def test_journal_cost_center_hits_project_cost_and_profit(client):
    page = client.get("/journal")
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    assert "مركز التكلفة" in body
    assert 'name="line_project_id"' in body

    with flask_app.app_context():
        project = Project(
            code="PRJ-CC1", project_name="كوبري النيل", client_name="عميل مركز",
            contract_type="مقاولات عامة", contract_value=10000,
        )
        db.session.add(project)
        db.session.commit()
        pid = project.id
        treasury_id, expense_id = _ids()
        revenue_id = ChartOfAccount.query.filter_by(code="REV-WRK").first().id

    posted = client.post("/journal", data={
        "date": "2026-09-10",
        "entry_action": "post",
        "line_description": ["مصروف موقع"],
        "line_debit_account_id": [str(expense_id)],
        "line_credit_account_id": [str(treasury_id)],
        "line_project_id": [str(pid)],
        "line_amount": ["400"],
    }, follow_redirects=True)
    assert posted.status_code == 200

    draft = client.post("/journal", data={
        "date": "2026-09-10",
        "entry_action": "draft",
        "line_description": ["مسودة لا تُحسب"],
        "line_debit_account_id": [str(expense_id)],
        "line_credit_account_id": [str(treasury_id)],
        "line_project_id": [str(pid)],
        "line_amount": ["999"],
    }, follow_redirects=True)
    assert draft.status_code == 200

    revenue = client.post("/journal", data={
        "date": "2026-09-11",
        "entry_action": "post",
        "line_description": ["إيراد إضافي"],
        "line_debit_account_id": [str(treasury_id)],
        "line_credit_account_id": [str(revenue_id)],
        "line_project_id": [str(pid)],
        "line_amount": ["1500"],
    }, follow_redirects=True)
    assert revenue.status_code == 200

    with flask_app.app_context():
        expense_entry = JournalEntry.query.filter_by(description="مصروف موقع").first()
        revenue_entry = JournalEntry.query.filter_by(description="إيراد إضافي").first()
        assert expense_entry.project_id == pid
        assert expense_entry.cost_center == "مشروع - كوبري النيل (PRJ-CC1)"
        assert revenue_entry.project_id == pid
        project = Project.query.get(pid)
        breakdown = build_project_cost_breakdown(project)
        assert breakdown["other_cost"] == 400.0
        assert breakdown["journal_revenue"] == 1500.0
        result = project_revenue_and_result(project, breakdown)
        assert result["total_revenue"] == 11500.0
        assert result["profit"] == 11100.0
        assert result["result_label"] == "ربح"

    detail = client.get(f"/projects/{pid}")
    body = detail.get_data(as_text=True)
    assert "قيود يومية (مصروف)" in body
    assert "صافي الربح / الخسارة" in body

    listing = client.get(f"/journal?project_id={pid}").get_data(as_text=True)
    assert "كوبري النيل" in listing
    assert "مصروف موقع" in listing


def test_journal_batch_uses_cost_center_per_line(client):
    with flask_app.app_context():
        first = Project(
            code="PRJ-CC-A", project_name="مشروع ألف", client_name="عميل أ",
            contract_type="مقاولات عامة", contract_value=5000,
        )
        second = Project(
            code="PRJ-CC-B", project_name="مشروع باء", client_name="عميل ب",
            contract_type="مقاولات عامة", contract_value=8000,
        )
        db.session.add_all([first, second])
        db.session.commit()
        first_id, second_id = first.id, second.id
        treasury_id, expense_id = _ids()

    posted = client.post("/journal", data={
        "date": "2026-09-12",
        "entry_action": "post",
        "line_description": ["مصروف ألف", "مصروف باء"],
        "line_debit_account_id": [str(expense_id), str(expense_id)],
        "line_credit_account_id": [str(treasury_id), str(treasury_id)],
        "line_project_id": [str(first_id), str(second_id)],
        "line_amount": ["200", "350"],
    }, follow_redirects=True)
    assert posted.status_code == 200

    with flask_app.app_context():
        entry_a = JournalEntry.query.filter_by(description="مصروف ألف").first()
        entry_b = JournalEntry.query.filter_by(description="مصروف باء").first()
        assert entry_a.project_id == first_id
        assert entry_b.project_id == second_id
        assert entry_a.cost_center == "مشروع - مشروع ألف (PRJ-CC-A)"
        assert entry_b.cost_center == "مشروع - مشروع باء (PRJ-CC-B)"
        assert build_project_cost_breakdown(Project.query.get(first_id))["other_cost"] == 200.0
        assert build_project_cost_breakdown(Project.query.get(second_id))["other_cost"] == 350.0


def test_auto_progress_journal_excluded_from_other_cost(client):
    with flask_app.app_context():
        project = Project(
            code="PRJ-CC2", project_name="نفق", client_name="عميل باطن",
            contract_type="مقاولات عامة", contract_value=1,
        )
        sub = Subcontractor(name="باطن مركز")
        db.session.add_all([project, sub])
        db.session.commit()
        pid, sid = project.id, sub.id

    client.post("/progress_payments", data={
        "project_id": str(pid),
        "subcontractor_id": str(sid),
        "date": "2026-09-01",
        "retention_percentage": "0",
        "tax_percentage": "0",
        "description": ["خرسانة"],
        "unit": ["متر مكعب"],
        "quantity": ["2"],
        "unit_price": ["1000"],
    }, follow_redirects=True)

    with flask_app.app_context():
        project = Project.query.get(pid)
        breakdown = build_project_cost_breakdown(project)
        assert breakdown["subcontractor_cost"] == 2000.0
        assert breakdown["other_cost"] == 0.0
        autos = [item for item in JournalEntry.query.filter_by(project_id=pid).all() if item.is_auto]
        assert autos
        assert all(item.project_id == pid for item in autos)


def test_payroll_daily_and_money_deductions_hit_net_and_payslip(client):
    client.post("/employees", data={
        "name": "أحمد خصم",
        "tag": "موظف",
        "basic_salary": "3000",
    }, follow_redirects=True)
    with flask_app.app_context():
        employee_id = Employee.query.filter_by(name="أحمد خصم").first().id

    saved = client.post("/hr", data={
        "action": "post_payroll",
        "period_month": "2026-09",
        "posting_date": "2026-09-30",
        "employee_id": [str(employee_id)],
        "work_days": ["30"],
        "vacation_days": ["0"],
        "basic_salary": ["3000"],
        "transport": ["0"],
        "overtime": ["0"],
        "incentives": ["0"],
        "delay_deduction": ["0"],
        "permission_deduction": ["0"],
        "daily_deduction_days": ["2"],
        "other_deductions": ["50"],
        "advances": ["0"],
        "payroll_notes": [""],
    }, follow_redirects=True)
    assert saved.status_code == 200

    with flask_app.app_context():
        slip = PayrollSlip.query.filter_by(employee_id=employee_id, period_month="2026-09").first()
        assert slip is not None
        assert round(slip.daily_rate or 0, 2) == 100.0
        assert round(slip.daily_deduction_days, 2) == 2.0
        assert round(slip.daily_deduction_amount, 2) == 200.0
        assert round(slip.other_deductions, 2) == 50.0
        assert round(slip.net_salary, 2) == 2750.0
        expense = get_account_by_code("EXP-SAL")
        balances = build_account_balances()
        assert round(balances.get(expense.id, 0.0), 2) == 2750.0
        slip_id = slip.id

    payslip = client.get(f"/hr/payroll/{slip_id}/print")
    body = payslip.get_data(as_text=True)
    assert "خصم باليومية" in body
    assert "خصم نقدي" in body
    report = client.get(f"/employees/{employee_id}/report?from_date=2026-09-01&to_date=2026-09-30")
    report_body = report.get_data(as_text=True)
    assert "خصم يومية" in report_body
    assert "2.750" in report_body.replace(",", "") or "2750" in report_body.replace(",", "").replace(".", "")


def test_hr_actions_honor_selected_employees_only(client):
    client.post("/employees", data={"name": "موظف أول", "tag": "موظف", "basic_salary": "3000"}, follow_redirects=True)
    client.post("/employees", data={"name": "موظف ثاني", "tag": "موظف", "basic_salary": "3000"}, follow_redirects=True)
    with flask_app.app_context():
        first_id = Employee.query.filter_by(name="موظف أول").first().id
        second_id = Employee.query.filter_by(name="موظف ثاني").first().id

    client.post("/hr", data={
        "action": "save_attendance",
        "period_month": "2026-09",
        "attendance_from": "2026-09-01",
        "attendance_to": "2026-09-03",
        "att_selected": [str(first_id)],
        "att_employee_id": [str(first_id), str(second_id)],
        "att_status": ["حضور", "حضور"],
        "att_from": ["2026-09-01", "2026-09-01"],
        "att_to": ["2026-09-03", "2026-09-03"],
        "att_check_in": ["", ""],
        "att_check_out": ["", ""],
        "att_notes": ["", ""],
    }, follow_redirects=True)

    with flask_app.app_context():
        assert EmployeeAttendance.query.filter_by(employee_id=first_id).count() == 3
        assert EmployeeAttendance.query.filter_by(employee_id=second_id).count() == 0
        first_row_ids = [row.id for row in EmployeeAttendance.query.filter_by(employee_id=first_id).all()]

    client.post("/hr", data={
        "action": "save_payroll",
        "period_month": "2026-09",
        "posting_date": "2026-09-30",
        "payroll_selected": [str(first_id)],
        "employee_id": [str(first_id), str(second_id)],
        "work_days": ["30", "30"],
        "vacation_days": ["0", "0"],
        "basic_salary": ["3000", "3000"],
        "transport": ["0", "0"],
        "overtime": ["0", "0"],
        "incentives": ["0", "0"],
        "delay_deduction": ["0", "0"],
        "permission_deduction": ["0", "0"],
        "daily_deduction_days": ["0", "0"],
        "other_deductions": ["0", "0"],
        "advances": ["0", "0"],
        "payroll_notes": ["", ""],
    }, follow_redirects=True)

    with flask_app.app_context():
        assert PayrollSlip.query.filter_by(employee_id=first_id, period_month="2026-09").first() is not None
        assert PayrollSlip.query.filter_by(employee_id=second_id, period_month="2026-09").first() is None

    deleted = client.post("/hr", data={
        "action": "delete_attendance_rows",
        "period_month": "2026-09",
        "att_row_id": [str(first_row_ids[0]), str(first_row_ids[1])],
    }, follow_redirects=True)
    assert deleted.status_code == 200
    with flask_app.app_context():
        assert EmployeeAttendance.query.filter_by(employee_id=first_id).count() == 1







