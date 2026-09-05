from calendar import monthrange
from collections import defaultdict
from datetime import date

from flask import flash, redirect, render_template, request, url_for
from sqlalchemy.exc import IntegrityError

from models import (
    ChartOfAccount, Employee, EmployeeAttendance, EmployeeSalaryPayment, PayrollSlip, db,
)
from services.accounting import (
    PeriodClosedError, as_float, as_int, assert_period_open, build_account_balances,
    get_or_create_employee_account, is_treasury_account,
    sync_employee_salary_payment_journal, sync_journal_related_accounts,
    sync_payroll_slip_journal,
)

EMPLOYEE_TAG_OPTIONS = ["موظف", "محاسب", "مهندس", "إداري", "سائق", "مورد", "مقاول", "فني"]
ATTENDANCE_STATUSES = ["حضور", "انصراف", "تأخير", "استئذان", "إجازة", "غياب"]


def _current_month():
    return date.today().strftime("%Y-%m")


def _normalize_month(value):
    raw = (value or "").strip()
    if len(raw) >= 7 and raw[4] == "-":
        return raw[:7]
    return _current_month()


def _month_last_day(period_month):
    year, month = [int(part) for part in period_month.split("-")]
    return f"{period_month}-{monthrange(year, month)[1]:02d}"


def _attendance_stats(period_month):
    stats = defaultdict(lambda: {"work_days": 0.0, "vacation_days": 0.0})
    rows = EmployeeAttendance.query.filter(EmployeeAttendance.date.like(f"{period_month}%")).all()
    for row in rows:
        if row.status in {"حضور", "انصراف", "تأخير"}:
            stats[row.employee_id]["work_days"] += 1
        elif row.status == "إجازة":
            stats[row.employee_id]["vacation_days"] += 1
        elif row.status == "استئذان":
            stats[row.employee_id]["work_days"] += 1
    return stats


def _employee_remaining(account_id, balances):
    if not account_id:
        return 0.0
    balance = as_float(balances.get(account_id, 0.0))
    return round(-balance if balance < 0 else 0.0, 2)


