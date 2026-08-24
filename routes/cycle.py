import json
import os
import zipfile
from datetime import date, datetime
from io import BytesIO

from flask import (
    current_app, flash, g, redirect, render_template, request, send_file, session, url_for,
)
from werkzeug.utils import secure_filename

from models import (
    ROLE_ADMIN, AccountingPeriod, BOQItem, ClientReceipt, CostEntry, DocumentAttachment,
    Estimation, EstimationItem, JournalEntry, LaborEntry, Equipment, ProgressPayment,
    ProgressPaymentItem, PurchaseOrder, InventoryTransaction, SubcontractorPayment,
    SupplierPayment, Project, Subcontractor, Supplier, ChartOfAccount, db,
    CustodySettlement, DriverCompensationEntry, User,
)
from services.accounting import (
    PeriodClosedError, UNIT_OPTIONS, as_float, as_int, assert_period_open,
    client_progress_query, format_grouped_number, generate_progress_payment_number,
    get_subcontractor_outstanding_advances, is_operational_treasury_account,
    is_treasury_account, list_client_names, parse_progress_payment_items,
    recalculate_progress_payment, resolve_client_name, find_client_account,
    build_account_statement, sync_client_receipt_journal,
    sync_equipment_journals, sync_inventory_transaction_journal, sync_labor_journal,
    sync_journal_related_accounts, sync_progress_payment_journals,
    sync_posted_journals_to_documents, sync_purchase_order_journal,
    sync_subcontractor_payment_journal, sync_supplier_payment_journal, void_document_journals,
)
from services.authz import require_perm, user_can


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
    ("progress_payments", ProgressPayment),
    ("progress_payment_items", ProgressPaymentItem),
    ("subcontractor_payments", SubcontractorPayment),
    ("purchase_orders", PurchaseOrder),
    ("inventory", InventoryTransaction),
    ("estimations", Estimation),
    ("estimation_items", EstimationItem),
    ("journal_entries", JournalEntry),
    ("client_receipts", ClientReceipt),
    ("supplier_payments", SupplierPayment),
    ("periods", AccountingPeriod),
    ("labor", LaborEntry),
    ("equipment", Equipment),
    ("custody", CustodySettlement),
    ("driver", DriverCompensationEntry),
    ("cost_entries", CostEntry),
    ("attachments", DocumentAttachment),
]


