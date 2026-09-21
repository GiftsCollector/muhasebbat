from functools import wraps
import json

from flask import flash, g, has_request_context, redirect, request, url_for

from models import ROLE_ACCOUNTANT, ROLE_ADMIN, ROLE_DATA_ENTRY, ROLE_LABELS, ROLE_PROJECT_MANAGER

ROLE_OPTIONS = [
    (ROLE_ADMIN, ROLE_LABELS[ROLE_ADMIN]),
    (ROLE_ACCOUNTANT, ROLE_LABELS[ROLE_ACCOUNTANT]),
    (ROLE_PROJECT_MANAGER, ROLE_LABELS[ROLE_PROJECT_MANAGER]),
    (ROLE_DATA_ENTRY, ROLE_LABELS[ROLE_DATA_ENTRY]),
]

ALL = "*"
ADMIN_LOCKED_PERMS = {ALL, "dashboard"}
LANDING_ENDPOINTS = (
    "sales_trips",
    "journal",
    "purchase_orders",
    "inventory",
    "projects",
    "estimations",
    "progress_payments",
    "client_progress_payments",
    "client_receipts",
    "supplier_payments",
    "hr_module",
    "employees",
    "labor",
    "custody_settlements",
    "driver_compensation",
    "equipment",
    "accounts",
    "suppliers",
    "subcontractors",
    "change_password",
)

ROLE_PERMS = {
    ROLE_ADMIN: {ALL},
    ROLE_ACCOUNTANT: {
        "dashboard", "journal", "journal.post", "journal.unpost", "accounts",
        "reports", "receipts", "supplier_payments", "custody", "drivers", "sales",
        "progress.view", "progress.delete", "estimations.view", "suppliers", "subcontractors",
        "purchase_orders", "inventory", "periods", "attachments", "print",
        "labor.view", "equipment.view", "projects.view", "hr",
    },
    ROLE_PROJECT_MANAGER: {
        "dashboard", "projects", "estimations", "progress", "subcontractors",
        "suppliers", "reports", "labor", "equipment", "journal.view",
        "purchase_orders", "inventory", "attachments", "print", "custody.view",
        "drivers", "sales",
    },
    ROLE_DATA_ENTRY: {
        "dashboard", "progress.create", "estimations.create", "purchase_orders",
        "inventory", "labor", "equipment", "custody.create", "journal.create",
        "receipts.create", "supplier_payments.create", "attachments",
        "projects.view", "print", "drivers", "hr", "sales",
    },
}

# توسيع الاختصارات: progress يشمل العرض والإنشاء، وهكذا
ALIASES = {
    "progress": {"progress", "progress.view", "progress.create", "progress.update", "progress.delete"},
    "estimations": {"estimations", "estimations.view", "estimations.create", "estimations.update", "estimations.delete"},
    "journal": {"journal", "journal.view", "journal.create", "journal.update", "journal.delete", "journal.post"},
    "custody": {"custody", "custody.view", "custody.create", "custody.update", "custody.delete"},
    "labor": {"labor", "labor.view", "labor.create", "labor.update", "labor.delete"},
    "equipment": {"equipment", "equipment.view", "equipment.create", "equipment.update", "equipment.delete"},
    "reports": {"reports", "reports.projects"},
    "receipts": {"receipts", "receipts.create", "receipts.update", "receipts.delete"},
    "supplier_payments": {"supplier_payments", "supplier_payments.create", "supplier_payments.update", "supplier_payments.delete"},
    "hr": {"hr", "hr.view", "hr.create", "hr.update", "hr.delete"},
    "accounts": {"accounts", "accounts.create", "accounts.update", "accounts.delete"},
    "suppliers": {"suppliers", "suppliers.create", "suppliers.update", "suppliers.delete"},
    "subcontractors": {"subcontractors", "subcontractors.create", "subcontractors.update", "subcontractors.delete"},
    "projects": {"projects", "projects.view", "projects.create", "projects.update", "projects.delete"},
    "purchase_orders": {"purchase_orders", "purchase_orders.create", "purchase_orders.update", "purchase_orders.delete"},
    "inventory": {"inventory", "inventory.create", "inventory.update", "inventory.delete"},
    "drivers": {"drivers", "drivers.create", "drivers.update", "drivers.delete"},
    "sales": {"sales", "sales.view", "sales.create", "sales.update", "sales.delete"},
}