def register(app):
    @app.route("/employees", methods=["GET", "POST"])
    def employees():
        sync_journal_related_accounts()
        if request.method == "POST":
            name = (request.form.get("name") or "").strip()
            if not name:
                flash("يرجى إدخال اسم الموظف", "danger")
                return redirect(url_for("employees"))
            tag = (request.form.get("tag") or "").strip() or "موظف"
            employee = Employee(
                name=name,
                code=(request.form.get("code") or "").strip() or None,
                tag=tag,
                basic_salary=as_float(request.form.get("basic_salary")),
                contact_info=(request.form.get("contact_info") or "").strip() or None,
                notes=request.form.get("notes"),
                is_active=True,
            )
            db.session.add(employee)
            db.session.commit()
            if not employee.code:
                employee.code = f"EMP-{employee.id:04d}"
                db.session.commit()
            sync_journal_related_accounts()
            flash(f"تم إضافة {employee.display_name} وحسابه في دليل الحسابات", "success")
            return redirect(url_for("employees"))

        items = Employee.query.order_by(Employee.name).all()
        accounts = ChartOfAccount.query.order_by(ChartOfAccount.code).all()
        balances = build_account_balances(accounts)
        account_ids = {}
        remaining = {}
        for item in items:
            account = get_or_create_employee_account(item)
            account_ids[item.id] = account.id if account else None
            remaining[item.id] = _employee_remaining(account_ids[item.id], balances)
        return render_template(
            "employees.html",
            items=items,
            tag_options=EMPLOYEE_TAG_OPTIONS,
            account_ids=account_ids,
            remaining=remaining,
        )

    @app.route("/employees/<int:employee_id>/update", methods=["POST"])
    def update_employee(employee_id):
        item = Employee.query.get_or_404(employee_id)
        name = (request.form.get("name") or "").strip()
        if not name:
            flash("يرجى إدخال اسم الموظف", "danger")
            return redirect(url_for("employees"))
        item.name = name
        item.code = (request.form.get("code") or "").strip() or item.code
        item.tag = (request.form.get("tag") or "").strip() or item.tag
        item.basic_salary = as_float(request.form.get("basic_salary"))
        item.contact_info = (request.form.get("contact_info") or "").strip() or None
        item.notes = request.form.get("notes")
        item.is_active = request.form.get("is_active") == "on"
        get_or_create_employee_account(item)
        db.session.commit()
        flash("تم تحديث بيانات الموظف واسم حسابه", "success")
        return redirect(url_for("employees"))

    @app.route("/hr", methods=["GET", "POST"])
    def hr_module():
        sync_journal_related_accounts()
        period_month = _normalize_month(request.values.get("period_month"))
        posting_date = request.form.get("posting_date") or _month_last_day(period_month)
        employees = Employee.query.filter_by(is_active=True).order_by(Employee.name).all()
        if not employees:
            employees = Employee.query.order_by(Employee.name).all()
        accounts = ChartOfAccount.query.order_by(ChartOfAccount.code).all()
        treasury_accounts = [account for account in accounts if is_treasury_account(account)]
        balances = build_account_balances(accounts)

        if request.method == "POST":
            action = (request.form.get("action") or "").strip()
            try:
                if action == "attendance":
                    return _save_attendance()
                if action in {"save_payroll", "post_payroll"}:
                    return _save_payroll(period_month, posting_date, post=action == "post_payroll")
                if action == "pay_salary":
                    return _save_salary_payment()
            except PeriodClosedError as exc:
                flash(str(exc), "danger")
                return redirect(url_for("hr_module", period_month=period_month))
            flash("إجراء غير معروف", "danger")
            return redirect(url_for("hr_module", period_month=period_month))

        attendance_rows = (
            EmployeeAttendance.query.filter(EmployeeAttendance.date.like(f"{period_month}%"))
            .order_by(EmployeeAttendance.date.desc(), EmployeeAttendance.id.desc())
            .all()
        )
        slips = {slip.employee_id: slip for slip in PayrollSlip.query.filter_by(period_month=period_month).all()}
        stats = _attendance_stats(period_month)
        payroll_rows = []
        totals = {
            "basic_salary": 0.0,
            "overtime": 0.0,
            "incentives": 0.0,
            "delay_deduction": 0.0,
            "permission_deduction": 0.0,
            "other_deductions": 0.0,
            "advances": 0.0,
            "additions": 0.0,
            "deductions": 0.0,
            "net": 0.0,
            "remaining": 0.0,
        }
        for employee in employees:
            slip = slips.get(employee.id)
            account = get_or_create_employee_account(employee)
            remaining = _employee_remaining(account.id if account else None, balances)
            row = {
                "employee": employee,
                "slip": slip,
                "account_id": account.id if account else None,
                "remaining": remaining,
                "work_days": slip.work_days if slip else stats[employee.id]["work_days"],
                "vacation_days": slip.vacation_days if slip else stats[employee.id]["vacation_days"],
                "basic_salary": slip.basic_salary if slip else employee.basic_salary,
                "overtime": slip.overtime if slip else 0,
                "incentives": slip.incentives if slip else 0,
                "delay_deduction": slip.delay_deduction if slip else 0,
                "permission_deduction": slip.permission_deduction if slip else 0,
                "other_deductions": slip.other_deductions if slip else 0,
                "advances": slip.advances if slip else 0,
                "notes": slip.notes if slip else "",
                "status": slip.status if slip else "مسودة",
                "net": slip.net_salary if slip else round(as_float(employee.basic_salary), 2),
            }
            payroll_rows.append(row)
            for key in (
                "basic_salary", "overtime", "incentives", "delay_deduction",
                "permission_deduction", "other_deductions", "advances",
            ):
                totals[key] += as_float(row[key])
            additions = as_float(row["basic_salary"]) + as_float(row["overtime"]) + as_float(row["incentives"])
            deductions = (
                as_float(row["delay_deduction"])
                + as_float(row["permission_deduction"])
                + as_float(row["other_deductions"])
                + as_float(row["advances"])
            )
            totals["additions"] += additions
            totals["deductions"] += deductions
            totals["net"] += additions - deductions
            totals["remaining"] += remaining

        salary_expense = next((account for account in accounts if account.code == "EXP-SAL"), None)
        salary_expense_balance = as_float(balances.get(salary_expense.id, 0.0)) if salary_expense else 0.0
        payments = (
            EmployeeSalaryPayment.query.filter(EmployeeSalaryPayment.date.like(f"{period_month}%"))
            .order_by(EmployeeSalaryPayment.id.desc())
            .all()
        )
        return render_template(
            "hr.html",
            period_month=period_month,
            posting_date=_month_last_day(period_month),
            today_date=date.today().isoformat(),
            employees=employees,
            attendance_rows=attendance_rows,
            attendance_statuses=ATTENDANCE_STATUSES,
            payroll_rows=payroll_rows,
            totals={key: round(value, 2) for key, value in totals.items()},
            treasury_accounts=treasury_accounts,
            payments=payments,
            salary_expense=salary_expense,
            salary_expense_balance=round(salary_expense_balance, 2),
            tag_options=EMPLOYEE_TAG_OPTIONS,
        )

    @app.route("/hr/attendance/<int:row_id>/update", methods=["POST"])
    def update_attendance(row_id):
        row = EmployeeAttendance.query.get_or_404(row_id)
        row.date = request.form.get("date") or row.date
        row.status = request.form.get("status") or row.status
        if row.status not in ATTENDANCE_STATUSES:
            row.status = "حضور"
        row.check_in = (request.form.get("check_in") or "").strip() or None
        row.check_out = (request.form.get("check_out") or "").strip() or None
        row.delay_minutes = as_float(request.form.get("delay_minutes"))
        row.permission_hours = as_float(request.form.get("permission_hours"))
        row.notes = request.form.get("notes")
        db.session.commit()
        flash("تم تعديل سجل الحضور", "success")
        return redirect(url_for("hr_module", period_month=_normalize_month(row.date)))

    @app.route("/hr/attendance/<int:row_id>/delete", methods=["POST"])
    def delete_attendance(row_id):
        row = EmployeeAttendance.query.get_or_404(row_id)
        period_month = _normalize_month(row.date)
        db.session.delete(row)
        db.session.commit()
        flash("تم حذف سجل الحضور", "success")
        return redirect(url_for("hr_module", period_month=period_month))

    @app.route("/hr/payroll/<int:slip_id>/update", methods=["POST"])
    def update_payroll_slip(slip_id):
        slip = PayrollSlip.query.get_or_404(slip_id)
        posting_date = request.form.get("date") or slip.date or date.today().isoformat()
        try:
            assert_period_open(slip.date)
            assert_period_open(posting_date)
        except PeriodClosedError as exc:
            flash(str(exc), "danger")
            return redirect(url_for("hr_module", period_month=slip.period_month))
        _apply_payroll_fields(slip, request.form)
        slip.date = posting_date
        slip.status = "مرحل" if request.form.get("post") == "1" else (request.form.get("status") or slip.status or "مسودة")
        db.session.flush()
        sync_payroll_slip_journal(slip)
        flash("تم تحديث كشف المرتب والقيد على مصروف المرتبات وحساب الموظف", "success")
        return redirect(url_for("hr_module", period_month=slip.period_month))

    @app.route("/hr/salary-payments/<int:payment_id>/update", methods=["POST"])
    def update_employee_salary_payment(payment_id):
        payment = EmployeeSalaryPayment.query.get_or_404(payment_id)
        amount = as_float(request.form.get("amount"))
        if amount <= 0:
            flash("يرجى إدخال مبلغ أكبر من صفر", "danger")
            return redirect(url_for("hr_module"))
        method = request.form.get("payment_method") or payment.payment_method or "نقدي"
        if method not in ("نقدي", "بنكي"):
            method = "نقدي"
        pay_date = request.form.get("date") or payment.date or date.today().isoformat()
        try:
            assert_period_open(payment.date)
            assert_period_open(pay_date)
        except PeriodClosedError as exc:
            flash(str(exc), "danger")
            return redirect(url_for("hr_module", period_month=_normalize_month(pay_date)))
        payment.date = pay_date
        employee_id = as_int(request.form.get("employee_id"))
        if employee_id:
            payment.employee_id = employee_id
        payment.amount = amount
        payment.payment_method = method
        payment.treasury_account_id = as_int(request.form.get("treasury_account_id")) or payment.treasury_account_id
        payment.reference = (request.form.get("reference") or "").strip() or None
        if request.form.get("notes") is not None and "JRN-SRC:" not in (payment.notes or ""):
            payment.notes = request.form.get("notes")
        db.session.flush()
        sync_employee_salary_payment_journal(payment)
        flash("تم تعديل صرف المرتب على الخزنة وحساب الموظف معًا", "success")
        return redirect(url_for("hr_module", period_month=_normalize_month(pay_date)))


