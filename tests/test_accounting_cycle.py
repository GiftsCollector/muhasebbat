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
    ROLE_ADMIN, ROLE_DATA_ENTRY, AccountingPeriod, ChartOfAccount, Estimation, JournalEntry,
    ProgressPayment, Project, Subcontractor, Supplier, User, db, PurchaseOrder,
    InventoryTransaction, ClientReceipt, SubcontractorPayment, SupplierPayment,
)
from services.accounting import (
    build_account_balances, build_project_cost_breakdown, build_subcontractor_statement,
    get_subcontractor_outstanding_advances, sync_journal_related_accounts,
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


def test_delete_client_ipc_voids_journals(client):
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
        assert JournalEntry.query.filter(JournalEntry.description.like(f"%PP-AUTO:{payment_id}%")).count() == 1

    client.post(f"/documents/progress_payment/{payment_id}/delete", follow_redirects=True)
    with flask_app.app_context():
        assert ProgressPayment.query.get(payment_id) is None
        assert JournalEntry.query.filter(JournalEntry.description.like(f"%PP-AUTO:{payment_id}%")).count() == 0


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
