import json
import os
import zipfile
from datetime import date, datetime
from io import BytesIO

from flask import (
    current_app, flash, g, redirect, render_template, request, send_file, session, url_for,
)
from sqlalchemy import text
from werkzeug.utils import secure_filename

from models import (
    ROLE_ADMIN, AccountingPeriod, ActivityLog, AlertDismissal, AppSetting, BOQItem, ClientReceipt,
    CostEntry, DocumentAttachment, Estimation, EstimationItem, JournalEntry, LaborEntry, Equipment,
    EquipmentMaintenance, ProgressPayment, ProgressPaymentItem, PurchaseOrder, PurchaseOrderItem,
    InventoryTransaction, StockItem, SubcontractorPayment, SubcontractorRetentionRelease,
    SupplierPayment, Project, Subcontractor, Supplier, ChartOfAccount, db, CustodySettlement,
    DriverCompensationEntry, SalesTrip, User, Employee, EmployeeAttendance, PayrollSlip,
    EmployeeSalaryPayment,
)
from services.accounting import (
    PeriodClosedError, UNIT_OPTIONS, as_float, as_int, assert_period_open,
    client_progress_query, executed_quantity_index, format_grouped_number, generate_progress_payment_number,
    get_subcontractor_outstanding_advances, is_operational_treasury_account,
    is_treasury_account, list_client_names, parse_progress_payment_items, previous_executed_quantity,
    recalculate_progress_payment, resolve_client_name, find_client_account,
    persist_synced, sanitize_progress_item_boq, apply_client_to_project, get_or_create_client_account,
    build_account_statement, lookup_existing_account, purge_operating_document, normalize_payment_method, resolve_treasury_account_id, sync_client_receipt_journal,
    sync_equipment_journals, sync_inventory_transaction_journal, sync_labor_journal,
    sync_journal_related_accounts, sync_progress_payment_journals,
    sync_posted_journals_to_documents, sync_purchase_order_journal,
    sync_subcontractor_payment_journal, sync_supplier_payment_journal, posted_source_journal,
)
from services.authz import DOCUMENT_DELETE_PERMS, filter_hidden_treasuries, filter_visible_journals, require_perm, user_can, user_can_see_account
from services.immutability import JOURNAL_SOURCED_LOCK_MESSAGE


ALLOWED_ATTACHMENTS = {"pdf", "png", "jpg", "jpeg", "webp", "doc", "docx", "xls", "xlsx"}


def _upload_folder():
    return current_app.config.get("UPLOAD_FOLDER") or os.path.join(os.path.dirname(__file__), "..", "instance", "uploads")


def save_attachment(entity_type, entity_id, file_storage):
    if not file_storage or not file_storage.filename:
        return None
    filename = secure_filename(file_storage.filename)
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in ALLOWED_ATTACHMENTS:
        raise ValueError("نوع الملف غير مسموح. استخدم PDF أو صورة أو مكتب.")
    stored = f"{entity_type}-{entity_id}-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}-{filename}"
    path = os.path.join(_upload_folder(), stored)
    os.makedirs(_upload_folder(), exist_ok=True)
    file_storage.save(path)
    row = DocumentAttachment(
        entity_type=entity_type,
        entity_id=entity_id,
        original_name=filename,
        stored_name=stored,
        uploaded_at=datetime.utcnow().isoformat(timespec="seconds"),
        uploaded_by=getattr(getattr(g, "current_user", None), "full_name", None),
    )
    db.session.add(row)
    db.session.commit()
    return row


def attachments_for(entity_type, entity_id):
    return DocumentAttachment.query.filter_by(entity_type=entity_type, entity_id=entity_id).order_by(DocumentAttachment.id.desc()).all()


BACKUP_TABLES = [
    ("users", User),
    ("projects", Project),
    ("boq_items", BOQItem),
    ("chart_of_account", ChartOfAccount),
    ("subcontractors", Subcontractor),
    ("suppliers", Supplier),
    ("employees", Employee),
    ("employee_attendance", EmployeeAttendance),
    ("payroll_slips", PayrollSlip),
    ("employee_salary_payments", EmployeeSalaryPayment),
    ("progress_payments", ProgressPayment),
    ("progress_payment_items", ProgressPaymentItem),
    ("subcontractor_payments", SubcontractorPayment),
    ("retention_releases", SubcontractorRetentionRelease),
    ("purchase_orders", PurchaseOrder),
    ("purchase_order_items", PurchaseOrderItem),
    ("stock_item", StockItem),
    ("inventory", InventoryTransaction),
    ("estimations", Estimation),
    ("estimation_items", EstimationItem),
    ("journal_entries", JournalEntry),
    ("client_receipts", ClientReceipt),
    ("supplier_payments", SupplierPayment),
    ("periods", AccountingPeriod),
    ("labor", LaborEntry),
    ("equipment", Equipment),
    ("equipment_maintenance", EquipmentMaintenance),
    ("custody", CustodySettlement),
    ("driver", DriverCompensationEntry),
    ("sales_trip", SalesTrip),
    ("cost_entries", CostEntry),
    ("attachments", DocumentAttachment),
    ("activity_log", ActivityLog),
    ("alert_dismissal", AlertDismissal),
    ("app_setting", AppSetting),
]

