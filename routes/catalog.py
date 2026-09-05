from datetime import date

from flask import flash, redirect, render_template, request, url_for
from sqlalchemy.exc import IntegrityError

from models import (
    BOQItem, ChartOfAccount, CustodySettlement, DocumentAttachment,
    DriverCompensationEntry, InventoryTransaction, JournalEntry, ProgressPayment,
    ProgressPaymentItem, Project, PurchaseOrder, Subcontractor, SubcontractorPayment,
    Supplier, db,
)
from services.accounting import (
    ENTITY_KIND_OPTIONS, EXPENSE_CLASS_OPTIONS, PeriodClosedError, UNIT_OPTIONS,
    as_float, as_int, assert_period_open, build_account_balances, build_account_statement,
    build_project_cost_breakdown, build_subcontractor_statement, format_grouped_number,
    generate_progress_payment_number, get_account_by_code, get_subcontractor_outstanding_advances,
    get_treasury_balance, is_operational_treasury_account, is_treasury_account,
    list_client_names, parse_progress_payment_items, recalculate_progress_payment,
    resolve_client_name, subcontractor_progress_query, sync_journal_related_accounts,
    sync_posted_journals_to_documents, sync_progress_payment_journals,
    sync_subcontractor_payment_journal,
)
from services.immutability import (
    ACCOUNT_DELETE_MESSAGE, OPENING_BALANCE_LOCK_MESSAGE,
    account_has_posted_moves, account_ids_with_posted_moves,
)