def _apply_payroll_fields(slip, form, prefix=""):
    slip.work_days = as_float(form.get(f"{prefix}work_days"))
    slip.vacation_days = as_float(form.get(f"{prefix}vacation_days"))
    slip.basic_salary = as_float(form.get(f"{prefix}basic_salary"))
    slip.overtime = as_float(form.get(f"{prefix}overtime"))
    slip.incentives = as_float(form.get(f"{prefix}incentives"))
    slip.delay_deduction = as_float(form.get(f"{prefix}delay_deduction"))
    slip.permission_deduction = as_float(form.get(f"{prefix}permission_deduction"))
    slip.other_deductions = as_float(form.get(f"{prefix}other_deductions"))
    slip.advances = as_float(form.get(f"{prefix}advances"))
    if form.get(f"{prefix}notes") is not None:
        slip.notes = form.get(f"{prefix}notes")


def _save_attendance():
    employee_id = as_int(request.form.get("employee_id"))
    row_date = request.form.get("date") or date.today().isoformat()
    if not employee_id:
        flash("يرجى اختيار الموظف", "danger")
        return redirect(url_for("hr_module", period_month=_normalize_month(row_date)))
    status = request.form.get("status") or "حضور"
    if status not in ATTENDANCE_STATUSES:
        status = "حضور"
    existing = EmployeeAttendance.query.filter_by(employee_id=employee_id, date=row_date).first()
    row = existing or EmployeeAttendance(employee_id=employee_id, date=row_date)
    row.status = status
    row.check_in = (request.form.get("check_in") or "").strip() or None
    row.check_out = (request.form.get("check_out") or "").strip() or None
    row.delay_minutes = as_float(request.form.get("delay_minutes"))
    row.permission_hours = as_float(request.form.get("permission_hours"))
    row.notes = request.form.get("notes")
    if existing is None:
        db.session.add(row)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash("يوجد سجل حضور لنفس الموظف في هذا اليوم", "danger")
        return redirect(url_for("hr_module", period_month=_normalize_month(row_date)))
    flash("تم حفظ سجل الحضور والانصراف", "success")
    return redirect(url_for("hr_module", period_month=_normalize_month(row_date)))


