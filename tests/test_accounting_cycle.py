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
    ROLE_ADMIN, ROLE_DATA_ENTRY, AccountingPeriod, ActivityLog, ChartOfAccount, Estimation, JournalEntry,
    ProgressPayment, Project, Subcontractor, Supplier, User, db, PurchaseOrder,
    InventoryTransaction, ClientReceipt, SubcontractorPayment, SupplierPayment, Employee,
    PayrollSlip, EmployeeSalaryPayment,
)
from services.accounting import (
    build_account_balances, build_project_cost_breakdown, build_subcontractor_statement,
    get_account_by_code, get_or_create_employee_account, get_subcontractor_outstanding_advances,
    sync_journal_related_accounts,
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
        tx = InventoryTransaction.query.filter(InventoryTransaction.notes.like(f"%PO-AUTO:{order.id}%")).first()
        assert tx is not None
        inv_journals = JournalEntry.query.filter(JournalEntry.description.like(f"%INV-AUTO:{tx.id}%")).all()
        po_journals = JournalEntry.query.filter(JournalEntry.description.like(f"%PO-JRN-AUTO:{order.id}%")).all()
        assert po_journals and round(po_journals[0].amount, 2) == 1000.0
        assert inv_journals == []


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
    assert response.status_code == 200
    assert "تغيير كلمة المرور" in response.get_data(as_text=True)


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
        "advance_deduction": "0",
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
        "work_days": ["22"],
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