def register(app):


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
            "المناديب",
            "الموظفين",
            "المشاريع",
            "حقوق الملكية",
            "رأس المال",
        ]

        if request.method == "POST":
            code = (request.form.get("code") or "").strip()
            name = (request.form.get("name") or "").strip()
            if not code or not name:
                flash("يرجى إدخال كود الحساب واسم الحساب", "danger")
                return redirect(url_for("accounts"))

            expense_class = (request.form.get("expense_class") or "").strip()
            account = ChartOfAccount(
                code=code,
                name=name,
                category=request.form.get("category"),
                project_id=as_int(request.form.get("project_id")),
                boq_item_id=as_int(request.form.get("boq_item_id")),
                stage=request.form.get("stage"),
                opening_balance=as_float(request.form.get("opening_balance")),
                expense_class=expense_class if expense_class in EXPENSE_CLASS_OPTIONS else None,
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
        posted_account_ids = account_ids_with_posted_moves()
        return render_template(
            "accounts.html",
            charts=charts,
            projects=projects,
            boq_items=boq_items,
            category_options=category_options,
            account_balances=account_balances,
            treasury_balance=treasury_balance,
            expense_class_options=EXPENSE_CLASS_OPTIONS,
            posted_account_ids=posted_account_ids,
        )


    @app.route("/accounts/<int:account_id>/update", methods=["POST"])
    def update_account(account_id):
        account = ChartOfAccount.query.get_or_404(account_id)
        code = (request.form.get("code") or "").strip()
        name = (request.form.get("name") or "").strip()
        if not code or not name:
            flash("يرجى إدخال كود الحساب واسم الحساب", "danger")
            return redirect(url_for("accounts"))

        requested_opening = as_float(request.form.get("opening_balance"))
        if abs(requested_opening - as_float(account.opening_balance)) > 0.009 and account_has_posted_moves(account.id):
            flash(OPENING_BALANCE_LOCK_MESSAGE, "danger")
            return redirect(url_for("accounts"))

        account.code = code
        account.name = name
        account.category = request.form.get("category")
        account.project_id = as_int(request.form.get("project_id"))
        account.boq_item_id = as_int(request.form.get("boq_item_id"))
        account.stage = request.form.get("stage")
        if abs(requested_opening - as_float(account.opening_balance)) > 0.009:
            account.opening_balance = requested_opening
        if "expense_class" in request.form:
            expense_class = (request.form.get("expense_class") or "").strip()
            account.expense_class = expense_class if expense_class in EXPENSE_CLASS_OPTIONS else None
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
        ChartOfAccount.query.get_or_404(account_id)
        flash(ACCOUNT_DELETE_MESSAGE, "danger")
        return redirect(url_for("accounts"))


    @app.route("/accounts/<int:account_id>/statement")
    def account_statement(account_id):
        sync_journal_related_accounts()
        account = ChartOfAccount.query.get_or_404(account_id)
        from_date = (request.args.get("from_date") or "").strip()
        to_date = (request.args.get("to_date") or "").strip()
        statement = build_account_statement(account, from_date or None, to_date or None)
        return render_template(
            "account_statement.html",
            statement=statement,
            from_date=from_date,
            to_date=to_date,
        )


    @app.route("/projects", methods=["GET", "POST"])
    def projects():
        if request.method == "POST":
            client_name = resolve_client_name(request.form)
            if not client_name:
                flash("يرجى اختيار عميل مسجّل أو إدخال اسم عميل جديد", "danger")
                return redirect(url_for("projects"))
            project = Project(
                code=request.form.get("code"),
                project_name=request.form.get("project_name"),
                client_name=client_name,
                contract_value=as_float(request.form.get("contract_value")),
                start_date=request.form.get("start_date") or None,
                end_date=request.form.get("end_date") or None,
                contract_type=request.form.get("contract_type"),
                admin_percentage=as_float(request.form.get("admin_percentage")),
            )
            db.session.add(project)
            db.session.commit()
            sync_journal_related_accounts()
            flash("تم إضافة المشروع بنجاح", "success")
            return redirect(url_for("projects"))

        items = Project.query.order_by(Project.start_date.desc()).all()
        return render_template("projects.html", items=items, client_names=list_client_names())


    @app.route("/projects/<int:project_id>/update", methods=["POST"])
    def update_project(project_id):
        project = Project.query.get_or_404(project_id)
        project.code = request.form.get("code")
        project.project_name = request.form.get("project_name")
        project.client_name = resolve_client_name(request.form) or project.client_name
        project.contract_value = as_float(request.form.get("contract_value"))
        project.start_date = request.form.get("start_date") or None
        project.end_date = request.form.get("end_date") or None
        project.contract_type = request.form.get("contract_type")
        project.admin_percentage = as_float(request.form.get("admin_percentage"))
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
        purchase_orders = PurchaseOrder.query.filter_by(project_id=project.id).all()
        inventory_transactions = InventoryTransaction.query.filter_by(project_id=project.id).all()
        breakdown = build_project_cost_breakdown(project)
        cost_summary = {
            "total_cost": breakdown["total_cost"],
            "materials": breakdown["material_cost"],
            "labor": breakdown["labor_cost"],
            "equipment": breakdown["equipment_cost"],
            "indirect": breakdown["custody_cost"] + breakdown["driver_cost"],
            "supervision": breakdown["subcontractor_cost"],
            "overhead": breakdown["admin_allocation"],
        }
        return render_template(
            "project_detail.html",
            project=project,
            boq_items=boq_items,
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
        sync_journal_related_accounts()
        sync_posted_journals_to_documents()
        projects = Project.query.order_by(Project.code).all()
        subcontractors = Subcontractor.query.order_by(Subcontractor.name).all()
        boq_items = BOQItem.query.order_by(BOQItem.name).all()
        accounts = ChartOfAccount.query.order_by(ChartOfAccount.category, ChartOfAccount.code).all()
        treasury_accounts = [account for account in accounts if is_operational_treasury_account(account)]

        if request.method == "POST":
            project_id = as_int(request.form.get("project_id"))
            subcontractor_id = as_int(request.form.get("subcontractor_id"))
            if not project_id:
                flash("يرجى اختيار مشروع صحيح", "danger")
                return redirect(url_for("progress_payments"))
            if not subcontractor_id:
                flash("مستخلص مقاول الباطن يتطلب اختيار المقاول. لمستخلص العميل استخدم صفحة مستخلصات العملاء.", "danger")
                return redirect(url_for("progress_payments"))

            item_lines = parse_progress_payment_items(request.form)
            if not item_lines:
                flash("يرجى إدخال بند واحد على الأقل بكمية وسعر فئة أكبر من صفر", "danger")
                return redirect(url_for("progress_payments"))

            advance_deduction = as_float(request.form.get("advance_deduction"))
            outstanding_advances = get_subcontractor_outstanding_advances(subcontractor_id)
            if advance_deduction <= 0 and outstanding_advances > 0:
                works_total = round(sum(as_float(line["value"]) for line in item_lines), 2)
                advance_deduction = min(outstanding_advances, works_total)
            if subcontractor_id and advance_deduction > outstanding_advances + 0.01:
                flash(
                    f"الدفعات تحت الحساب المتاحة للخصم هي {format_grouped_number(outstanding_advances)} فقط",
                    "danger",
                )
                return redirect(url_for("progress_payments"))

            payment_number = (request.form.get("payment_number") or "").strip()
            payment = ProgressPayment(
                project_id=project_id,
                subcontractor_id=subcontractor_id,
                payment_number=payment_number or generate_progress_payment_number(project_id, subcontractor_id),
                date=request.form.get("date") or date.today().isoformat(),
                period_start=request.form.get("period_start") or None,
                period_end=request.form.get("period_end") or None,
                retention_percentage=as_float(request.form.get("retention_percentage")),
                tax_percentage=as_float(request.form.get("tax_percentage")),
                discount_insurance=as_float(request.form.get("discount_insurance")),
                tax=as_float(request.form.get("tax")),
                penalties=as_float(request.form.get("penalties")),
                other_deductions=as_float(request.form.get("other_deductions")),
                advance_deduction=advance_deduction,
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
                return redirect(url_for("progress_payments"))

            flash(
                f"تم حفظ المستخلص رقم {payment.document_number}: "
                f"إجمالي الأعمال {format_grouped_number(payment.total_value)} - "
                f"الاستقطاعات {format_grouped_number(payment.deductions_total)} - "
                f"دفعات تحت الحساب {format_grouped_number(payment.advance_deduction)} - "
                f"الصافي المستحق {format_grouped_number(payment.net_value)}",
                "success",
            )
            return redirect(url_for("progress_payments"))

        payments = subcontractor_progress_query().order_by(ProgressPayment.id.desc()).all()
        outstanding_advances_map = {
            item.id: get_subcontractor_outstanding_advances(item.id) for item in subcontractors
        }
        totals = {
            "works": round(sum(as_float(item.total_value) for item in payments), 2),
            "deductions": round(sum(as_float(item.deductions_total) for item in payments), 2),
            "advances": round(sum(as_float(item.advance_deduction) for item in payments), 2),
            "net": round(sum(as_float(item.net_value) for item in payments), 2),
        }
        return render_template(
            "progress_payments.html",
            payments=payments,
            projects=projects,
            subcontractors=subcontractors,
            boq_items=boq_items,
            unit_options=UNIT_OPTIONS,
            treasury_accounts=treasury_accounts,
            outstanding_advances_map=outstanding_advances_map,
            totals=totals,
            today_date=date.today().isoformat(),
        )


    @app.route("/progress_payments/<int:payment_id>/update", methods=["POST"])
    def update_progress_payment(payment_id):
        payment = ProgressPayment.query.get_or_404(payment_id)
        item_lines = parse_progress_payment_items(request.form)
        if not item_lines:
            flash("يرجى إدخال بند واحد على الأقل بكمية وسعر فئة أكبر من صفر", "danger")
            return redirect(url_for("progress_payment_detail", payment_id=payment.id))

        pay_date = request.form.get("date") or payment.date or date.today().isoformat()
        try:
            assert_period_open(payment.date)
            assert_period_open(pay_date)
        except PeriodClosedError as exc:
            flash(str(exc), "danger")
            return redirect(url_for("progress_payment_detail", payment_id=payment.id))

        payment.project_id = as_int(request.form.get("project_id")) or payment.project_id
        if "subcontractor_id" in request.form:
            payment.subcontractor_id = as_int(request.form.get("subcontractor_id")) or None
        payment.payment_number = (request.form.get("payment_number") or "").strip() or payment.payment_number
        payment.date = pay_date
        payment.period_start = request.form.get("period_start") or None
        payment.period_end = request.form.get("period_end") or None
        payment.retention_percentage = as_float(request.form.get("retention_percentage"))
        payment.tax_percentage = as_float(request.form.get("tax_percentage"))
        payment.discount_insurance = as_float(request.form.get("discount_insurance"))
        payment.tax = as_float(request.form.get("tax"))
        payment.penalties = as_float(request.form.get("penalties"))
        payment.other_deductions = as_float(request.form.get("other_deductions"))
        payment.advance_deduction = as_float(request.form.get("advance_deduction"))
        payment.notes = request.form.get("notes")

        ProgressPaymentItem.query.filter_by(progress_payment_id=payment.id).delete()
        for line in item_lines:
            db.session.add(ProgressPaymentItem(progress_payment_id=payment.id, **line))
        db.session.flush()
        recalculate_progress_payment(payment)
        db.session.flush()
        sync_progress_payment_journals(payment)
        flash("تم تعديل المستخلص وانعكس على الحسابين المرتبطين معًا", "success")
        return redirect(url_for("progress_payment_detail", payment_id=payment.id))


    @app.route("/progress_payments/<int:payment_id>")
    def progress_payment_detail(payment_id):
        payment = ProgressPayment.query.get_or_404(payment_id)
        items = ProgressPaymentItem.query.filter_by(progress_payment_id=payment.id).all()
        boq_items = BOQItem.query.order_by(BOQItem.name).all()
        linked_journals = JournalEntry.query.filter(
            JournalEntry.description.like(f"%PP-AUTO:{payment.id}%")
        ).order_by(JournalEntry.id.asc()).all()
        attachments = DocumentAttachment.query.filter_by(entity_type="progress_payment", entity_id=payment.id).all()
        return render_template(
            "progress_payment_detail.html",
            payment=payment,
            items=items,
            boq_items=boq_items,
            unit_options=UNIT_OPTIONS,
            linked_journals=linked_journals,
            attachments=attachments,
        )


    @app.route("/subcontractor_payments", methods=["POST"])
    def create_subcontractor_payment():
        """تسجيل دفعة نقدية/بنكية تحت الحساب لمقاول الباطن (SRS 2.2)."""
        sync_journal_related_accounts()
        subcontractor_id = as_int(request.form.get("subcontractor_id"))
        amount = as_float(request.form.get("amount"))
        redirect_target = request.form.get("redirect_to") or "progress_payments"

        if not subcontractor_id:
            flash("يرجى اختيار مقاول الباطن", "danger")
            return redirect(url_for("progress_payments"))
        if amount <= 0:
            flash("يرجى إدخال مبلغ دفعة أكبر من صفر", "danger")
            return redirect(url_for("progress_payments"))

        payment_method = request.form.get("payment_method") or "نقدي"
        treasury_account_id = as_int(request.form.get("treasury_account_id"))
        if not treasury_account_id:
            fallback = get_account_by_code("TRS-MAIN")
            treasury_account_id = fallback.id if fallback else None

        advance = SubcontractorPayment(
            subcontractor_id=subcontractor_id,
            project_id=as_int(request.form.get("project_id")),
            progress_payment_id=as_int(request.form.get("progress_payment_id")),
            date=request.form.get("date") or date.today().isoformat(),
            amount=amount,
            payment_method=payment_method if payment_method in ("نقدي", "بنكي") else "نقدي",
            treasury_account_id=treasury_account_id,
            reference=(request.form.get("reference") or "").strip() or None,
            notes=request.form.get("notes"),
        )
        try:
            assert_period_open(advance.date)
        except PeriodClosedError as exc:
            flash(str(exc), "danger")
            return redirect(url_for("progress_payments"))
        db.session.add(advance)
        db.session.commit()
        try:
            sync_subcontractor_payment_journal(advance)
        except PeriodClosedError as exc:
            flash(str(exc), "danger")
            return redirect(url_for("progress_payments"))
        flash(f"تم تسجيل دفعة تحت الحساب بقيمة {format_grouped_number(amount)}", "success")

        if redirect_target == "subcontractor_statement":
            return redirect(url_for("subcontractor_statement", subcontractor_id=subcontractor_id))
        return redirect(url_for("progress_payments"))


    @app.route("/subcontractors/<int:subcontractor_id>/statement")
    def subcontractor_statement(subcontractor_id):
        sync_journal_related_accounts()
        sync_posted_journals_to_documents()
        subcontractor = Subcontractor.query.get_or_404(subcontractor_id)
        statement = build_subcontractor_statement(subcontractor)
        party_account = get_account_by_code(f"SUB-{subcontractor.id:04d}")
        accounts = ChartOfAccount.query.order_by(ChartOfAccount.category, ChartOfAccount.code).all()
        treasury_accounts = [account for account in accounts if is_operational_treasury_account(account)]
        projects = Project.query.order_by(Project.code).all()
        account_balance = 0.0
        if party_account:
            account_balance = build_account_balances([party_account]).get(party_account.id, 0.0)

        return render_template(
            "subcontractor_statement.html",
            subcontractor=subcontractor,
            statement=statement,
            party_account=party_account,
            account_balance=account_balance,
            treasury_accounts=treasury_accounts,
            projects=projects,
            today_date=date.today().isoformat(),
        )


    @app.route("/subcontractors", methods=["GET", "POST"])
    def subcontractors():
        sync_journal_related_accounts()
        if request.method == "POST":
            name = (request.form.get("name") or "").strip()
            if not name:
                flash("يرجى إدخال اسم المقاول", "danger")
                return redirect(url_for("subcontractors"))
            entity_kind = request.form.get("entity_kind") or ENTITY_KIND_OPTIONS[0]
            subcontractor = Subcontractor(
                name=name,
                code=(request.form.get("code") or "").strip() or None,
                entity_kind=entity_kind if entity_kind in ENTITY_KIND_OPTIONS else ENTITY_KIND_OPTIONS[0],
                contact_info=(request.form.get("contact_info") or "").strip() or None,
                contract_value=as_float(request.form.get("contract_value")),
                discount_percentage=as_float(request.form.get("discount_percentage")),
                retention_percentage=as_float(request.form.get("retention_percentage")),
                tax_percentage=as_float(request.form.get("tax_percentage")),
                notes=request.form.get("notes"),
            )
            db.session.add(subcontractor)
            db.session.commit()
            if not subcontractor.code:
                subcontractor.code = f"SUB-{subcontractor.id:04d}"
                db.session.commit()
            sync_journal_related_accounts()
            flash("تم إضافة جهة التعامل بنجاح مع إنشاء حسابها المحاسبي تلقائيًا", "success")
            return redirect(url_for("subcontractors"))

        items = Subcontractor.query.order_by(Subcontractor.name).all()
        statement_summaries = {}
        for item in items:
            statement = build_subcontractor_statement(item)
            statement_summaries[item.id] = {
                "total_works": statement["total_works"],
                "total_paid": statement["total_paid"],
                "total_deductions": statement["total_deductions"],
                "net_due": statement["net_due"],
            }
        return render_template(
            "subcontractors.html",
            items=items,
            entity_kind_options=ENTITY_KIND_OPTIONS,
            statement_summaries=statement_summaries,
        )


    @app.route("/subcontractors/<int:subcontractor_id>/update", methods=["POST"])
    def update_subcontractor(subcontractor_id):
        item = Subcontractor.query.get_or_404(subcontractor_id)
        item.name = (request.form.get("name") or "").strip() or item.name
        item.code = (request.form.get("code") or "").strip() or item.code
        entity_kind = request.form.get("entity_kind") or item.entity_kind
        item.entity_kind = entity_kind if entity_kind in ENTITY_KIND_OPTIONS else item.entity_kind
        item.contact_info = (request.form.get("contact_info") or "").strip() or None
        item.contract_value = as_float(request.form.get("contract_value"))
        item.discount_percentage = as_float(request.form.get("discount_percentage"))
        item.retention_percentage = as_float(request.form.get("retention_percentage"))
        item.tax_percentage = as_float(request.form.get("tax_percentage"))
        item.notes = request.form.get("notes")
        db.session.commit()
        flash("تم تحديث جهة التعامل بنجاح", "success")
        return redirect(url_for("subcontractors"))


    @app.route("/suppliers", methods=["GET", "POST"])
    def suppliers():
        # تأكد من توفر جميع الحسابات قبل البدء
        sync_journal_related_accounts()

        if request.method == "POST":
            # Check if batch mode
            is_batch = request.form.get("batch_mode") == "true"

            if is_batch:
                # Process batch entries
                names = request.form.getlist("batch_name")
                contacts = request.form.getlist("batch_contact_info")
                notes_list = request.form.getlist("batch_notes")
                kinds = request.form.getlist("batch_entity_kind")

                success_count = 0
                error_count = 0
                created_suppliers = []  # لتتبع الموردين المنشأين

                for i in range(len(names)):
                    name = (names[i] if i < len(names) else "").strip()
                    contact_info = (contacts[i] if i < len(contacts) else "").strip()
                    notes = (notes_list[i] if i < len(notes_list) else "").strip()

                    # Skip empty rows
                    if not name:
                        continue

                    entity_kind = (kinds[i] if i < len(kinds) else "").strip() or ENTITY_KIND_OPTIONS[1]
                    try:
                        supplier = Supplier(
                            name=name,
                            entity_kind=entity_kind if entity_kind in ENTITY_KIND_OPTIONS else ENTITY_KIND_OPTIONS[1],
                            contact_info=contact_info or None,
                            notes=notes or None,
                        )
                        db.session.add(supplier)
                        created_suppliers.append(supplier)
                        success_count += 1
                    except Exception as e:
                        error_count += 1
                        print(f"Error adding supplier: {e}")

                # Commit الموردين الجدد
                db.session.commit()

                # مزامنة الحسابات المحاسبية التلقائية للموردين الجدد
                # (سيتم إنشاء حسابات SUP-{id} تلقائياً)
                sync_journal_related_accounts()

                if success_count > 0:
                    flash(f"تم إضافة {success_count} مورد بنجاح + حسابات محاسبية تلقائية", "success")
                if error_count > 0:
                    flash(f"حدث خطأ مع {error_count} صف", "warning")

                return redirect(url_for("suppliers"))
            else:
                # Single entry mode
                entity_kind = request.form.get("entity_kind") or ENTITY_KIND_OPTIONS[1]
                supplier = Supplier(
                    name=request.form.get("name"),
                    code=(request.form.get("code") or "").strip() or None,
                    entity_kind=entity_kind if entity_kind in ENTITY_KIND_OPTIONS else ENTITY_KIND_OPTIONS[1],
                    contact_info=request.form.get("contact_info"),
                    notes=request.form.get("notes"),
                )
                db.session.add(supplier)
                db.session.commit()
                if not supplier.code:
                    supplier.code = f"SUP-{supplier.id:04d}"
                    db.session.commit()
                # مزامنة الحسابات بعد إضافة المورد
                sync_journal_related_accounts()
                flash("تم إضافة مورد بنجاح + حساب محاسبي تلقائي", "success")
                return redirect(url_for("suppliers"))

        items = Supplier.query.order_by(Supplier.name).all()
        return render_template("suppliers.html", items=items, entity_kind_options=ENTITY_KIND_OPTIONS)


    @app.route("/suppliers/<int:supplier_id>/statement")
    def supplier_statement(supplier_id):
        sync_journal_related_accounts()
        sync_posted_journals_to_documents()
        supplier = Supplier.query.get_or_404(supplier_id)
        supplier_account = ChartOfAccount.query.filter_by(code=f"SUP-{supplier.id:04d}").first()

        supplier_orders = PurchaseOrder.query.filter_by(supplier_id=supplier.id).order_by(PurchaseOrder.date.desc(), PurchaseOrder.id.desc()).all()
        total_orders_value = sum(as_float(item.total_value) for item in supplier_orders)
        total_due_value = total_orders_value
        total_paid_value = 0
        supplier_balance = total_due_value - total_paid_value

        # كشف الحساب يعتمد على حركات حساب المورد في اليومية (مبدأ القيد المزدوج):
        # الدائن = التزام على الشركة (توريدات وخدمات)، المدين = سداد أو مرتجع.
        journal_movements = []
        opening_balance = as_float(getattr(supplier_account, "opening_balance", 0)) if supplier_account else 0.0
        if supplier_account:
            linked_entries = JournalEntry.query.filter(
                JournalEntry.status == "مرحل"
            ).filter(
                (JournalEntry.debit_account_id == supplier_account.id) |
                (JournalEntry.credit_account_id == supplier_account.id)
            ).order_by(JournalEntry.date.asc(), JournalEntry.id.asc()).all()

            total_journal_due = 0.0
            total_journal_paid = 0.0

            for entry in linked_entries:
                debit = as_float(entry.amount) if entry.debit_account_id == supplier_account.id else 0.0
                credit = as_float(entry.amount) if entry.credit_account_id == supplier_account.id else 0.0
                total_journal_due += credit
                total_journal_paid += debit
                journal_movements.append(
                    {
                        "date": entry.date,
                        "source": "قيد يومية",
                        "reference": entry.reference or entry.display_number,
                        "description": entry.description,
                        "project": entry.project.display_name if entry.project else "-",
                        "debit": debit,
                        "credit": credit,
                    }
                )

            total_due_value = round(total_journal_due + max(opening_balance * -1, 0.0), 2)
            total_paid_value = round(total_journal_paid, 2)
            supplier_balance = round(total_due_value - total_paid_value, 2)

        movements = sorted(
            journal_movements,
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
        item.code = (request.form.get("code") or "").strip() or item.code
        entity_kind = request.form.get("entity_kind") or item.entity_kind
        item.entity_kind = entity_kind if entity_kind in ENTITY_KIND_OPTIONS else item.entity_kind
        item.contact_info = request.form.get("contact_info")
        item.notes = request.form.get("notes")
        db.session.commit()
        flash("تم تحديث المورد بنجاح", "success")
        return redirect(url_for("suppliers"))