PERMISSION_GROUPS = [
    {
        "id": "access",
        "label": "الوصول",
        "permissions": [
            ("dashboard", "الشاشة الرئيسية"),
        ],
    },
    {
        "id": "accounts_reports",
        "label": "الحسابات والتقارير",
        "permissions": [
            ("accounts", "عرض الحسابات"),
            ("accounts.create", "إضافة حسابات"),
            ("accounts.update", "تعديل الحسابات"),
            ("accounts.delete", "حذف الحسابات"),
            ("reports", "التقارير"),
            ("print", "الطباعة"),
            ("attachments", "المرفقات"),
            ("periods", "إقفال الفترات"),
        ],
    },
    {
        "id": "journal",
        "label": "القيود والترحيل",
        "permissions": [
            ("journal.view", "عرض القيود"),
            ("journal.create", "إضافة قيود"),
            ("journal.update", "تعديل القيود"),
            ("journal.delete", "حذف القيود"),
            ("journal.post", "ترحيل القيود"),
            ("journal.unpost", "إلغاء ترحيل القيود"),
        ],
    },
    {
        "id": "progress",
        "label": "المستخلصات",
        "permissions": [
            ("progress.view", "عرض المستخلصات"),
            ("progress.create", "إضافة مستخلصات"),
            ("progress.update", "تعديل المستخلصات"),
            ("progress.delete", "حذف المستخلصات"),
            ("estimations.view", "عرض المقايسات"),
            ("estimations.create", "إضافة/تعديل المقايسات"),
        ],
    },
    {
        "id": "parties",
        "label": "الموردون والمقاولون",
        "permissions": [
            ("suppliers", "عرض الموردين"),
            ("suppliers.create", "إضافة موردين"),
            ("suppliers.update", "تعديل الموردين"),
            ("suppliers.delete", "حذف الموردين"),
            ("subcontractors", "عرض مقاولي الباطن"),
            ("subcontractors.create", "إضافة مقاولين"),
            ("subcontractors.update", "تعديل المقاولين"),
            ("subcontractors.delete", "حذف المقاولين"),
            ("receipts.create", "تحصيل العملاء"),
            ("supplier_payments.create", "سداد الموردين"),
        ],
    },
    {
        "id": "hr",
        "label": "الموظفون والمرتبات",
        "permissions": [
            ("hr", "عرض الموظفين والمرتبات"),
            ("hr.create", "إضافة موظفين ومرتبات"),
            ("hr.update", "تعديل الموظفين والمرتبات"),
            ("hr.delete", "حذف الموظفين والمرتبات"),
        ],
    },
    {
        "id": "projects_inventory",
        "label": "المشاريع والمخازن",
        "permissions": [
            ("projects.view", "عرض المشاريع"),
            ("projects.create", "إضافة مشاريع"),
            ("projects.update", "تعديل المشاريع"),
            ("projects.delete", "حذف المشاريع"),
            ("purchase_orders", "أوامر الشراء"),
            ("inventory", "المخازن"),
            ("labor", "العمالة"),
            ("equipment", "المعدات"),
            ("custody", "تسوية العهد"),
            ("drivers", "محاسبة السواقين"),
            ("sales", "المبيعات والنقلات"),
        ],
    },
]

ALLOWED_PERMISSION_KEYS = {key for group in PERMISSION_GROUPS for key, _label in group["permissions"]}
ALLOWED_PERMISSION_KEYS.update({"dashboard", ALL})

