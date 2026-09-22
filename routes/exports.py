from flask import flash, redirect, request, url_for

from models import (
    ChartOfAccount, ClientReceipt, CustodySettlement, DriverCompensationEntry, Employee,
    EmployeeAttendance, EmployeeSalaryPayment, Equipment, Estimation, InventoryTransaction,
    JournalEntry, LaborEntry, PayrollSlip, ProgressPayment, Project, PurchaseOrder, SalesTrip,
    Subcontractor, Supplier, SupplierPayment,
)
from services.accounting import as_float
from services.authz import first_allowed_endpoint, user_can
from services.workbook import excel_download


SCREEN_PERMS = {
    "journal": ("journal", "journal.view", "journal.create"),
    "accounts": ("accounts", "accounts.create"),
    "purchase_orders": ("purchase_orders",),
    "inventory": ("inventory",),
    "projects": ("projects", "projects.view", "projects.create"),
    "estimations": ("estimations", "estimations.view", "estimations.create"),
    "progress_payments": ("progress", "progress.view", "progress.create"),
    "client_progress_payments": ("progress", "progress.view", "progress.create"),
    "client_receipts": ("receipts", "receipts.create"),
    "supplier_payments": ("supplier_payments", "supplier_payments.create"),
    "suppliers": ("suppliers",),
    "subcontractors": ("subcontractors",),
    "custody": ("custody", "custody.view", "custody.create"),
    "labor": ("labor", "labor.view"),
    "employees": ("hr", "hr.view", "hr.create"),
    "payroll": ("hr", "hr.view", "hr.create"),
    "attendance": ("hr", "hr.view", "hr.create"),
    "salary_payments": ("hr", "hr.view", "hr.create"),
    "equipment": ("equipment", "equipment.view"),
    "drivers": ("drivers",),
    "sales": ("sales", "sales.view", "sales.create"),
}


def _can_export(screen):
    required = SCREEN_PERMS.get(screen)
    if not required:
        return False
    return any(user_can(item) for item in required)


def _rows_journal():
    headers = ["الرقم", "التاريخ", "البيان", "مدين", "دائن", "المبلغ", "الحالة", "المشروع"]
    rows = []
    for item in JournalEntry.query.order_by(JournalEntry.date.desc(), JournalEntry.id.desc()).all():
        rows.append([
            item.display_number,
            item.date or "",
            item.description or "",
            f"{item.debit_account.code} - {item.debit_account.name}" if item.debit_account else "",
            f"{item.credit_account.code} - {item.credit_account.name}" if item.credit_account else "",
            item.amount or 0,
            item.status or "",
            item.project.display_name if item.project else "",
        ])
    return "القيود", headers, rows


def _rows_accounts():
    headers = ["الكود", "الاسم", "الفئة", "الرصيد الافتتاحي", "تبويب المصروف"]
    rows = [
        [item.code, item.name, item.category, item.opening_balance or 0, item.expense_class or ""]
        for item in ChartOfAccount.query.order_by(ChartOfAccount.category, ChartOfAccount.code).all()
    ]
    return "الحسابات", headers, rows


def _rows_purchase_orders():
    headers = ["الرقم", "التاريخ", "المشروع", "المورد", "الحالة", "القيمة"]
    rows = []
    for item in PurchaseOrder.query.order_by(PurchaseOrder.date.desc(), PurchaseOrder.id.desc()).all():
        rows.append([
            item.order_number or item.id,
            item.date or "",
            item.project.display_name if item.project else "",
            item.supplier.name if item.supplier else "",
            item.status or "",
            item.total_value or 0,
        ])
    return "أوامر الشراء", headers, rows


def _rows_inventory():
    headers = ["التاريخ", "المشروع", "المخزن", "المادة", "الكمية", "تكلفة الوحدة", "النوع"]
    rows = []
    for item in InventoryTransaction.query.order_by(InventoryTransaction.id.desc()).all():
        rows.append([
            item.date or "",
            item.project.display_name if item.project else "",
            item.warehouse_name or "",
            item.material_name or "",
            item.quantity or 0,
            item.unit_cost or 0,
            item.transaction_type or "",
        ])
    return "المخزون", headers, rows


