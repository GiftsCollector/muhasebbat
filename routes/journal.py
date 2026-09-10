from datetime import date

from flask import flash, redirect, render_template, request, url_for

from models import ChartOfAccount, JournalEntry, Project, db
from services.accounting import (
    JOURNAL_OPTIONS, PeriodClosedError, as_float, as_int, assert_period_open,
    auto_journal_source, build_account_balances, format_grouped_number, get_treasury_balance,
    is_auto_journal_entry, normalize_journal_status, purge_operating_document,
    sync_journal_related_accounts, clear_journal_sourced_docs, sync_operational_docs_from_journal,
    sync_posted_journals_to_documents,
)
from services.immutability import POSTED_LOCK_MESSAGE
from services.authz import user_can


def register(app):


    @app.route("/journal", methods=["GET", "POST"])
    def journal():
        sync_journal_related_accounts()
        if request.method == "POST":
            entry_action = request.form.get("entry_action") or "draft"
            entry_date = request.form.get("date") or date.today().isoformat()
            if entry_action == "post" and not user_can("journal.post"):
                flash("إدخال البيانات يحفظ القيود كمسودة فقط. الترحيل يحتاج صلاحية محاسب.", "warning")
                entry_action = "draft"
            try:
                assert_period_open(entry_date)
            except PeriodClosedError as exc:
                flash(str(exc), "danger")
                return redirect(url_for("journal"))
            status = "مرحل" if entry_action == "post" else "مسودة"
            # كل بند في الجدول قيد مستقل له رقمه ومرجعه وبيانه، والتاريخ مشترك للدفعة كلها
            line_numbers = request.form.getlist("line_entry_number")
            line_references = request.form.getlist("line_reference")
            line_descriptions = request.form.getlist("line_description")
            line_debit_ids = request.form.getlist("line_debit_account_id")
            line_credit_ids = request.form.getlist("line_credit_account_id")
            line_amounts = request.form.getlist("line_amount")

            valid_account_ids = {
                account_id for (account_id,) in db.session.query(ChartOfAccount.id).all()
            }
            used_entry_numbers = {
                (value or "").strip()
                for (value,) in db.session.query(JournalEntry.entry_number).all()
                if (value or "").strip()
            }

            parsed_lines = []
            max_len = max(
                len(line_numbers),
                len(line_references),
                len(line_descriptions),
                len(line_debit_ids),
                len(line_credit_ids),
                len(line_amounts),
            ) if any([line_debit_ids, line_credit_ids, line_amounts]) else 0

            for idx in range(max_len):
                entry_number = (line_numbers[idx] if idx < len(line_numbers) else "").strip()
                reference = (line_references[idx] if idx < len(line_references) else "").strip()
                description = (line_descriptions[idx] if idx < len(line_descriptions) else "").strip()
                debit_id = as_int(line_debit_ids[idx] if idx < len(line_debit_ids) else None) or 0
                credit_id = as_int(line_credit_ids[idx] if idx < len(line_credit_ids) else None) or 0
                amount = as_float(line_amounts[idx] if idx < len(line_amounts) else 0)

                is_empty_row = (
                    debit_id <= 0 and credit_id <= 0 and amount <= 0
                    and not description and not reference and not entry_number
                )
                if is_empty_row:
                    continue
                if debit_id <= 0 or credit_id <= 0:
                    flash(f"يرجى اختيار الحساب المدين والدائن في البند رقم {idx + 1}", "danger")
                    return redirect(url_for("journal"))
                if debit_id == credit_id:
                    flash(f"لا يمكن تكرار نفس الحساب مدينًا ودائنًا في البند رقم {idx + 1}", "danger")
                    return redirect(url_for("journal"))
                if debit_id not in valid_account_ids or credit_id not in valid_account_ids:
                    flash(f"حساب غير موجود في دليل الحسابات في البند رقم {idx + 1}", "danger")
                    return redirect(url_for("journal"))
                if amount <= 0:
                    flash(f"يرجى إدخال مبلغ أكبر من صفر في البند رقم {idx + 1}", "danger")
                    return redirect(url_for("journal"))
                if not description:
                    flash(f"يرجى إدخال بيان القيد في البند رقم {idx + 1}", "danger")
                    return redirect(url_for("journal"))
                if entry_number and entry_number in used_entry_numbers:
                    flash(
                        f"رقم القيد {entry_number} في البند رقم {idx + 1} مستخدم بالفعل، اختر رقمًا مختلفًا",
                        "danger",
                    )
                    return redirect(url_for("journal"))
                if entry_number:
                    used_entry_numbers.add(entry_number)

                parsed_lines.append({
                    "entry_number": entry_number or None,
                    "reference": reference or None,
                    "description": description,
                    "debit_account_id": debit_id,
                    "credit_account_id": credit_id,
                    "amount": amount,
                })

            if not parsed_lines:
                flash("يرجى إضافة بند قيد واحد على الأقل", "danger")
                return redirect(url_for("journal"))

            created_entries = []
            for line in parsed_lines:
                entry = JournalEntry(
                    date=entry_date,
                    entry_number=line["entry_number"],
                    reference=line["reference"],
                    journal_name="يومية عامة",
                    branch=None,
                    stock_move=None,
                    status=status,
                    description=line["description"],
                    debit_account_id=line["debit_account_id"],
                    credit_account_id=line["credit_account_id"],
                    amount=line["amount"],
                    project_id=None,
                    cost_center=None,
                )
                db.session.add(entry)
                created_entries.append(entry)

            db.session.commit()
            for entry in created_entries:
                sync_operational_docs_from_journal(entry)
            total_amount = round(sum(line["amount"] for line in parsed_lines), 2)
            numbers_label = "، ".join(entry.display_number for entry in created_entries[:6])
            if len(created_entries) > 6:
                numbers_label += " ..."
            if status == "مرحل":
                flash(
                    f"تم ترحيل {len(parsed_lines)} قيد بإجمالي {format_grouped_number(total_amount)}"
                    f" ({numbers_label}) وتحديث أرصدة الحسابات والتقارير تلقائيًا",
                    "success",
                )
            else:
                flash(
                    f"تم حفظ {len(parsed_lines)} قيد كمسودة بإجمالي {format_grouped_number(total_amount)}"
                    f" ({numbers_label}) — المسودات لا تؤثر على الأرصدة حتى ترحيلها",
                    "success",
                )
            return redirect(url_for("journal"))

        status_filter = request.args.get("status") or ""
        search_term = (request.args.get("search") or "").strip()
        from_date = (request.args.get("from_date") or "").strip()
        to_date = (request.args.get("to_date") or "").strip()
        sync_posted_journals_to_documents()

        query = JournalEntry.query
        if status_filter in ("مسودة", "مرحل"):
            query = query.filter_by(status=status_filter)
        if from_date:
            query = query.filter(JournalEntry.date >= from_date)
        if to_date:
            query = query.filter(JournalEntry.date <= to_date)
        entries = query.order_by(JournalEntry.date.desc(), JournalEntry.id.desc()).all()

        if search_term:
            needle = search_term.lower()

            def entry_matches(item):
                haystack = " ".join([
                    item.display_number,
                    item.reference or "",
                    item.description or "",
                    item.debit_account.name if item.debit_account else "",
                    item.credit_account.name if item.credit_account else "",
                ]).lower()
                return needle in haystack

            entries = [item for item in entries if entry_matches(item)]

        accounts = ChartOfAccount.query.order_by(ChartOfAccount.code, ChartOfAccount.name).all()
        account_balances = build_account_balances(accounts)
        treasury_balance = get_treasury_balance(accounts, account_balances)

        posted_total = round(sum(as_float(item.amount) for item in entries if item.status == "مرحل"), 2)
        draft_total = round(sum(as_float(item.amount) for item in entries if item.status != "مرحل"), 2)
        journal_totals = {
            "count": len(entries),
            "posted_count": sum(1 for item in entries if item.status == "مرحل"),
            "draft_count": sum(1 for item in entries if item.status != "مرحل"),
            "posted_total": posted_total,
            "draft_total": draft_total,
            "all_total": round(posted_total + draft_total, 2),
        }

        latest_entry = JournalEntry.query.order_by(JournalEntry.id.desc()).first()
        next_journal_serial = (latest_entry.id + 1) if latest_entry else 1
        return render_template(
            "journal.html",
            entries=entries,
            accounts=accounts,
            account_balances=account_balances,
            treasury_balance=treasury_balance,
            journal_options=JOURNAL_OPTIONS,
            status_filter=status_filter,
            search_term=search_term,
            from_date=from_date,
            to_date=to_date,
            journal_totals=journal_totals,
            today_date=date.today().isoformat(),
            next_journal_serial=next_journal_serial,
        )

    def journal_redirect_target():
        """يرجع لصفحة القيود مع الحفاظ على الفلاتر المستخدمة."""
        filters = {
            key: (request.form.get(key) or "").strip()
            for key in ("status", "search", "from_date", "to_date")
        }
        return url_for("journal", **{key: value for key, value in filters.items() if value})


    @app.route("/journal/<int:entry_id>")
    def journal_entry_detail(entry_id):
        sync_journal_related_accounts()
        entry = JournalEntry.query.get_or_404(entry_id)
        accounts = ChartOfAccount.query.order_by(ChartOfAccount.code, ChartOfAccount.name).all()
        account_balances = build_account_balances(accounts)
        projects = Project.query.order_by(Project.code).all()
        return render_template(
            "journal_entry_detail.html",
            entry=entry,
            accounts=accounts,
            account_balances=account_balances,
            projects=projects,
            journal_options=JOURNAL_OPTIONS,
            is_auto_entry=is_auto_journal_entry(entry),
        )


    @app.route("/journal/<int:entry_id>/update", methods=["POST"])
    def update_journal_entry(entry_id):
        entry = JournalEntry.query.get_or_404(entry_id)
        if is_auto_journal_entry(entry):
            flash(
                "هذا قيد تلقائي مرتبط بمستند. عدّل أو احذف المستند الأصلي (السداد/التحصيل) ليسمع في الحسابين معًا.",
                "danger",
            )
            return redirect(url_for("journal_entry_detail", entry_id=entry.id))

        debit_account_id = as_int(request.form.get("debit_account_id")) or 0
        credit_account_id = as_int(request.form.get("credit_account_id")) or 0
        amount = as_float(request.form.get("amount"))
        entry_date = request.form.get("date") or entry.date
        entry_number = (request.form.get("entry_number") or "").strip() or None
        description = (request.form.get("description") or "").strip()
        project_id = as_int(request.form.get("project_id")) or None

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
        if not ChartOfAccount.query.get(debit_account_id) or not ChartOfAccount.query.get(credit_account_id):
            flash("أحد الحسابات المختارة غير موجود في دليل الحسابات", "danger")
            return redirect(url_for("journal_entry_detail", entry_id=entry.id))
        if entry_number:
            duplicate = JournalEntry.query.filter(
                JournalEntry.entry_number == entry_number,
                JournalEntry.id != entry.id,
            ).first()
            if duplicate:
                flash(f"رقم القيد {entry_number} مستخدم بالفعل في قيد آخر", "danger")
                return redirect(url_for("journal_entry_detail", entry_id=entry.id))

        try:
            assert_period_open(entry.date)
            assert_period_open(entry_date)
        except PeriodClosedError as exc:
            flash(str(exc), "danger")
            return redirect(url_for("journal_entry_detail", entry_id=entry.id))
        if entry.status == "مرحل" and not user_can("journal.post"):
            flash("تعديل قيد مرحل يحتاج صلاحية محاسب", "danger")
            return redirect(url_for("journal_entry_detail", entry_id=entry.id))

        entry.entry_number = entry_number
        entry.reference = (request.form.get("reference") or "").strip() or None
        entry.journal_name = (request.form.get("journal_name") or "يومية عامة").strip()
        entry.branch = (request.form.get("branch") or "").strip() or None
        entry.stock_move = (request.form.get("stock_move") or "").strip() or None
        entry.status = normalize_journal_status(request.form.get("status"))
        entry.date = entry_date
        entry.description = description
        entry.debit_account_id = debit_account_id
        entry.credit_account_id = credit_account_id
        entry.amount = amount
        entry.project_id = project_id
        db.session.commit()
        sync_operational_docs_from_journal(entry)
        flash("تم تحديث القيد على الحساب المدين والدائن معًا", "success")
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
        sync_operational_docs_from_journal(reversed_entry)
        flash("تم إنشاء قيد عكسي بنجاح", "success")
        return redirect(url_for("journal"))


    @app.route("/journal/<int:entry_id>/delete", methods=["POST"])
    def delete_journal_entry(entry_id):
        entry = JournalEntry.query.get_or_404(entry_id)
        if is_auto_journal_entry(entry):
            entity_type, source_id = auto_journal_source(entry)
            if not entity_type or not source_id:
                flash("تعذر تحديد المستند المرتبط بهذا القيد التلقائي", "danger")
                return redirect(journal_redirect_target())
            try:
                purge_operating_document(entity_type, source_id)
            except PeriodClosedError as exc:
                flash(str(exc), "danger")
                return redirect(journal_redirect_target())
            flash("تم حذف المستند والقيود من الحساب المدين والدائن معًا", "success")
            return redirect(journal_redirect_target())
        entry_label = entry.display_number
        try:
            assert_period_open(entry.date)
        except PeriodClosedError as exc:
            flash(str(exc), "danger")
            return redirect(journal_redirect_target())
        clear_journal_sourced_docs(entry.id, commit=False)
        db.session.delete(entry)
        db.session.commit()
        flash(f"تم حذف القيد {entry_label} من الحساب المدين والدائن معًا", "success")
        return redirect(journal_redirect_target())


    @app.route("/journal/<int:entry_id>/post", methods=["POST"])
    def post_journal_entry(entry_id):
        entry = JournalEntry.query.get_or_404(entry_id)
        if entry.status == "مرحل":
            flash("القيد مرحل بالفعل", "info")
            return redirect(journal_redirect_target())
        if not user_can("journal.post"):
            flash("ليست لديك صلاحية ترحيل القيود", "danger")
            return redirect(journal_redirect_target())
        try:
            assert_period_open(entry.date)
        except PeriodClosedError as exc:
            flash(str(exc), "danger")
            return redirect(journal_redirect_target())
        entry.status = "مرحل"
        db.session.commit()
        sync_operational_docs_from_journal(entry)
        flash(
            f"تم ترحيل القيد {entry.display_number} وانعكس على أرصدة الحسابات وميزان المراجعة والتقارير",
            "success",
        )
        return redirect(journal_redirect_target())


    @app.route("/journal/<int:entry_id>/unpost", methods=["POST"])
    def unpost_journal_entry(entry_id):
        entry = JournalEntry.query.get_or_404(entry_id)
        flash(POSTED_LOCK_MESSAGE, "danger")
        return redirect(journal_redirect_target())


    @app.route("/journal/post-all", methods=["POST"])
    def post_all_journal_drafts():
        drafts = JournalEntry.query.filter(JournalEntry.status != "مرحل").all()
        if not drafts:
            flash("لا توجد قيود مسودة للترحيل", "info")
            return redirect(journal_redirect_target())

        total = 0.0
        for entry in drafts:
            try:
                assert_period_open(entry.date)
            except PeriodClosedError as exc:
                flash(str(exc), "danger")
                return redirect(journal_redirect_target())
            entry.status = "مرحل"
            total += as_float(entry.amount)
        db.session.commit()
        for entry in drafts:
            sync_operational_docs_from_journal(entry)
        flash(
            f"تم ترحيل {len(drafts)} قيد مسودة بإجمالي {format_grouped_number(round(total, 2))}",
            "success",
        )
        return redirect(journal_redirect_target())