def _save_payroll(period_month, posting_date, post=False):
    if post:
        assert_period_open(posting_date)
    employee_ids = request.form.getlist("employee_id")
    saved = 0
    posted = 0
    for index, raw_id in enumerate(employee_ids):
        employee_id = as_int(raw_id)
        if not employee_id:
            continue
        employee = Employee.query.get(employee_id)
        if employee is None:
            continue
        slip = PayrollSlip.query.filter_by(employee_id=employee_id, period_month=period_month).first()
        if slip is None:
            slip = PayrollSlip(employee_id=employee_id, period_month=period_month)
            db.session.add(slip)
            db.session.flush()

        def field(name):
            values = request.form.getlist(name)
            return values[index] if index < len(values) else ""

        slip.date = posting_date
        slip.work_days = as_float(field("work_days"))
        slip.vacation_days = as_float(field("vacation_days"))
        slip.basic_salary = as_float(field("basic_salary"))
        slip.overtime = as_float(field("overtime"))
        slip.incentives = as_float(field("incentives"))
        slip.delay_deduction = as_float(field("delay_deduction"))
        slip.permission_deduction = as_float(field("permission_deduction"))
        slip.other_deductions = as_float(field("other_deductions"))
        slip.advances = as_float(field("advances"))
        notes_values = request.form.getlist("payroll_notes")
        slip.notes = notes_values[index] if index < len(notes_values) else slip.notes
        slip.status = "مرحل" if post else "مسودة"
        db.session.flush()
        sync_payroll_slip_journal(slip)
        saved += 1
        if post and slip.net_salary > 0:
            posted += 1
    db.session.commit()
    if post:
        flash(
            f"تم ترحيل {posted} مرتب إلى مصروف المرتبات (Basic Salary). "
            "حساب الموظف أصبح دائنًا بصافي المرتب، والصرف من الخزنة يتم لاحقًا دون المساس بالمصروف.",
            "success",
        )
    else:
        flash(f"تم حفظ {saved} كشف مرتب كمسودة دون ترحيل قيود", "success")
    return redirect(url_for("hr_module", period_month=period_month))


def _save_salary_payment():
    employee_id = as_int(request.form.get("employee_id"))
    amount = as_float(request.form.get("amount"))
    pay_date = request.form.get("date") or date.today().isoformat()
    if not employee_id:
        flash("يرجى اختيار الموظف", "danger")
        return redirect(url_for("hr_module", period_month=_normalize_month(pay_date)))
    if amount <= 0:
        flash("يرجى إدخال مبلغ أكبر من صفر", "danger")
        return redirect(url_for("hr_module", period_month=_normalize_month(pay_date)))
    assert_period_open(pay_date)
    method = request.form.get("payment_method") or "نقدي"
    if method not in ("نقدي", "بنكي"):
        method = "نقدي"
    payment = EmployeeSalaryPayment(
        employee_id=employee_id,
        date=pay_date,
        amount=amount,
        payment_method=method,
        treasury_account_id=as_int(request.form.get("treasury_account_id")),
        reference=(request.form.get("reference") or "").strip() or None,
        notes=request.form.get("notes"),
    )
    db.session.add(payment)
    db.session.commit()
    sync_employee_salary_payment_journal(payment)
    flash("تم صرف المرتب من الخزنة على حساب الموظف دون المساس بمصروف المرتبات", "success")
    return redirect(url_for("hr_module", period_month=_normalize_month(pay_date)))