def _rows_projects():
    headers = ["الكود", "اسم المشروع", "العميل", "قيمة العقد", "البداية", "النهاية", "النوع"]
    rows = [
        [item.code, item.project_name, item.client_name, item.contract_value or 0, item.start_date or "", item.end_date or "", item.contract_type or ""]
        for item in Project.query.order_by(Project.code).all()
    ]
    return "المشاريع", headers, rows


def _rows_estimations():
    headers = ["الرقم", "العميل", "التاريخ", "القيمة النهائية", "الحالة"]
    rows = [
        [item.code, item.client_name, item.date or "", item.final_value or 0, item.status or ""]
        for item in Estimation.query.order_by(Estimation.id.desc()).all()
    ]
    return "المقايسات", headers, rows


def _rows_progress(client_only=False):
    query = ProgressPayment.query
    if client_only:
        query = query.filter(ProgressPayment.subcontractor_id.is_(None))
        title = "مستخلصات العملاء"
    else:
        query = query.filter(ProgressPayment.subcontractor_id.isnot(None))
        title = "مستخلصات الباطن"
    headers = ["الرقم", "التاريخ", "المشروع", "الجهة", "إجمالي الأعمال", "الصافي"]
    rows = []
    for item in query.order_by(ProgressPayment.id.desc()).all():
        party = item.project.client_name if client_only and item.project else (item.subcontractor.name if item.subcontractor else "")
        rows.append([
            item.document_number,
            item.date or "",
            item.project.display_name if item.project else "",
            party or "",
            item.total_value or 0,
            item.net_value or 0,
        ])
    return title, headers, rows


def _rows_client_receipts():
    headers = ["الرقم", "التاريخ", "العميل", "المبلغ", "الطريقة"]
    rows = [
        [item.document_number, item.date or "", item.client_name, item.amount or 0, item.payment_method or ""]
        for item in ClientReceipt.query.order_by(ClientReceipt.id.desc()).all()
    ]
    return "تحصيل العملاء", headers, rows


def _rows_supplier_payments():
    headers = ["الرقم", "التاريخ", "المورد", "المبلغ", "الطريقة"]
    rows = [
        [item.document_number, item.date or "", item.supplier.name if item.supplier else "", item.amount or 0, item.payment_method or ""]
        for item in SupplierPayment.query.order_by(SupplierPayment.id.desc()).all()
    ]
    return "سداد الموردين", headers, rows


def _rows_suppliers():
    headers = ["الكود", "الاسم", "التصنيف", "الاتصال"]
    rows = [
        [item.code or "", item.name, item.entity_kind or "", item.contact_info or ""]
        for item in Supplier.query.order_by(Supplier.name).all()
    ]
    return "الموردون", headers, rows


def _rows_subcontractors():
    headers = ["الكود", "الاسم", "التصنيف", "الاتصال"]
    rows = [
        [item.code or "", item.name, item.entity_kind or "", item.contact_info or ""]
        for item in Subcontractor.query.order_by(Subcontractor.name).all()
    ]
    return "مقاولو الباطن", headers, rows


def _rows_custody():
    headers = ["التاريخ", "صاحب العهدة", "النوع", "المبلغ", "الملاحظات"]
    rows = [
        [item.date or "", item.entity_name or "", item.operation_type or item.voucher_type, item.amount or 0, item.notes or ""]
        for item in CustodySettlement.query.order_by(CustodySettlement.id.desc()).all()
    ]
    return "تسوية العهد", headers, rows


def _rows_labor():
    headers = ["التاريخ", "المشروع", "الوصف", "الساعات", "المبلغ"]
    rows = [
        [item.date or "", item.project.display_name if item.project else "", item.description or "", item.hours or 0, item.amount or 0]
        for item in LaborEntry.query.order_by(LaborEntry.id.desc()).all()
    ]
    return "العمالة", headers, rows