REQUIRED_BACKUP_TABLES = frozenset((
    "users",
    "projects",
    "boq_items",
    "chart_of_account",
    "subcontractors",
    "suppliers",
    "employees",
    "employee_attendance",
    "payroll_slips",
    "employee_salary_payments",
    "progress_payments",
    "progress_payment_items",
    "subcontractor_payments",
    "retention_releases",
    "purchase_orders",
    "purchase_order_items",
    "inventory",
    "estimations",
    "estimation_items",
    "journal_entries",
    "client_receipts",
    "supplier_payments",
    "periods",
    "labor",
    "equipment",
    "equipment_maintenance",
    "custody",
    "driver",
    "sales_trip",
    "cost_entries",
    "attachments",
))
OPTIONAL_BACKUP_TABLES = frozenset(("stock_item", "activity_log", "alert_dismissal", "app_setting"))


class BackupValidationError(ValueError):
    pass


def _backup_row_count(data):
    return sum(len(rows) for rows in data.values() if isinstance(rows, list))


def tables_for_payload(data):
    selected = []
    skipped_optional = []
    for name, model in BACKUP_TABLES:
        if name in data:
            selected.append((name, model))
        elif name in REQUIRED_BACKUP_TABLES:
            raise BackupValidationError(f"ملف النسخة ناقص جدول أساسي: {name}")
        else:
            skipped_optional.append(name)
    return selected, skipped_optional


def validate_backup_payload(data):
    if not isinstance(data, dict):
        raise BackupValidationError("ملف النسخة غير صالح: المحتوى ليس بيانات جداول.")
    missing = [name for name in sorted(REQUIRED_BACKUP_TABLES) if name not in data]
    if missing:
        raise BackupValidationError("ملف النسخة ناقص جداول أساسية: " + "، ".join(missing))
    known = {name for name, _model in BACKUP_TABLES}
    for name, rows in data.items():
        if name not in known:
            continue
        if not isinstance(rows, list):
            raise BackupValidationError(f"جدول {name} في النسخة ليس قائمة سجلات.")
        for idx, row in enumerate(rows):
            if not isinstance(row, dict):
                raise BackupValidationError(f"سجل غير صالح في جدول {name} رقم {idx + 1}.")
    return tables_for_payload(data)


def apply_backup_payload(data):
    selected, skipped_optional = validate_backup_payload(data)
    dialect = db.engine.dialect.name
    try:
        for _name, model in reversed(selected):
            model.query.delete()
        for _name, model in selected:
            for row in data.get(_name) or []:
                item = model()
                for column in model.__table__.columns:
                    if column.name in row:
                        setattr(item, column.name, row[column.name])
                db.session.merge(item)
        db.session.flush()
        if dialect == "postgresql":
            for _name, model in selected:
                table = model.__table__.name
                seq = db.session.execute(text(
                    f"SELECT pg_get_serial_sequence('{table}', 'id')"
                )).scalar()
                if seq:
                    db.session.execute(
                        text(f"SELECT setval('{seq}', COALESCE((SELECT MAX(id) FROM {table}), 1))")
                    )
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return {"tables": [name for name, _model in selected], "skipped": skipped_optional}


