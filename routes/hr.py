from calendar import monthrange
from collections import defaultdict
from datetime import date, datetime, timedelta

from flask import flash, redirect, render_template, request, url_for
from sqlalchemy.exc import IntegrityError

from models import (
    ChartOfAccount, Employee, EmployeeAttendance, EmployeeSalaryPayment, PayrollSlip, db,
)
from services.authz import filter_hidden_treasuries, user_can_see_account
from services.accounting import (
    PeriodClosedError, as_float, as_int, assert_period_open, build_account_balances,
    employee_code_taken, get_account_by_code, get_or_create_employee_account,
    is_treasury_account, next_employee_code,
    sync_employee_salary_payment_journal, sync_journal_related_accounts,
    sync_payroll_slip_journal,
)

EMPLOYEE_TAG_OPTIONS = ["موظف", "محاسب", "مهندس", "إداري", "سائق", "مورد", "مقاول", "فني"]
ATTENDANCE_STATUSES = [
    "حضور",
    "غياب",
    "تأخير",
    "استئذان",
    "إجازة اعتيادية",
    "إجازة مرضية",
    "إجازة عارضة",
    "إجازة بدون أجر",
    "إجازة",
]
WORK_DAY_STATUSES = {
    "حضور", "انصراف", "تأخير", "استئذان",
    "إجازة اعتيادية", "إجازة مرضية", "إجازة عارضة", "إجازة",
}
VACATION_STATUSES = {"إجازة اعتيادية", "إجازة مرضية", "إجازة عارضة", "إجازة"}
ABSENT_STATUSES = {"غياب", "إجازة بدون أجر"}
PAYROLL_MONTH_DAYS = 30
PAYSLIP_COMPANY_NAME = "شركة السيد الشيخ للمقاولات العمومية"
PAYSLIP_FINANCE_MANAGER = "IBRAHIM ABD ALKREEM"
ARABIC_WEEKDAYS = ["الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]


def _current_month():
    return date.today().strftime("%Y-%m")


def _default_daily_rate(basic_salary):
    basic = as_float(basic_salary)
    return round(basic / PAYROLL_MONTH_DAYS, 2) if basic else 0.0


def _default_earned_salary(basic_salary, work_days, daily_rate=0):
    basic = as_float(basic_salary)
    days = max(as_float(work_days), 0.0)
    if basic:
        return round(basic * days / PAYROLL_MONTH_DAYS, 2)
    rate = as_float(daily_rate)
    if rate:
        return round(rate * days, 2)
    return 0.0


def _apply_attendance_salary(slip):
    slip.daily_rate = _default_daily_rate(slip.basic_salary)
    slip.earned_salary = _default_earned_salary(slip.basic_salary, slip.work_days, slip.daily_rate)


def _arabic_weekday(value):
    raw = (value or "")[:10]
    try:
        year, month, day = [int(part) for part in raw.split("-")]
        return ARABIC_WEEKDAYS[date(year, month, day).weekday()]
    except (TypeError, ValueError):
        return ""


def _normalize_month(value):
    raw = (value or "").strip()
    if len(raw) >= 7 and raw[4] == "-":
        return raw[:7]
    return _current_month()


def _month_last_day(period_month):
    year, month = [int(part) for part in period_month.split("-")]
    return f"{period_month}-{monthrange(year, month)[1]:02d}"


def _empty_attendance_stats():
    return {
        "work_days": 0.0,
        "vacation_days": 0.0,
        "absent_days": 0.0,
        "draft_days": 0.0,
        "posted_days": 0.0,
        "total_days": 0.0,
    }


def _attendance_stats(period_month, include_drafts=False):
    stats = defaultdict(_empty_attendance_stats)
    rows = EmployeeAttendance.query.filter(EmployeeAttendance.date.like(f"{period_month}%")).all()
    for row in rows:
        is_draft = (getattr(row, "record_status", None) or "مرحل") == "مسودة"
        if is_draft:
            stats[row.employee_id]["draft_days"] += 1
            if not include_drafts:
                continue
        else:
            stats[row.employee_id]["posted_days"] += 1
        stats[row.employee_id]["total_days"] += 1
        if row.status in WORK_DAY_STATUSES:
            stats[row.employee_id]["work_days"] += 1
        if row.status in VACATION_STATUSES:
            stats[row.employee_id]["vacation_days"] += 1
        if row.status in ABSENT_STATUSES:
            stats[row.employee_id]["absent_days"] += 1
    return stats


def _last_saved_range(rows):
    if not rows:
        return "", "", ""
    ordered = sorted(rows, key=lambda item: ((item.date or ""), item.id or 0), reverse=True)
    status = ordered[0].status or ""
    start = ordered[0].date or ""
    end = ordered[0].date or ""
    for item in ordered:
        if (item.status or "") != status:
            break
        start = item.date or start
    return start, end, status


def _parse_iso_date(value):
    raw = (value or "").strip()[:10]
    try:
        year, month, day = [int(part) for part in raw.split("-")]
        return date(year, month, day)
    except (TypeError, ValueError):
        return None


def _dates_in_range(from_date, to_date, limit=62):
    start = _parse_iso_date(from_date)
    end = _parse_iso_date(to_date)
    if not start or not end:
        return []
    if end < start:
        start, end = end, start
    days = []
    current = start
    while current <= end and len(days) < limit:
        days.append(current.isoformat())
        current += timedelta(days=1)
    return days


def _month_first_day(period_month):
    return f"{period_month}-01"


def _build_attendance_sheet(employees, period_month):
    stats = _attendance_stats(period_month, include_drafts=True)
    grouped = defaultdict(list)
    rows = (
        EmployeeAttendance.query.filter(EmployeeAttendance.date.like(f"{period_month}%"))
        .order_by(EmployeeAttendance.date.desc(), EmployeeAttendance.id.desc())
        .all()
    )
    for row in rows:
        grouped[row.employee_id].append(row)
    month_from = _month_first_day(period_month)
    month_to = _month_last_day(period_month)
    sheet = []
    for employee in employees:
        emp_rows = grouped.get(employee.id) or []
        last = emp_rows[0] if emp_rows else None
        saved_from, saved_to, saved_status = _last_saved_range(emp_rows)
        counts = stats[employee.id]
        sheet.append({
            "employee": employee,
            "status": saved_status,
            "from_date": saved_from or month_from,
            "to_date": saved_to or month_to,
            "check_in": last.check_in if last else "",
            "check_out": last.check_out if last else "",
            "notes": last.notes if last else "",
            "work_days": counts["work_days"],
            "vacation_days": counts["vacation_days"],
            "absent_days": counts["absent_days"],
            "draft_days": counts["draft_days"],
            "posted_days": counts["posted_days"],
            "total_days": counts["total_days"],
            "record_status": "مسودة" if counts["draft_days"] else ("مرحل" if counts["posted_days"] else ""),
        })
    return sheet


def _employee_remaining(account_id, balances):
    if not account_id:
        return 0.0
    balance = as_float(balances.get(account_id, 0.0))
    return round(-balance if balance < 0 else 0.0, 2)


def _build_payroll_rows(period_month, employees, balances):
    """يبني صفوف كشف المرتبات لشهر معيّن، مستخدَمة في شاشة الموارد البشرية وفي كشف الطباعة."""
    slips = {slip.employee_id: slip for slip in PayrollSlip.query.filter_by(period_month=period_month).all()}
    stats = _attendance_stats(period_month)
    payroll_rows = []
    totals = {
        "basic_salary": 0.0,
        "daily_rate": 0.0,
        "earned_salary": 0.0,
        "transport": 0.0,
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
        basic = slip.basic_salary if slip else employee.basic_salary
        work_days = slip.work_days if slip else (stats[employee.id]["work_days"] or 30)
        daily_rate = slip.daily_rate if slip else _default_daily_rate(basic)
        earned = slip.earned_salary if slip else _default_earned_salary(basic, work_days, daily_rate)
        transport = slip.transport if slip else 0
        overtime = slip.overtime if slip else 0
        incentives = slip.incentives if slip else 0
        advances = slip.advances if slip else 0
        other_deductions = slip.other_deductions if slip else 0
        delay_deduction = slip.delay_deduction if slip else 0
        permission_deduction = slip.permission_deduction if slip else 0
        deductions = other_deductions + delay_deduction + permission_deduction
        net = round(as_float(earned) + as_float(transport) + as_float(overtime) + as_float(incentives) - as_float(advances) - as_float(deductions), 2)
        row = {
            "employee": employee,
            "slip": slip,
            "account_id": account.id if account else None,
            "remaining": remaining,
            "work_days": work_days,
            "vacation_days": slip.vacation_days if slip else stats[employee.id]["vacation_days"],
            "basic_salary": basic,
            "daily_rate": daily_rate,
            "earned_salary": earned,
            "transport": transport,
            "overtime": overtime,
            "incentives": incentives,
            "delay_deduction": delay_deduction,
            "permission_deduction": permission_deduction,
            "other_deductions": deductions,
            "advances": advances,
            "notes": slip.notes if slip else "",
            "status": slip.status if slip else "مسودة",
            "net": slip.net_salary if slip else net,
        }
        payroll_rows.append(row)
        for key in (
            "basic_salary", "daily_rate", "earned_salary", "transport", "overtime", "incentives",
            "delay_deduction", "permission_deduction", "other_deductions", "advances",
        ):
            totals[key] = totals.get(key, 0) + as_float(row[key])
        additions = (
            as_float(row["earned_salary"])
            + as_float(row["transport"])
            + as_float(row["overtime"])
            + as_float(row["incentives"])
        )
        deductions = as_float(row["other_deductions"]) + as_float(row["advances"])
        totals["additions"] += additions
        totals["deductions"] += deductions
        totals["net"] += additions - deductions
        totals["remaining"] += remaining

    return payroll_rows, {key: round(value, 2) for key, value in totals.items()}


def register(app):
    @app.route("/employees", methods=["GET", "POST"])
    def employees():
        sync_journal_related_accounts()
        if request.method == "POST":
            name = (request.form.get("name") or "").strip()
            if not name:
                flash("يرجى إدخال اسم الموظف", "danger")
                return redirect(url_for("employees"))
            code = (request.form.get("code") or "").strip() or None
            if code:
                taken = employee_code_taken(code)
                if taken:
                    flash(
                        f"الكود {code} مسجّل على الموظف {taken.display_name}. غيّره أو اتركه فارغًا ليُكتب تلقائيًا.",
                        "danger",
                    )
                    return redirect(url_for("employees"))
            else:
                code = next_employee_code()
            tag = (request.form.get("tag") or "").strip() or "موظف"
            employee = Employee(
                name=name,
                code=code,
                tag=tag,
                job_title=(request.form.get("job_title") or "").strip() or tag,
                hometown=(request.form.get("hometown") or "").strip() or None,
                site=(request.form.get("site") or "").strip() or None,
                basic_salary=as_float(request.form.get("basic_salary")),
                contact_info=(request.form.get("contact_info") or "").strip() or None,
                notes=request.form.get("notes"),
                is_active=True,
            )
            db.session.add(employee)
            db.session.commit()
            sync_journal_related_accounts()
            flash(f"تم إضافة {employee.display_name} بالكود {employee.display_code} وحسابه في دليل الحسابات", "success")
            return redirect(url_for("employees"))

        items = Employee.query.order_by(Employee.code, Employee.name).all()
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
            next_employee_code=next_employee_code(),
        )

    @app.route("/employees/<int:employee_id>/update", methods=["POST"])
    def update_employee(employee_id):
        item = Employee.query.get_or_404(employee_id)
        name = (request.form.get("name") or "").strip()
        if not name:
            flash("يرجى إدخال اسم الموظف", "danger")
            return redirect(url_for("employees"))
        item.name = name
        submitted_code = (request.form.get("code") or "").strip()
        if submitted_code:
            taken = employee_code_taken(submitted_code, exclude_id=item.id)
            if taken:
                flash(
                    f"الكود {submitted_code} مسجّل على الموظف {taken.display_name}. غيّره أو امسحه ليُكتب تلقائيًا.",
                    "danger",
                )
                return redirect(url_for("employees"))
            item.code = submitted_code
        else:
            item.code = next_employee_code(exclude_id=item.id)
        item.tag = (request.form.get("tag") or "").strip() or item.tag
        item.job_title = (request.form.get("job_title") or "").strip() or item.job_title
        item.hometown = (request.form.get("hometown") or "").strip() or None
        item.site = (request.form.get("site") or "").strip() or None
        item.basic_salary = as_float(request.form.get("basic_salary"))
        item.contact_info = (request.form.get("contact_info") or "").strip() or None
        item.notes = request.form.get("notes")
        item.is_active = request.form.get("is_active") == "on"
        get_or_create_employee_account(item)
        db.session.commit()
        flash("تم تحديث بيانات الموظف واسم حسابه", "success")
        return redirect(url_for("employees"))

    @app.route("/employees/<int:employee_id>/delete", methods=["POST"])
    def delete_employee(employee_id):
        from services.immutability import PARTY_DELETE_MESSAGE, account_has_any_journals, delete_unused_account

        item = Employee.query.get_or_404(employee_id)
        if (
            EmployeeAttendance.query.filter_by(employee_id=item.id).first()
            or PayrollSlip.query.filter_by(employee_id=item.id).first()
            or EmployeeSalaryPayment.query.filter_by(employee_id=item.id).first()
        ):
            flash(PARTY_DELETE_MESSAGE, "danger")
            return redirect(url_for("employees"))
        account = get_account_by_code(f"EMP-{item.id:04d}")
        if account and account_has_any_journals(account.id):
            flash(PARTY_DELETE_MESSAGE, "danger")
            return redirect(url_for("employees"))
        name = item.display_name
        db.session.delete(item)
        delete_unused_account(account)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash(PARTY_DELETE_MESSAGE, "danger")
            return redirect(url_for("employees"))
        flash(f"تم حذف الموظف {name}", "success")
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
        treasury_accounts = filter_hidden_treasuries(
            [account for account in accounts if is_treasury_account(account)]
        )
        balances = build_account_balances(accounts)

        if request.method == "POST":
            action = (request.form.get("action") or "").strip()
            try:
                if action in {"attendance", "save_attendance", "post_attendance"}:
                    if action == "attendance":
                        return _save_attendance()
                    return _save_attendance_sheet(period_month, post=action == "post_attendance")
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
        payroll_rows, totals = _build_payroll_rows(period_month, employees, balances)

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
            attendance_from=_month_first_day(period_month),
            attendance_to=_month_last_day(period_month),
            employees=employees,
            attendance_rows=attendance_rows,
            attendance_sheet=_build_attendance_sheet(employees, period_month),
            attendance_statuses=ATTENDANCE_STATUSES,
            payroll_rows=payroll_rows,
            totals=totals,
            treasury_accounts=treasury_accounts,
            payments=payments,
            salary_expense=salary_expense,
            salary_expense_balance=round(salary_expense_balance, 2),
            tag_options=EMPLOYEE_TAG_OPTIONS,
            active_tab=request.values.get("tab") or "attendance",
        )

    @app.route("/hr/payroll/<int:slip_id>/print")
    def print_payslip(slip_id):
        slip = PayrollSlip.query.get_or_404(slip_id)
        employee = slip.employee
        payslip_date = slip.date or _month_last_day(slip.period_month)
        now = datetime.now()
        clock_label = f"{now.strftime('%I:%M')} {'م' if now.hour >= 12 else 'ص'}"
        entitlements = [
            ("الراتب", slip.earned_salary_value),
            ("مواصلات", as_float(slip.transport)),
            ("الاضافى", as_float(slip.overtime)),
            ("منح", as_float(slip.incentives)),
        ]
        deduction_amount = (
            as_float(slip.delay_deduction)
            + as_float(slip.permission_deduction)
            + as_float(slip.other_deductions)
        )
        deductions = [
            ("سلف", as_float(slip.advances)),
            ("خصومات", deduction_amount),
        ]
        return render_template(
            "print_payslip.html",
            slip=slip,
            employee=employee,
            payslip_date=payslip_date,
            weekday_name=_arabic_weekday(payslip_date),
            print_time=clock_label,
            entitlements=entitlements,
            deductions=deductions,
            entitlements_total=slip.additions_total,
            deductions_total=slip.deductions_total,
            company_name=PAYSLIP_COMPANY_NAME,
            finance_manager=PAYSLIP_FINANCE_MANAGER,
        )

    @app.route("/hr/payroll/print")
    def print_payroll_sheet():
        period_month = _normalize_month(request.args.get("period_month"))
        employees = Employee.query.filter_by(is_active=True).order_by(Employee.name).all()
        if not employees:
            employees = Employee.query.order_by(Employee.name).all()
        accounts = ChartOfAccount.query.order_by(ChartOfAccount.code).all()
        balances = build_account_balances(accounts)
        payroll_rows, totals = _build_payroll_rows(period_month, employees, balances)
        now = datetime.now()
        clock_label = f"{now.strftime('%I:%M')} {'م' if now.hour >= 12 else 'ص'}"
        today_iso = date.today().isoformat()
        return render_template(
            "print_payroll_sheet.html",
            period_month=period_month,
            payroll_rows=payroll_rows,
            totals=totals,
            company_name=PAYSLIP_COMPANY_NAME,
            finance_manager=PAYSLIP_FINANCE_MANAGER,
            print_date=today_iso,
            weekday_name=_arabic_weekday(today_iso),
            print_time=clock_label,
        )

    @app.route("/hr/attendance/print")
    def print_attendance_sheet():
        period_month = _normalize_month(request.args.get("period_month"))
        employees = Employee.query.filter_by(is_active=True).order_by(Employee.name).all()
        if not employees:
            employees = Employee.query.order_by(Employee.name).all()
        attendance_rows = (
            EmployeeAttendance.query.filter(EmployeeAttendance.date.like(f"{period_month}%"))
            .order_by(EmployeeAttendance.date, EmployeeAttendance.id)
            .all()
        )
        now = datetime.now()
        clock_label = f"{now.strftime('%I:%M')} {'م' if now.hour >= 12 else 'ص'}"
        today_iso = date.today().isoformat()
        return render_template(
            "print_attendance.html",
            period_month=period_month,
            attendance_sheet=_build_attendance_sheet(employees, period_month),
            attendance_rows=attendance_rows,
            company_name=PAYSLIP_COMPANY_NAME,
            finance_manager=PAYSLIP_FINANCE_MANAGER,
            print_date=today_iso,
            weekday_name=_arabic_weekday(today_iso),
            print_time=clock_label,
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
        if request.form.get("record_status") in {"مسودة", "مرحل"}:
            row.record_status = request.form.get("record_status")
        db.session.commit()
        flash("تم تعديل سجل الحضور", "success")
        return redirect(url_for("hr_module", period_month=_normalize_month(row.date), tab="attendance"))

    @app.route("/hr/attendance/<int:row_id>/delete", methods=["POST"])
    def delete_attendance(row_id):
        row = EmployeeAttendance.query.get_or_404(row_id)
        period_month = _normalize_month(row.date)
        db.session.delete(row)
        db.session.commit()
        flash("تم حذف سجل الحضور", "success")
        return redirect(url_for("hr_module", period_month=period_month, tab="attendance"))

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
        if payment.treasury_account_id and not user_can_see_account(payment.treasury_account_id):
            flash("ليست لديك صلاحية استخدام هذه الخزنة", "danger")
            return redirect(url_for("hr_module", period_month=_normalize_month(pay_date)))
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
    _apply_attendance_salary(slip)
    slip.transport = as_float(form.get(f"{prefix}transport"))
    slip.overtime = as_float(form.get(f"{prefix}overtime"))
    slip.incentives = as_float(form.get(f"{prefix}incentives"))
    slip.delay_deduction = as_float(form.get(f"{prefix}delay_deduction"))
    slip.permission_deduction = as_float(form.get(f"{prefix}permission_deduction"))
    slip.other_deductions = as_float(form.get(f"{prefix}other_deductions"))
    slip.advances = as_float(form.get(f"{prefix}advances"))
    if form.get(f"{prefix}notes") is not None:
        slip.notes = form.get(f"{prefix}notes")


def _upsert_attendance_day(employee_id, row_date, status, check_in, check_out, notes, record_status):
    existing = EmployeeAttendance.query.filter_by(employee_id=employee_id, date=row_date).first()
    row = existing or EmployeeAttendance(employee_id=employee_id, date=row_date)
    row.status = status
    row.check_in = check_in
    row.check_out = check_out
    row.notes = notes
    row.record_status = record_status
    if existing is None:
        db.session.add(row)
    return row


def _apply_attendance_to_payroll(period_month):
    stats = _attendance_stats(period_month)
    employees = Employee.query.filter_by(is_active=True).all() or Employee.query.all()
    posting_date = _month_last_day(period_month)
    updated = 0
    for employee in employees:
        work_days = stats[employee.id]["work_days"]
        vacation_days = stats[employee.id]["vacation_days"]
        if work_days <= 0 and vacation_days <= 0:
            continue
        slip = PayrollSlip.query.filter_by(employee_id=employee.id, period_month=period_month).first()
        if slip is None:
            slip = PayrollSlip(
                employee_id=employee.id,
                period_month=period_month,
                date=posting_date,
                basic_salary=employee.basic_salary or 0,
                status="مسودة",
            )
            db.session.add(slip)
            db.session.flush()
        slip.work_days = work_days
        slip.vacation_days = vacation_days
        if not slip.basic_salary:
            slip.basic_salary = employee.basic_salary or 0
        _apply_attendance_salary(slip)
        if slip.status == "مرحل":
            sync_payroll_slip_journal(slip)
        updated += 1
    return updated


def _save_attendance():
    employee_id = as_int(request.form.get("employee_id"))
    row_date = request.form.get("date") or date.today().isoformat()
    if not employee_id:
        flash("يرجى اختيار الموظف", "danger")
        return redirect(url_for("hr_module", period_month=_normalize_month(row_date), tab="attendance"))
    status = request.form.get("status") or "حضور"
    if status not in ATTENDANCE_STATUSES:
        status = "حضور"
    try:
        _upsert_attendance_day(
            employee_id,
            row_date,
            status,
            (request.form.get("check_in") or "").strip() or None,
            (request.form.get("check_out") or "").strip() or None,
            request.form.get("notes"),
            "مرحل",
        )
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash("يوجد سجل حضور لنفس الموظف في هذا اليوم", "danger")
        return redirect(url_for("hr_module", period_month=_normalize_month(row_date), tab="attendance"))
    flash("تم حفظ سجل الحضور والانصراف", "success")
    return redirect(url_for("hr_module", period_month=_normalize_month(row_date), tab="attendance"))


def _save_attendance_sheet(period_month, post=False):
    employee_ids = request.form.getlist("att_employee_id")
    if not employee_ids:
        flash("لا يوجد موظفون لتسجيل الحضور", "danger")
        return redirect(url_for("hr_module", period_month=period_month, tab="attendance"))

    default_from = request.form.get("attendance_from") or _month_first_day(period_month)
    default_to = request.form.get("attendance_to") or _month_last_day(period_month)
    record_status = "مرحل" if post else "مسودة"
    saved_days = 0
    skipped = 0

    def field(name, index):
        values = request.form.getlist(name)
        return values[index] if index < len(values) else ""

    for index, raw_id in enumerate(employee_ids):
        employee_id = as_int(raw_id)
        if not employee_id:
            continue
        status = (field("att_status", index) or "").strip()
        if not status:
            skipped += 1
            continue
        if status not in ATTENDANCE_STATUSES:
            flash(f"حالة غير معروفة في صف الموظف رقم {index + 1}", "danger")
            return redirect(url_for("hr_module", period_month=period_month, tab="attendance"))
        from_date = (field("att_from", index) or "").strip() or default_from
        to_date = (field("att_to", index) or "").strip() or default_to
        days = _dates_in_range(from_date, to_date)
        if not days:
            flash(f"يرجى إدخال فترة صحيحة من-إلى في الصف رقم {index + 1}", "danger")
            return redirect(url_for("hr_module", period_month=period_month, tab="attendance"))
        check_in = (field("att_check_in", index) or "").strip() or None
        check_out = (field("att_check_out", index) or "").strip() or None
        notes = (field("att_notes", index) or "").strip() or None
        for day in days:
            _upsert_attendance_day(employee_id, day, status, check_in, check_out, notes, record_status)
            saved_days += 1

    if saved_days == 0 and skipped and not post:
        flash(
            "لم يُحفظ شيء. اختر الحالة لكل موظف (أو استخدم تطبيق حالة على الكل) ثم حدّد الفترة من-إلى.",
            "warning",
        )
        return redirect(url_for("hr_module", period_month=period_month, tab="attendance"))

    payroll_updated = 0
    posted_existing = 0
    if post:
        db.session.flush()
        drafts = EmployeeAttendance.query.filter(
            EmployeeAttendance.date.like(f"{period_month}%"),
            EmployeeAttendance.record_status == "مسودة",
        ).all()
        for row in drafts:
            row.record_status = "مرحل"
            posted_existing += 1
        db.session.flush()
        payroll_updated = _apply_attendance_to_payroll(period_month)
    db.session.commit()
    if post:
        flash(
            f"تم ترحيل {saved_days + posted_existing} يوم حضور وتحديث {payroll_updated} كشف مرتب بأيام العمل الفعلية.",
            "success",
        )
    else:
        flash(
            f"تم حفظ {saved_days} يوم حضور كمسودة. الأرقام على يسار الكشف محدّثة، والترحيل في آخر الشهر يسمّع في كشف المرتبات.",
            "success",
        )
    return redirect(url_for("hr_module", period_month=period_month, tab="attendance"))


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
        _apply_attendance_salary(slip)
        slip.transport = as_float(field("transport"))
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
    if payment.treasury_account_id and not user_can_see_account(payment.treasury_account_id):
        flash("ليست لديك صلاحية استخدام هذه الخزنة", "danger")
        return redirect(url_for("hr_module", period_month=_normalize_month(pay_date)))
    db.session.add(payment)
    db.session.commit()
    sync_employee_salary_payment_journal(payment)
    flash("تم صرف المرتب من الخزنة على حساب الموظف دون المساس بمصروف المرتبات", "success")
    return redirect(url_for("hr_module", period_month=_normalize_month(pay_date)))