def _rows_employees():
    headers = ["الكود", "الاسم", "الوظيفة", "البلد", "الموقع", "المرتب الأساسي"]
    rows = [
        [item.display_code, item.name, item.payroll_job_title, item.hometown or "", item.site or "", item.basic_salary or 0]
        for item in Employee.query.order_by(Employee.name).all()
    ]
    return "الموظفون", headers, rows


def _rows_payroll(period_month):
    headers = ["م", "الاسم", "البلد", "الوظيفة", "موقع", "الراتب الأساسي", "اليومية", "عدد الأيام", "الراتب", "موصلات", "اضافي", "منح", "سلف", "خصومات", "الصافي النقدي", "ملاحظات"]
    query = PayrollSlip.query
    if period_month:
        query = query.filter_by(period_month=period_month)
    rows = []
    for index, item in enumerate(query.order_by(PayrollSlip.id.asc()).all(), start=1):
        employee = item.employee
        deductions = as_float(item.delay_deduction) + as_float(item.permission_deduction) + as_float(item.other_deductions)
        rows.append([
            index,
            employee.name if employee else "",
            employee.payroll_hometown if employee else "",
            employee.payroll_job_title if employee else "",
            employee.payroll_site if employee else "",
            item.basic_salary or 0,
            item.daily_rate_value,
            item.work_days or 0,
            item.earned_salary_value,
            item.transport or 0,
            item.overtime or 0,
            item.incentives or 0,
            item.advances or 0,
            deductions,
            item.net_salary,
            item.notes or "",
        ])
    return "كشف المرتبات", headers, rows


def _rows_attendance(period_month):
    headers = ["التاريخ", "الموظف", "الحالة", "حضور", "انصراف", "تأخير", "استئذان"]
    query = EmployeeAttendance.query
    if period_month:
        query = query.filter(EmployeeAttendance.date.like(f"{period_month}%"))
    rows = [
        [item.date, item.employee.display_name if item.employee else "", item.status, item.check_in or "", item.check_out or "", item.delay_minutes or 0, item.permission_hours or 0]
        for item in query.order_by(EmployeeAttendance.date.desc()).all()
    ]
    return "الحضور", headers, rows


def _rows_salary_payments(period_month):
    headers = ["الرقم", "التاريخ", "الموظف", "المبلغ", "الطريقة"]
    query = EmployeeSalaryPayment.query
    if period_month:
        query = query.filter(EmployeeSalaryPayment.date.like(f"{period_month}%"))
    rows = [
        [item.document_number, item.date or "", item.employee.display_name if item.employee else "", item.amount or 0, item.payment_method or ""]
        for item in query.order_by(EmployeeSalaryPayment.id.desc()).all()
    ]
    return "صرف المرتبات", headers, rows


def _rows_equipment():
    headers = ["المعدة", "تكلفة الشراء", "تشغيل", "صيانة", "ساعات", "المشروع"]
    rows = [
        [item.name, item.purchase_cost or 0, item.operating_cost or 0, item.maintenance or 0, item.hours_used or 0, item.project.display_name if item.project else ""]
        for item in Equipment.query.order_by(Equipment.name).all()
    ]
    return "المعدات", headers, rows


def _rows_drivers():
    headers = ["التاريخ", "السائق", "الطريقة", "الوحدات", "السعر", "الاستحقاق", "المسدد"]
    rows = [
        [item.date or "", item.driver_name, item.settlement_basis or "", item.units or 0, item.unit_rate or 0, item.gross_amount or 0, item.paid_amount or 0]
        for item in DriverCompensationEntry.query.order_by(DriverCompensationEntry.id.desc()).all()
    ]
    return "محاسبة السواقين", headers, rows