DOCUMENT_DELETE_PERMS = {
    "progress_payment": ("progress.delete", "progress"),
    "subcontractor_payment": ("progress.delete", "progress"),
    "retention_release": ("progress.delete", "progress"),
    "estimation": ("estimations.delete", "estimations"),
    "purchase_order": ("purchase_orders.delete", "purchase_orders"),
    "inventory": ("inventory.delete", "inventory"),
    "custody": ("custody.delete", "custody"),
    "labor": ("labor.delete", "labor"),
    "equipment": ("equipment.delete", "equipment"),
    "driver": ("drivers.delete", "drivers"),
    "sales_trip": ("sales.delete", "sales"),
    "client_receipt": ("receipts.delete", "receipts"),
    "supplier_payment": ("supplier_payments.delete", "supplier_payments"),
    "payroll_slip": ("hr.delete", "hr"),
    "employee_salary_payment": ("hr.delete", "hr"),
}


ENDPOINT_WRITE_PERMS = {
    "accounts": "accounts.create",
    "update_account": "accounts.update",
    "delete_account": "accounts.delete",
    "journal": "journal.create",
    "update_journal_entry": "journal.update",
    "copy_journal_entry": "journal.create",
    "delete_journal_entry": "journal.delete",
    "post_journal_entry": "journal.post",
    "unpost_journal_entry": "journal.unpost",
    "post_all_journal_drafts": "journal.post",
    "progress_payments": "progress.create",
    "client_progress_payments": "progress.create",
    "update_progress_payment": "progress.update",
    "create_subcontractor_payment": "progress.create",
    "create_retention_release": "progress.create",
    "update_subcontractor_payment": "progress.update",
    "suppliers": "suppliers.create",
    "update_supplier": "suppliers.update",
    "delete_supplier": "suppliers.delete",
    "subcontractors": "subcontractors.create",
    "update_subcontractor": "subcontractors.update",
    "delete_subcontractor": "subcontractors.delete",
    "employees": "hr.create",
    "update_employee": "hr.update",
    "delete_employee": "hr.delete",
    "hr_module": "hr.create",
    "update_attendance": "hr.update",
    "delete_attendance": "hr.delete",
    "update_payroll_slip": "hr.update",
    "update_employee_salary_payment": "hr.update",
    "projects": "projects.create",
    "update_project": "projects.update",
    "delete_project": "projects.delete",
    "update_boq_item": "projects.update",
    "delete_boq_item": "projects.delete",
    "purchase_orders": "purchase_orders.create",
    "update_purchase_order": "purchase_orders.update",
    "inventory": "inventory.create",
    "update_inventory_transaction": "inventory.update",
    "labor": "labor.create",
    "update_labor_entry": "labor.update",
    "equipment": "equipment.create",
    "update_equipment": "equipment.update",
    "estimations": "estimations.create",
    "update_estimation": "estimations.update",
    "update_estimation_status": "estimations.update",
    "client_receipts": "receipts.create",
    "update_client_receipt": "receipts.update",
    "supplier_payments": "supplier_payments.create",
    "update_supplier_payment": "supplier_payments.update",
    "custody_settlements": "custody.create",
    "update_custody_settlement": "custody.update",
    "driver_compensation": "drivers.create",
    "update_driver_compensation": "drivers.update",
    "sales_trips": "sales.create",
    "update_sales_trip": "sales.update",
}