def register(app):
    @app.route("/client_progress_payments", methods=["GET", "POST"])
    def client_progress_payments():
        sync_journal_related_accounts()
        projects = Project.query.order_by(Project.code).all()
        if request.method == "POST":
            project_id = as_int(request.form.get("project_id"))
            if not project_id:
                flash("يرجى اختيار مشروع صحيح", "danger")
                return redirect(url_for("client_progress_payments"))
            project = Project.query.get(project_id)
            client_name = resolve_client_name(request.form)
            if client_name:
                apply_client_to_project(project, client_name)
            elif not project or not (project.client_name or "").strip():
                flash("يرجى اختيار العميل", "danger")
                return redirect(url_for("client_progress_payments"))
            else:
                get_or_create_client_account(project.client_name)
            item_lines = sanitize_progress_item_boq(parse_progress_payment_items(request.form), project_id)
            if not item_lines:
                flash("يرجى إدخال بند واحد على الأقل بكمية وسعر فئة أكبر من صفر", "danger")
                return redirect(url_for("client_progress_payments"))
            pay_date = request.form.get("date") or date.today().isoformat()
            try:
                assert_period_open(pay_date)
            except PeriodClosedError as exc:
                flash(str(exc), "danger")
                return redirect(url_for("client_progress_payments"))
            payment = ProgressPayment(
                project_id=project_id,
                subcontractor_id=None,
                payment_number=(request.form.get("payment_number") or "").strip() or generate_progress_payment_number(project_id, None),
                date=pay_date,
                period_start=request.form.get("period_start") or None,
                period_end=request.form.get("period_end") or None,
                retention_percentage=as_float(request.form.get("retention_percentage")),
                tax_percentage=as_float(request.form.get("tax_percentage")),
                discount_percentage=as_float(request.form.get("discount_percentage")),
                discount_insurance=as_float(request.form.get("discount_insurance")),
                tax=as_float(request.form.get("tax")),
                discount_value=as_float(request.form.get("discount_value")),
                penalties=as_float(request.form.get("penalties")),
                other_deductions=as_float(request.form.get("other_deductions")),
                notes=request.form.get("notes"),
            )
            db.session.add(payment)
            db.session.flush()
            for line in item_lines:
                db.session.add(ProgressPaymentItem(progress_payment_id=payment.id, **line))
            db.session.flush()
            recalculate_progress_payment(payment)
            try:
                persist_synced(lambda: sync_progress_payment_journals(payment))
            except PeriodClosedError as exc:
                db.session.rollback()
                flash(str(exc), "danger")
                return redirect(url_for("client_progress_payments"))
            flash(
                f"تم حفظ مستخلص العميل {payment.document_number} — إجمالي الأعمال {format_grouped_number(payment.total_value)} وصافي المستحق على العميل {format_grouped_number(payment.net_value)}",
                "success",
            )
            return redirect(url_for("client_progress_payments"))

        payments = client_progress_query().order_by(ProgressPayment.id.desc()).all()
        totals = {
            "works": round(sum(as_float(item.total_value) for item in payments), 2),
            "deductions": round(sum(as_float(item.deductions_total) for item in payments), 2),
            "net": round(sum(as_float(item.net_value) for item in payments), 2),
        }
        client_account_ids = {}
        for item in payments:
            name = item.project.client_name if item.project else None
            if name and name not in client_account_ids:
                account = find_client_account(name)
                client_account_ids[name] = account.id if account else None
        return render_template(
            "client_progress_payments.html",
            payments=payments,
            projects=projects,
            unit_options=UNIT_OPTIONS,
            totals=totals,
            today_date=date.today().isoformat(),
            client_account_ids=client_account_ids,
            executed_qty_rows=executed_quantity_index(),
            client_names=list_client_names(),
        )

    @app.route("/client_receipts", methods=["GET", "POST"])
    def client_receipts():
        sync_journal_related_accounts()
        sync_posted_journals_to_documents()
        projects = Project.query.order_by(Project.code).all()
        accounts = ChartOfAccount.query.order_by(ChartOfAccount.code).all()
        treasury_accounts = filter_hidden_treasuries(
            [account for account in accounts if is_treasury_account(account)]
        )
        if request.method == "POST":
            client_name = resolve_client_name(request.form)
            amount = as_float(request.form.get("amount"))
            if not client_name:
                flash("يرجى اختيار عميل مسجّل أو إدخال اسم عميل جديد", "danger")
                return redirect(url_for("client_receipts"))
            if amount <= 0:
                flash("يرجى إدخال مبلغ أكبر من صفر", "danger")
                return redirect(url_for("client_receipts"))
            method = normalize_payment_method(request.form.get("payment_method"))
            receipt_date = request.form.get("date") or date.today().isoformat()
            try:
                assert_period_open(receipt_date)
            except PeriodClosedError as exc:
                flash(str(exc), "danger")
                return redirect(url_for("client_receipts"))
            receipt = ClientReceipt(
                date=receipt_date,
                client_name=client_name,
                project_id=as_int(request.form.get("project_id")),
                amount=amount,
                payment_method=method,
                treasury_account_id=as_int(request.form.get("treasury_account_id")),
                reference=(request.form.get("reference") or "").strip() or None,
                notes=request.form.get("notes"),
            )
            if receipt.treasury_account_id and not user_can_see_account(receipt.treasury_account_id):
                flash("ليست لديك صلاحية استخدام هذه الخزنة", "danger")
                return redirect(url_for("client_receipts"))
            db.session.add(receipt)
            persist_synced(lambda: sync_client_receipt_journal(receipt))
            uploaded = request.files.get("attachment")
            if uploaded and uploaded.filename:
                try:
                    save_attachment("client_receipt", receipt.id, uploaded)
                except ValueError as exc:
                    flash(str(exc), "danger")
            flash(f"تم تسجيل التحصيل {receipt.document_number} وانعكس على ذمة العميل والخزنة", "success")
            return redirect(url_for("client_receipts"))
        items = ClientReceipt.query.order_by(ClientReceipt.id.desc()).all()
        grouped_attachments = {}
        for row in DocumentAttachment.query.filter_by(entity_type="client_receipt").order_by(DocumentAttachment.id.desc()).all():
            grouped_attachments.setdefault(row.entity_id, []).append(row)
        client_account_ids = {}
        for item in items:
            name = item.client_name
            if name and name not in client_account_ids:
                account = find_client_account(name)
                client_account_ids[name] = account.id if account else None
        return render_template(
            "client_receipts.html",
            items=items,
            projects=projects,
            treasury_accounts=treasury_accounts,
            client_names=list_client_names(),
            today_date=date.today().isoformat(),
            attachments=grouped_attachments,
            client_account_ids=client_account_ids,
        )

    @app.route("/supplier_payments", methods=["GET", "POST"])
    def supplier_payments():
        sync_journal_related_accounts()
        sync_posted_journals_to_documents()
        suppliers = Supplier.query.order_by(Supplier.name).all()
        projects = Project.query.order_by(Project.code).all()
        accounts = ChartOfAccount.query.order_by(ChartOfAccount.code).all()
        treasury_accounts = filter_hidden_treasuries(
            [account for account in accounts if is_treasury_account(account)]
        )
        if request.method == "POST":
            supplier_id = as_int(request.form.get("supplier_id"))
            amount = as_float(request.form.get("amount"))
            if not supplier_id:
                flash("يرجى اختيار المورد", "danger")
                return redirect(url_for("supplier_payments"))
            if amount <= 0:
                flash("يرجى إدخال مبلغ أكبر من صفر", "danger")
                return redirect(url_for("supplier_payments"))
            method = normalize_payment_method(request.form.get("payment_method"))
            pay_date = request.form.get("date") or date.today().isoformat()
            try:
                assert_period_open(pay_date)
            except PeriodClosedError as exc:
                flash(str(exc), "danger")
                return redirect(url_for("supplier_payments"))
            payment = SupplierPayment(
                date=pay_date,
                supplier_id=supplier_id,
                project_id=as_int(request.form.get("project_id")),
                amount=amount,
                payment_method=method,
                treasury_account_id=as_int(request.form.get("treasury_account_id")),
                reference=(request.form.get("reference") or "").strip() or None,
                notes=request.form.get("notes"),
            )
            if payment.treasury_account_id and not user_can_see_account(payment.treasury_account_id):
                flash("ليست لديك صلاحية استخدام هذه الخزنة", "danger")
                return redirect(url_for("supplier_payments"))
            db.session.add(payment)
            persist_synced(lambda: sync_supplier_payment_journal(payment))
            flash(f"تم تسجيل سداد المورد {payment.document_number}", "success")
            return redirect(url_for("supplier_payments"))
        items = SupplierPayment.query.order_by(SupplierPayment.id.desc()).all()
        supplier_account_ids = {}
        for item in items:
            if item.supplier_id and item.supplier_id not in supplier_account_ids:
                account = ChartOfAccount.query.filter_by(code=f"SUP-{item.supplier_id:04d}").first()
                supplier_account_ids[item.supplier_id] = account.id if account else None
        return render_template(
            "supplier_payments.html",
            items=items,
            suppliers=suppliers,
            projects=projects,
            treasury_accounts=treasury_accounts,
            today_date=date.today().isoformat(),
            supplier_account_ids=supplier_account_ids,
        )

    @app.route("/client_receipts/<int:receipt_id>/update", methods=["POST"])
    def update_client_receipt(receipt_id):
        receipt = ClientReceipt.query.get_or_404(receipt_id)
        if posted_source_journal(receipt):
            flash(JOURNAL_SOURCED_LOCK_MESSAGE, "danger")
            return redirect(url_for("client_receipts"))
        client_name = resolve_client_name(request.form) or receipt.client_name
        amount = as_float(request.form.get("amount"))
        if amount <= 0:
            flash("يرجى إدخال مبلغ أكبر من صفر", "danger")
            return redirect(url_for("client_receipts"))
        method = normalize_payment_method(request.form.get("payment_method") or receipt.payment_method)
        receipt_date = request.form.get("date") or receipt.date or date.today().isoformat()
        try:
            assert_period_open(receipt.date)
            assert_period_open(receipt_date)
        except PeriodClosedError as exc:
            flash(str(exc), "danger")
            return redirect(url_for("client_receipts"))
        receipt.date = receipt_date
        receipt.client_name = client_name
        receipt.project_id = as_int(request.form.get("project_id"))
        receipt.amount = amount
        receipt.payment_method = method
        receipt.treasury_account_id = as_int(request.form.get("treasury_account_id"))
        if receipt.treasury_account_id and not user_can_see_account(receipt.treasury_account_id):
            flash("ليست لديك صلاحية استخدام هذه الخزنة", "danger")
            return redirect(url_for("client_receipts"))
        receipt.reference = (request.form.get("reference") or "").strip() or None
        if request.form.get("notes") is not None and "JRN-SRC:" not in (receipt.notes or ""):
            receipt.notes = request.form.get("notes")
        persist_synced(lambda: sync_client_receipt_journal(receipt))
        flash(f"تم تعديل التحصيل {receipt.document_number} على الخزنة وحساب العميل معًا", "success")
        return redirect(url_for("client_receipts"))

    @app.route("/supplier_payments/<int:payment_id>/update", methods=["POST"])
    def update_supplier_payment(payment_id):
        payment = SupplierPayment.query.get_or_404(payment_id)
        if posted_source_journal(payment):
            flash(JOURNAL_SOURCED_LOCK_MESSAGE, "danger")
            return redirect(url_for("supplier_payments"))
        supplier_id = as_int(request.form.get("supplier_id")) or payment.supplier_id
        amount = as_float(request.form.get("amount"))
        if not supplier_id:
            flash("يرجى اختيار المورد", "danger")
            return redirect(url_for("supplier_payments"))
        if amount <= 0:
            flash("يرجى إدخال مبلغ أكبر من صفر", "danger")
            return redirect(url_for("supplier_payments"))
        method = normalize_payment_method(request.form.get("payment_method") or payment.payment_method)
        pay_date = request.form.get("date") or payment.date or date.today().isoformat()
        try:
            assert_period_open(payment.date)
            assert_period_open(pay_date)
        except PeriodClosedError as exc:
            flash(str(exc), "danger")
            return redirect(url_for("supplier_payments"))
        payment.date = pay_date
        payment.supplier_id = supplier_id
        payment.project_id = as_int(request.form.get("project_id"))
        payment.amount = amount
        payment.payment_method = method
        payment.treasury_account_id = as_int(request.form.get("treasury_account_id"))
        if payment.treasury_account_id and not user_can_see_account(payment.treasury_account_id):
            flash("ليست لديك صلاحية استخدام هذه الخزنة", "danger")
            return redirect(url_for("supplier_payments"))
        payment.reference = (request.form.get("reference") or "").strip() or None
        if request.form.get("notes") is not None and "JRN-SRC:" not in (payment.notes or ""):
            payment.notes = request.form.get("notes")
        db.session.flush()
        sync_supplier_payment_journal(payment)
        flash(f"تم تعديل سداد {payment.document_number} على الخزنة وحساب المورد معًا", "success")
        return redirect(url_for("supplier_payments"))

    @app.route("/subcontractor_payments/<int:payment_id>/update", methods=["POST"])
    def update_subcontractor_payment(payment_id):
        payment = SubcontractorPayment.query.get_or_404(payment_id)
        amount = as_float(request.form.get("amount"))
        redirect_target = request.form.get("redirect_to") or "progress_payments"

        def bounce():
            if redirect_target == "subcontractor_statement" and payment.subcontractor_id:
                return redirect(url_for("subcontractor_statement", subcontractor_id=payment.subcontractor_id))
            return redirect(url_for("progress_payments"))

        if posted_source_journal(payment):
            flash(JOURNAL_SOURCED_LOCK_MESSAGE, "danger")
            return bounce()
        if amount <= 0:
            flash("يرجى إدخال مبلغ دفعة أكبر من صفر", "danger")
            return bounce()
        method = normalize_payment_method(request.form.get("payment_method") or payment.payment_method)
        pay_date = request.form.get("date") or payment.date or date.today().isoformat()
        try:
            assert_period_open(payment.date)
            assert_period_open(pay_date)
        except PeriodClosedError as exc:
            flash(str(exc), "danger")
            return bounce()
        payment.date = pay_date
        payment.project_id = as_int(request.form.get("project_id")) if "project_id" in request.form else payment.project_id
        payment.amount = amount
        payment.payment_method = method
        treasury_id = resolve_treasury_account_id(request.form.get("treasury_account_id"))
        if treasury_id:
            payment.treasury_account_id = treasury_id
        if payment.treasury_account_id and not user_can_see_account(payment.treasury_account_id):
            flash("ليست لديك صلاحية استخدام هذه الخزنة", "danger")
            return bounce()
        payment.reference = (request.form.get("reference") or "").strip() or payment.reference
        if request.form.get("notes") is not None and "JRN-SRC:" not in (payment.notes or ""):
            payment.notes = request.form.get("notes")
        try:
            persist_synced(lambda: sync_subcontractor_payment_journal(payment))
        except PeriodClosedError as exc:
            db.session.rollback()
            flash(str(exc), "danger")
            return bounce()
        flash("تم تعديل الدفعة على الخزنة وحساب المقاول معًا", "success")
        return bounce()

    @app.route("/documents/<entity_type>/<int:entity_id>/delete", methods=["POST"])
    def delete_operating_document(entity_type, entity_id):
        redirect_map = {
            "progress_payment": "progress_payments",
            "subcontractor_payment": "progress_payments",
            "retention_release": "progress_payments",
            "estimation": "estimations",
            "purchase_order": "purchase_orders",
            "inventory": "inventory",
            "custody": "custody_settlements",
            "labor": "labor",
            "equipment": "equipment",
            "driver": "driver_compensation",
            "sales_trip": "sales_trips",
            "client_receipt": "client_receipts",
            "supplier_payment": "supplier_payments",
            "payroll_slip": "hr_module",
            "employee_salary_payment": "hr_module",
        }
        target = redirect_map.get(entity_type)
        if not target:
            flash("نوع مستند غير معروف", "danger")
            return redirect(url_for("index"))
        if entity_type == "progress_payment" and ProgressPayment.query.get(entity_id) and not ProgressPayment.query.get(entity_id).subcontractor_id:
            target = "client_progress_payments"
        allowed = DOCUMENT_DELETE_PERMS.get(entity_type, ())
        if not any(user_can(perm) for perm in allowed if str(perm).endswith(".delete")):
            flash("ليست لديك صلاحية حذف هذا المستند", "danger")
            return redirect(url_for(target))

        statement_id = None
        if entity_type == "subcontractor_payment":
            advance = SubcontractorPayment.query.get(entity_id)
            if advance and request.form.get("redirect_to") == "subcontractor_statement":
                statement_id = advance.subcontractor_id
        if entity_type == "retention_release":
            release = SubcontractorRetentionRelease.query.get(entity_id)
            if release and request.form.get("redirect_to") == "subcontractor_statement":
                statement_id = release.subcontractor_id

        model_map = {
            "progress_payment": ProgressPayment,
            "subcontractor_payment": SubcontractorPayment,
            "retention_release": SubcontractorRetentionRelease,
            "estimation": Estimation,
            "purchase_order": PurchaseOrder,
            "inventory": InventoryTransaction,
            "custody": CustodySettlement,
            "labor": LaborEntry,
            "equipment": Equipment,
            "driver": DriverCompensationEntry,
            "sales_trip": SalesTrip,
            "client_receipt": ClientReceipt,
            "supplier_payment": SupplierPayment,
            "payroll_slip": PayrollSlip,
            "employee_salary_payment": EmployeeSalaryPayment,
        }
        model = model_map[entity_type]
        item = model.query.get_or_404(entity_id)
        label = getattr(item, "document_number", None) or f"{entity_id}"
        try:
            purge_operating_document(entity_type, entity_id)
        except (PeriodClosedError, ValueError) as exc:
            flash(str(exc), "danger")
            return redirect(url_for(target))
        flash(
            f"تم حذف {label} من المستند ومن الحسابين معًا (المدين والدائن).",
            "success",
        )
        if statement_id:
            return redirect(url_for("subcontractor_statement", subcontractor_id=statement_id))
        if request.form.get("redirect_to") == "equipment_cycle":
            return redirect(url_for(
                "equipment_cycle",
                tab=(request.form.get("tab") or "works").strip(),
                equipment_id=as_int(request.form.get("equipment_id")),
            ))
        if request.form.get("redirect_to") == "inventory":
            inv_tab = (request.form.get("tab") or "").strip()
            return redirect(url_for("inventory", **({"tab": inv_tab} if inv_tab else {})))
        return redirect(url_for(target))

    @app.route("/periods", methods=["GET", "POST"])
    def accounting_periods():
        if not user_can("periods") and g.current_user.role != ROLE_ADMIN:
            flash("إقفال الفترات متاح للمحاسب ومدير النظام فقط", "danger")
            return redirect(url_for("index"))
        if request.method == "POST":
            name = (request.form.get("name") or "").strip()
            from_date = (request.form.get("from_date") or "").strip()
            to_date = (request.form.get("to_date") or "").strip()
            if not name or not from_date or not to_date or from_date > to_date:
                flash("يرجى إدخال اسم الفترة وتاريخين صحيحين", "danger")
                return redirect(url_for("accounting_periods"))
            period = AccountingPeriod(name=name, from_date=from_date, to_date=to_date, status="مفتوحة", notes=request.form.get("notes"))
            db.session.add(period)
            db.session.commit()
            flash("تم إنشاء الفترة المحاسبية", "success")
            return redirect(url_for("accounting_periods"))
        periods = AccountingPeriod.query.order_by(AccountingPeriod.from_date.desc()).all()
        return render_template("periods.html", periods=periods, today_date=date.today().isoformat())

    @app.route("/periods/<int:period_id>/close", methods=["POST"])
    def close_accounting_period(period_id):
        if not user_can("periods") and g.current_user.role != ROLE_ADMIN:
            flash("ليست لديك صلاحية إقفال الفترة", "danger")
            return redirect(url_for("index"))
        period = AccountingPeriod.query.get_or_404(period_id)
        period.status = "مغلقة"
        period.closed_at = datetime.utcnow().isoformat(timespec="seconds")
        period.closed_by = g.current_user.full_name
        db.session.commit()
        flash(f"تم إقفال الفترة {period.name}. لن يمكن تعديل القيود المرحلة داخلها.", "success")
        return redirect(url_for("accounting_periods"))

    @app.route("/periods/<int:period_id>/reopen", methods=["POST"])
    def reopen_accounting_period(period_id):
        if g.current_user.role != ROLE_ADMIN:
            flash("إعادة فتح الفترة متاحة لمدير النظام فقط", "danger")
            return redirect(url_for("accounting_periods"))
        period = AccountingPeriod.query.get_or_404(period_id)
        period.status = "مفتوحة"
        period.closed_at = None
        period.closed_by = None
        db.session.commit()
        flash("تم إعادة فتح الفترة", "success")
        return redirect(url_for("accounting_periods"))

    @app.route("/attachments/<entity_type>/<int:entity_id>", methods=["POST"])
    def upload_attachment(entity_type, entity_id):
        try:
            save_attachment(entity_type, entity_id, request.files.get("attachment"))
        except ValueError as exc:
            flash(str(exc), "danger")
            return redirect(request.referrer or url_for("index"))
        flash("تم رفع المرفق", "success")
        return redirect(request.referrer or url_for("index"))

    @app.route("/attachments/<int:attachment_id>/download")
    def download_attachment(attachment_id):
        item = DocumentAttachment.query.get_or_404(attachment_id)
        path = os.path.join(_upload_folder(), item.stored_name)
        if not os.path.exists(path):
            flash("الملف غير موجود على الخادم", "danger")
            return redirect(request.referrer or url_for("index"))
        return send_file(path, as_attachment=True, download_name=item.original_name)

    @app.route("/print/<doc_type>/<int:doc_id>")
    def print_document(doc_type, doc_id):
        if doc_type == "progress":
            payment = ProgressPayment.query.get_or_404(doc_id)
            items = ProgressPaymentItem.query.filter_by(progress_payment_id=payment.id).all()
            for item in items:
                item.previous_qty = previous_executed_quantity(
                    payment.project_id,
                    payment.subcontractor_id,
                    item.boq_item_id,
                    item.description,
                    item.unit,
                    payment.id,
                )
            journals = JournalEntry.query.filter(JournalEntry.description.like(f"%PP-AUTO:{payment.id}%")).all()
            from app import _print_stamp
            return render_template(
                "print_progress.html",
                payment=payment,
                items=items,
                journals=journals,
                **_print_stamp(
                    f"مستخلص {payment.document_number}",
                    "قسم المشروعات",
                    "مدير المشروعات",
                ),
            )
        if doc_type == "statement":
            subcontractor = Subcontractor.query.get_or_404(doc_id)
            from services.accounting import build_subcontractor_statement
            statement = build_subcontractor_statement(subcontractor)
            from app import _print_stamp
            return render_template(
                "print_statement.html",
                subcontractor=subcontractor,
                statement=statement,
                **_print_stamp(
                    f"كشف حساب {subcontractor.name}",
                    "قسم الحسابات",
                    "المحاسب",
                ),
            )
        if doc_type == "supplier":
            supplier = Supplier.query.get_or_404(doc_id)
            return redirect(url_for("supplier_statement", supplier_id=supplier.id))
        if doc_type == "journal":
            entry = JournalEntry.query.get_or_404(doc_id)
            if not filter_visible_journals([entry]):
                flash("ليست لديك صلاحية عرض هذا القيد لأنه يخص خزنة محجوبة", "danger")
                return redirect(url_for("journal"))
            from app import _print_stamp
            return render_template(
                "print_journal.html",
                entry=entry,
                **_print_stamp(
                    f"قيد يومية {entry.display_number}",
                    "قسم الحسابات",
                    "المحاسب",
                ),
            )
        if doc_type == "account":
            from routes.catalog import build_driver_trip_summary

            account = ChartOfAccount.query.get_or_404(doc_id)
            if not user_can_see_account(account):
                flash("ليست لديك صلاحية عرض هذه الخزنة أو حركتها", "danger")
                return redirect(url_for("accounts"))
            from_date = (request.args.get("from_date") or "").strip()
            to_date = (request.args.get("to_date") or "").strip()
            statement = build_account_statement(account, from_date or None, to_date or None)
            driver_trips = build_driver_trip_summary(account, from_date, to_date)
            from app import _print_stamp
            return render_template(
                "print_account_statement.html",
                statement=statement,
                driver_trips=driver_trips,
                **_print_stamp(
                    f"كشف حساب {account.name}",
                    "قسم الحسابات",
                    "المحاسب",
                ),
            )
        flash("نوع مستند الطباعة غير معروف", "danger")
        return redirect(url_for("index"))

    @app.route("/admin/backup")
    def download_backup():
        payload = {}
        for name, model in BACKUP_TABLES:
            rows = []
            for item in model.query.all():
                row = {}
                for column in model.__table__.columns:
                    row[column.name] = getattr(item, column.name)
                rows.append(row)
            payload[name] = rows
        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("backup.json", json.dumps(payload, ensure_ascii=False, default=str, indent=2))
            upload_dir = _upload_folder()
            if os.path.isdir(upload_dir):
                for filename in os.listdir(upload_dir):
                    archive.write(os.path.join(upload_dir, filename), arcname=f"uploads/{os.path.basename(filename)}")
        buffer.seek(0)
        stamp = date.today().isoformat()
        return send_file(buffer, as_attachment=True, download_name=f"muhasebbat-backup-{stamp}.zip", mimetype="application/zip")

    @app.route("/admin/restore", methods=["GET", "POST"])
    def restore_backup():
        if request.method == "POST":
            if not user_can("backup.restore"):
                flash("ليست لديك صلاحية استعادة نسخة احتياطية", "danger")
                return redirect(url_for("restore_backup") if user_can("backup.download") else url_for("index"))
            uploaded = request.files.get("backup_file")
            if not uploaded or not (uploaded.filename or "").lower().endswith(".zip"):
                flash("يرجى رفع ملف zip للنسخة الاحتياطية", "danger")
                return redirect(url_for("restore_backup"))
            try:
                with zipfile.ZipFile(BytesIO(uploaded.read())) as archive:
                    if archive.testzip() is not None:
                        raise BackupValidationError("ملف ZIP تالف ولا يمكن استعادته.")
                    if "backup.json" not in archive.namelist():
                        raise BackupValidationError("ملف النسخة لا يحتوي backup.json.")
                    data = json.loads(archive.read("backup.json").decode("utf-8"))
                    selected, skipped_optional = validate_backup_payload(data)
                    record_count = _backup_row_count(data)
                    if request.form.get("confirm_restore") == "on":
                        apply_backup_payload(data)
                        upload_dir = _upload_folder()
                        os.makedirs(upload_dir, exist_ok=True)
                        for info in archive.infolist():
                            if info.filename.startswith("uploads/") and not info.is_dir():
                                target = os.path.join(upload_dir, os.path.basename(info.filename))
                                if not target or target.endswith(("\\", "/")):
                                    continue
                                with archive.open(info) as src, open(target, "wb") as dest:
                                    dest.write(src.read())
                        session.clear()
                        extra = ""
                        if skipped_optional:
                            extra = " جداول أحدث غير موجودة في هذه النسخة تُركت كما هي: " + "، ".join(skipped_optional) + "."
                        flash(
                            f"تمت استعادة {record_count} سجلًا من {len(selected)} جدولًا.{extra} سجّل الدخول مجددًا.",
                            "success",
                        )
                        return redirect(url_for("login"))
                extra = ""
                if skipped_optional:
                    extra = " جداول اختيارية غير موجودة في الملف ولن تُمس: " + "، ".join(skipped_optional) + "."
                flash(
                    f"تم التحقق من النسخة ({record_count} سجل في {len(selected)} جدول).{extra} لتطبيقها فعّل تأكيد الاستعادة.",
                    "success",
                )
            except BackupValidationError as exc:
                db.session.rollback()
                flash(str(exc), "danger")
            except Exception:
                db.session.rollback()
                flash("تعذر قراءة أو استعادة النسخة. لم تُحذف بيانات القاعدة الحالية.", "danger")
            return redirect(url_for("restore_backup"))
        return render_template("backup_restore.html")