def _rows_sales():
    from routes.sales import arabic_weekday
    query = SalesTrip.query
    from_date = (request.args.get("from_date") or "").strip()
    to_date = (request.args.get("to_date") or "").strip()
    driver_name = (request.args.get("driver_name") or "").strip()
    client_name = (request.args.get("client_name") or "").strip()
    voucher = (request.args.get("voucher") or "").strip()
    if from_date:
        query = query.filter(SalesTrip.date >= from_date)
    if to_date:
        query = query.filter(SalesTrip.date <= to_date)
    if driver_name:
        query = query.filter(SalesTrip.driver_name == driver_name)
    if client_name:
        query = query.filter(SalesTrip.client_name == client_name)
    if voucher:
        query = query.filter(SalesTrip.voucher_number.contains(voucher))
    headers = [
        "التاريخ", "اليوم", "رقم البون", "النوع", "العميل", "البيان", "المسافة", "اسم السائق",
        "رقم المعدة", "التكعيب", "الخصم", "صافي الكمية", "السعر", "الإجمالي",
        "عهدة ودفعات", "المتبقي",
    ]
    rows = []
    total_amount = 0.0
    advances = 0.0
    remaining = 0.0
    cubage = 0.0
    discount = 0.0
    net_quantity = 0.0
    for item in query.order_by(SalesTrip.date.desc(), SalesTrip.id.desc()).all():
        cubage += as_float(item.cubage)
        discount += as_float(item.discount)
        net_quantity += as_float(item.net_quantity)
        total_amount += as_float(item.total_amount)
        advances += as_float(item.advances)
        remaining += as_float(item.remaining)
        rows.append([
            item.date or "",
            arabic_weekday(item.date),
            item.document_number,
            item.trip_type or "",
            item.client_name or "",
            item.notes or "",
            item.distance_km or 0,
            item.driver_name,
            item.tractor_number or "",
            item.cubage or 0,
            item.discount or 0,
            item.net_quantity or 0,
            item.unit_price or 0,
            item.total_amount or 0,
            item.advances or 0,
            item.remaining or 0,
        ])
    rows.append([
        "الإجمالي", "", "", "", "", "", "", "", "", cubage, discount, net_quantity, "",
        total_amount, advances, remaining,
    ])
    return "المبيعات", headers, rows


BUILDERS = {
    "journal": lambda: _rows_journal(),
    "accounts": lambda: _rows_accounts(),
    "purchase_orders": lambda: _rows_purchase_orders(),
    "inventory": lambda: _rows_inventory(),
    "projects": lambda: _rows_projects(),
    "estimations": lambda: _rows_estimations(),
    "progress_payments": lambda: _rows_progress(False),
    "client_progress_payments": lambda: _rows_progress(True),
    "client_receipts": lambda: _rows_client_receipts(),
    "supplier_payments": lambda: _rows_supplier_payments(),
    "suppliers": lambda: _rows_suppliers(),
    "subcontractors": lambda: _rows_subcontractors(),
    "custody": lambda: _rows_custody(),
    "labor": lambda: _rows_labor(),
    "employees": lambda: _rows_employees(),
    "payroll": lambda: _rows_payroll((request.args.get("period_month") or "").strip()),
    "attendance": lambda: _rows_attendance((request.args.get("period_month") or "").strip()),
    "salary_payments": lambda: _rows_salary_payments((request.args.get("period_month") or "").strip()),
    "equipment": lambda: _rows_equipment(),
    "drivers": lambda: _rows_drivers(),
    "sales": lambda: _rows_sales(),
}


def register(app):
    @app.route("/export/<screen>")
    def export_screen(screen):
        if not _can_export(screen) or screen not in BUILDERS:
            flash("ليست لديك صلاحية تصدير هذه الشاشة", "danger")
            return redirect(url_for(first_allowed_endpoint()))
        title, headers, rows = BUILDERS[screen]()
        filename = f"{title}.xlsx"
        return excel_download(filename, headers, rows, title=title)