def parse_custom_permissions(user):
    raw = (getattr(user, "permissions_json", None) or "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, list):
        return None
    keys = {str(item).strip() for item in data if str(item).strip()}
    return keys or None


def expand_custom_permissions(keys):
    view_modules = {
        "journal", "progress", "estimations", "projects", "labor", "equipment", "custody", "hr",
    }
    result = {item for item in keys if item in ALLOWED_PERMISSION_KEYS}
    for key in list(result):
        if "." not in key:
            continue
        module = key.split(".", 1)[0]
        if module in view_modules:
            result.add(f"{module}.view")
        else:
            result.add(module)
    return result


def role_default_perms(role):
    if role == ROLE_ADMIN or role == "admin":
        return {ALL}
    if role == "user":
        role = ROLE_DATA_ENTRY
    return set(ROLE_PERMS.get(role, ROLE_PERMS[ROLE_DATA_ENTRY]))


def effective_perms(user):
    if not user:
        return set()
    if getattr(user, "role", None) == ROLE_ADMIN:
        return {ALL}
    custom = parse_custom_permissions(user)
    if custom:
        return expand_custom_permissions(custom)
    return role_default_perms(user.role)


def uses_custom_permissions(user):
    if not user or user.role == ROLE_ADMIN:
        return False
    return parse_custom_permissions(user) is not None


def sanitize_permissions(raw_keys, target_user):
    if target_user and target_user.role == ROLE_ADMIN:
        return sorted(ADMIN_LOCKED_PERMS)
    keys = expand_custom_permissions(raw_keys or [])
    keys.discard(ALL)
    return sorted(keys)


def dump_permissions(keys):
    return json.dumps(sorted(set(keys)), ensure_ascii=False)


def current_perms():
    return effective_perms(getattr(g, "current_user", None))


def user_can(permission):
    perms = current_perms()
    if ALL in perms:
        return True
    if permission in perms:
        return True
    user = getattr(g, "current_user", None)
    if uses_custom_permissions(user):
        return False
    for key, extras in ALIASES.items():
        if key in perms and permission in extras:
            return True
    return False


# أسماء دوال Flask (endpoint) → صلاحية واحدة أو مجموعة (أيّ واحدة تكفي)
ENDPOINT_PERMS = {
    "index": "dashboard",
    "users": ALL,
    "update_user": ALL,
    "delete_user": ALL,
    "update_user_permissions": ALL,
    "activity_log": ALL,
    "accounts": "accounts",
    "update_account": "accounts.update",
    "delete_account": "accounts.delete",
    "account_statement": ("accounts", "reports"),
    "account_lookup": ("accounts", "reports", "journal", "dashboard"),
    "import_workbook": ("accounts", "suppliers"),
    "download_import_template": ("accounts", "suppliers"),
    "download_current_workbook": ("accounts", "suppliers"),
    "desktop_shortcut": "dashboard",
    "download_desktop_url": "dashboard",
    "projects": ("projects", "projects.view", "projects.create"),
    "update_project": "projects.update",
    "delete_project": "projects.delete",
    "project_detail": ("projects", "projects.view"),
    "update_boq_item": "projects.update",
    "delete_boq_item": "projects.delete",
    "progress_payments": ("progress", "progress.create", "progress.view"),
    "update_progress_payment": ("progress", "progress.create", "progress.update"),
    "progress_payment_detail": ("progress", "progress.view", "progress.create"),
    "create_subcontractor_payment": ("progress", "progress.create"),
    "create_retention_release": ("progress", "progress.create"),
    "update_subcontractor_payment": ("progress", "progress.create", "progress.update"),
    "subcontractor_statement": ("subcontractors", "progress.view"),
    "subcontractors": "subcontractors",
    "update_subcontractor": "subcontractors.update",
    "delete_subcontractor": "subcontractors.delete",
    "suppliers": "suppliers",
    "supplier_statement": "suppliers",
    "update_supplier": "suppliers.update",
    "delete_supplier": "suppliers.delete",
    "purchase_orders": "purchase_orders",
    "update_purchase_order": "purchase_orders",
    "inventory": "inventory",
    "update_inventory_transaction": "inventory",
    "inventory_report": "reports",
    "general_ledger_report": "reports",
    "trial_balance_report": "reports",
    "profit_loss_report": "reports",
    "treasury_dynamics_report": "reports",
    "entity_accounts_report": "reports",
    "aging_report": "reports",
    "balance_sheet_report": "reports",
    "cash_flow_report": "reports",
    "custody_settlements": ("custody", "custody.view", "custody.create"),
    "export_custody_settlements": ("custody", "reports"),
    "update_custody_settlement": ("custody", "custody.create"),
    "project_report": "reports",
    "labor": ("labor", "labor.view"),
    "update_labor_entry": "labor",
    "equipment": ("equipment", "equipment.view"),
    "update_equipment": "equipment",
    "driver_compensation": "drivers",
    "driver_compensation_weekly": "drivers",
    "update_driver_compensation": "drivers",
    "sales_trips": ("sales", "sales.view", "sales.create"),
    "update_sales_trip": ("sales", "sales.update"),
    "print_sales_trips": ("sales", "sales.view", "print"),
    "journal": ("journal", "journal.view", "journal.create"),
    "journal_entry_detail": ("journal", "journal.view", "journal.create"),
    "update_journal_entry": ("journal", "journal.create", "journal.update"),
    "copy_journal_entry": ("journal", "journal.create"),
    "reverse_journal_entry": ("journal", "journal.update", "journal.post"),
    "delete_journal_entry": "journal.delete",
    "post_journal_entry": "journal.post",
    "unpost_journal_entry": "journal.unpost",
    "post_all_journal_drafts": "journal.post",
    "estimations": ("estimations", "estimations.view", "estimations.create"),
    "estimation_detail": ("estimations", "estimations.view", "estimations.create"),
    "update_estimation": ("estimations", "estimations.create", "estimations.update"),
    "update_estimation_status": ("estimations", "estimations.create"),
    "active_custody_report": "reports",
    "estimation_profitability_report": "reports",
    "client_progress_payments": ("progress", "progress.create", "progress.view"),
    "client_receipts": ("receipts", "receipts.create"),
    "update_client_receipt": ("receipts", "receipts.update"),
    "supplier_payments": ("supplier_payments", "supplier_payments.create"),
    "update_supplier_payment": ("supplier_payments", "supplier_payments.update"),
    "employees": "hr",
    "update_employee": "hr.update",
    "delete_employee": "hr.delete",
    "hr_module": "hr",
    "print_payslip": ("hr", "hr.view", "print"),
    "update_attendance": "hr",
    "delete_attendance": "hr.delete",
    "update_payroll_slip": "hr",
    "update_employee_salary_payment": "hr",
    "delete_operating_document": (
        "progress.delete", "receipts.delete", "supplier_payments.delete",
        "purchase_orders.delete", "inventory.delete", "custody.delete",
        "labor.delete", "equipment.delete", "estimations.delete",
        "drivers.delete", "hr.delete", "sales.delete", "progress", "receipts",
        "supplier_payments", "purchase_orders", "inventory", "custody",
        "labor", "equipment", "estimations", "drivers", "hr", "sales",
    ),
    "accounting_periods": "periods",
    "close_accounting_period": "periods",
    "reopen_accounting_period": ALL,
    "upload_attachment": "attachments",
    "download_attachment": "attachments",
    "print_document": "print",
    "export_screen": (
        "dashboard", "journal", "accounts", "purchase_orders", "inventory",
        "projects", "estimations", "progress", "receipts", "supplier_payments",
        "suppliers", "subcontractors", "custody", "labor", "hr", "equipment",
        "drivers", "sales", "print",
    ),
    "download_backup": ALL,
    "restore_backup": ALL,
}


def endpoint_allowed(endpoint):
    if not endpoint:
        return True
    required = ENDPOINT_PERMS.get(endpoint)
    if required is None:
        allowed = True
    elif required == ALL:
        allowed = user_can(ALL)
    elif isinstance(required, (list, tuple, set)):
        allowed = any(user_can(item) for item in required)
    else:
        allowed = user_can(required)
    if not allowed:
        return False

    user = getattr(g, "current_user", None)
    if uses_custom_permissions(user) and has_request_context() and request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        write_perm = ENDPOINT_WRITE_PERMS.get(endpoint)
        if write_perm and not user_can(write_perm):
            return False
    return True


def first_allowed_endpoint():
    if user_can(ALL) or user_can("dashboard"):
        return "index"
    for endpoint in LANDING_ENDPOINTS:
        if endpoint_allowed(endpoint):
            return endpoint
    return "change_password"


def require_perm(*permissions):
    def decorator(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            if any(user_can(item) for item in permissions):
                return fn(*args, **kwargs)
            flash("ليست لديك صلاحية لتنفيذ هذا الإجراء", "danger")
            return redirect(url_for(first_allowed_endpoint()))
        return wrapped
    return decorator


def normalize_legacy_role(role):
    if role in ROLE_LABELS:
        return role
    if role == "user":
        return ROLE_DATA_ENTRY
    return ROLE_DATA_ENTRY