def apply_backup_payload(data):
    for name, model in reversed(BACKUP_TABLES):
        model.query.delete()
    db.session.commit()
    for name, model in BACKUP_TABLES:
        for row in data.get(name) or []:
            item = model()
            for column in model.__table__.columns:
                if column.name in row:
                    setattr(item, column.name, row[column.name])
            db.session.merge(item)
        db.session.commit()
    dialect = db.engine.dialect.name
    if dialect == "postgresql":
        from sqlalchemy import text
        with db.engine.begin() as connection:
            for name, model in BACKUP_TABLES:
                table = model.__table__.name
                seq = connection.execute(text(
                    f"SELECT pg_get_serial_sequence('{table}', 'id')"
                )).scalar()
                if seq:
                    connection.execute(
                        text(f"SELECT setval('{seq}', COALESCE((SELECT MAX(id) FROM {table}), 1))")
                    )


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
            item_lines = parse_progress_payment_items(request.form)
            if not item_lines:
                flash("يرجى إدخال بند واحد على الأقل", "danger")
                return redirect(url_for("client_progress_payments"))
            payment = ProgressPayment(
                project_id=project_id,
                subcontractor_id=None,
                payment_number=(request.form.get("payment_number") or "").strip() or generate_progress_payment_number(project_id, None),
                date=request.form.get("date") or date.today().isoformat(),
                period_start=request.form.get("period_start") or None,
                period_end=request.form.get("period_end") or None,
                retention_percentage=as_float(request.form.get("retention_percentage")),
                tax_percentage=as_float(request.form.get("tax_percentage")),
                penalties=as_float(request.form.get("penalties")),
                other_deductions=as_float(request.form.get("other_deductions")),
                notes=request.form.get("notes"),
            )
            db.session.add(payment)
            db.session.commit()
            for line in item_lines:
                db.session.add(ProgressPaymentItem(progress_payment_id=payment.id, **line))
            db.session.commit()
            recalculate_progress_payment(payment)
            db.session.commit()
            try:
                assert_period_open(payment.date)
                sync_progress_payment_journals(payment)
            except PeriodClosedError as exc:
                flash(str(exc), "danger")
                return redirect(url_for("client_progress_payments"))
            flash(
                f"تم حفظ مستخلص العميل {payment.document_number} وإثبات الإيراد بقيمة {format_grouped_number(payment.total_value)}",
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
        )

    @app.route("/client_receipts", methods=["GET", "POST"])
    def client_receipts():
        sync_journal_related_accounts()
        sync_posted_journals_to_documents()
        projects = Project.query.order_by(Project.code).all()
        accounts = ChartOfAccount.query.order_by(ChartOfAccount.code).all()
        treasury_accounts = [account for account in accounts if is_treasury_account(account)]
        if request.method == "POST":
            client_name = resolve_client_name(request.form)
            amount = as_float(request.form.get("amount"))
            if not client_name:
                flash("يرجى اختيار عميل مسجّل أو إدخال اسم عميل جديد", "danger")
                return redirect(url_for("client_receipts"))
            if amount <= 0:
                flash("يرجى إدخال مبلغ أكبر من صفر", "danger")
                return redirect(url_for("client_receipts"))
            method = request.form.get("payment_method") or "نقدي"
            if method not in ("نقدي", "بنكي"):
                method = "نقدي"
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
            db.session.add(receipt)
            db.session.commit()
            sync_client_receipt_journal(receipt)
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
        treasury_accounts = [account for account in accounts if is_treasury_account(account)]
        if request.method == "POST":
            supplier_id = as_int(request.form.get("supplier_id"))
            amount = as_float(request.form.get("amount"))
            if not supplier_id:
                flash("يرجى اختيار المورد", "danger")
                return redirect(url_for("supplier_payments"))
            if amount <= 0:
                flash("يرجى إدخال مبلغ أكبر من صفر", "danger")
                return redirect(url_for("supplier_payments"))
            method = request.form.get("payment_method") or "نقدي"
            if method not in ("نقدي", "بنكي"):
                method = "نقدي"
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
            db.session.add(payment)
            db.session.commit()
            sync_supplier_payment_journal(payment)
            flash(f"تم تسجيل سداد المورد {payment.document_number}", "success")
            return redirect(url_for("supplier_payments"))
        items = SupplierPayment.query.order_by(SupplierPayment.id.desc()).all()
        return render_template(
            "supplier_payments.html",
            items=items,
            suppliers=suppliers,
            projects=projects,
            treasury_accounts=treasury_accounts,
            today_date=date.today().isoformat(),
        )

    @app.route("/documents/<entity_type>/<int:entity_id>/delete", methods=["POST"])
    def delete_operating_document(entity_type, entity_id):
        redirect_map = {
            "progress_payment": "progress_payments",
            "subcontractor_payment": "progress_payments",
            "estimation": "estimations",
            "purchase_order": "purchase_orders",
            "inventory": "inventory",
            "custody": "custody_settlements",
            "labor": "labor",
            "equipment": "equipment",
            "driver": "driver_compensation",
            "client_receipt": "client_receipts",
            "supplier_payment": "supplier_payments",
        }
        target = redirect_map.get(entity_type)
        if not target:
            flash("نوع مستند غير معروف", "danger")
            return redirect(url_for("index"))
        if entity_type == "progress_payment" and ProgressPayment.query.get(entity_id) and not ProgressPayment.query.get(entity_id).subcontractor_id:
            target = "client_progress_payments"

        model_map = {
            "progress_payment": ProgressPayment,
            "subcontractor_payment": SubcontractorPayment,
            "estimation": Estimation,
            "purchase_order": PurchaseOrder,
            "inventory": InventoryTransaction,
            "custody": CustodySettlement,
            "labor": LaborEntry,
            "equipment": Equipment,
            "driver": DriverCompensationEntry,
            "client_receipt": ClientReceipt,
            "supplier_payment": SupplierPayment,
        }
        model = model_map[entity_type]
        item = model.query.get_or_404(entity_id)
        item_date = getattr(item, "date", None) or date.today().isoformat()
        try:
            assert_period_open(item_date)
            void_document_journals(entity_type, entity_id)
        except PeriodClosedError as exc:
            flash(str(exc), "danger")
            return redirect(url_for(target))

        if entity_type == "progress_payment":
            ProgressPaymentItem.query.filter_by(progress_payment_id=item.id).delete()
        if entity_type == "purchase_order":
            auto_tx = InventoryTransaction.query.filter(InventoryTransaction.notes.like(f"%PO-AUTO:{item.id}%")).first()
            if auto_tx:
                void_document_journals("inventory", auto_tx.id)
                db.session.delete(auto_tx)
        db.session.delete(item)
        db.session.commit()
        flash("تم إلغاء المستند وعكس قيوده التلقائية", "success")
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
            journals = JournalEntry.query.filter(JournalEntry.description.like(f"%PP-AUTO:{payment.id}%")).all()
            return render_template("print_progress.html", payment=payment, items=items, journals=journals)
        if doc_type == "statement":
            subcontractor = Subcontractor.query.get_or_404(doc_id)
            from services.accounting import build_subcontractor_statement
            statement = build_subcontractor_statement(subcontractor)
            return render_template("print_statement.html", subcontractor=subcontractor, statement=statement)
        if doc_type == "supplier":
            supplier = Supplier.query.get_or_404(doc_id)
            return redirect(url_for("supplier_statement", supplier_id=supplier.id))
        if doc_type == "journal":
            entry = JournalEntry.query.get_or_404(doc_id)
            return render_template("print_journal.html", entry=entry)
        if doc_type == "account":
            account = ChartOfAccount.query.get_or_404(doc_id)
            from_date = (request.args.get("from_date") or "").strip()
            to_date = (request.args.get("to_date") or "").strip()
            statement = build_account_statement(account, from_date or None, to_date or None)
            return render_template("print_account_statement.html", statement=statement)
        flash("نوع مستند الطباعة غير معروف", "danger")
        return redirect(url_for("index"))

    @app.route("/admin/backup")
    def download_backup():
        if g.current_user.role != ROLE_ADMIN:
            flash("النسخ الاحتياطي متاح لمدير النظام فقط", "danger")
            return redirect(url_for("index"))
        models = BACKUP_TABLES
        payload = {}
        for name, model in models:
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
                    archive.write(os.path.join(upload_dir, filename), arcname=f"uploads/{filename}")
        buffer.seek(0)
        stamp = date.today().isoformat()
        return send_file(buffer, as_attachment=True, download_name=f"muhasebbat-backup-{stamp}.zip", mimetype="application/zip")

    @app.route("/admin/restore", methods=["GET", "POST"])
    def restore_backup():
        if g.current_user.role != ROLE_ADMIN:
            flash("الاستعادة متاحة لمدير النظام فقط", "danger")
            return redirect(url_for("index"))
        if request.method == "POST":
            uploaded = request.files.get("backup_file")
            if not uploaded or not uploaded.filename.endswith(".zip"):
                flash("يرجى رفع ملف zip للنسخة الاحتياطية", "danger")
                return redirect(url_for("restore_backup"))
            try:
                with zipfile.ZipFile(BytesIO(uploaded.read())) as archive:
                    data = json.loads(archive.read("backup.json").decode("utf-8"))
                    upload_dir = _upload_folder()
                    os.makedirs(upload_dir, exist_ok=True)
                    for info in archive.infolist():
                        if info.filename.startswith("uploads/") and not info.is_dir():
                            target = os.path.join(upload_dir, os.path.basename(info.filename))
                            with archive.open(info) as src, open(target, "wb") as dest:
                                dest.write(src.read())
                record_count = sum(len(v) for v in data.values() if isinstance(v, list))
                if request.form.get("confirm_restore") == "on":
                    apply_backup_payload(data)
                    session.clear()
                    flash(f"تمت استعادة {record_count} سجلًا والمرفقات. سجّل الدخول مجددًا.", "success")
                    return redirect(url_for("login"))
                flash(
                    f"تم التحقق من النسخة ({record_count} سجل). لتطبيقها على القاعدة الحالية فعّل تأكيد الاستعادة.",
                    "success",
                )
            except Exception as exc:
                flash(f"تعذر قراءة أو استعادة النسخة: {exc}", "danger")
            return redirect(url_for("restore_backup"))
        return render_template("backup_restore.html")
