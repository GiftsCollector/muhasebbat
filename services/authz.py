from functools import wraps
import json

from flask import flash, g, has_request_context, redirect, request, url_for

from models import ChartOfAccount, ROLE_ACCOUNTANT, ROLE_ADMIN, ROLE_DATA_ENTRY, ROLE_LABELS, ROLE_PROJECT_MANAGER

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
    "receipts": {"receipts", "receipts.view", "receipts.create", "receipts.update", "receipts.delete"},
    "supplier_payments": {"supplier_payments", "supplier_payments.view", "supplier_payments.create", "supplier_payments.update", "supplier_payments.delete"},
    "hr": {"hr", "hr.view", "hr.create", "hr.update", "hr.delete"},
    "accounts": {"accounts", "accounts.view", "accounts.create", "accounts.update", "accounts.delete"},
    "suppliers": {"suppliers", "suppliers.view", "suppliers.create", "suppliers.update", "suppliers.delete"},
    "subcontractors": {"subcontractors", "subcontractors.view", "subcontractors.create", "subcontractors.update", "subcontractors.delete"},
    "projects": {"projects", "projects.view", "projects.create", "projects.update", "projects.delete"},
    "purchase_orders": {"purchase_orders", "purchase_orders.view", "purchase_orders.create", "purchase_orders.update", "purchase_orders.delete"},
    "inventory": {"inventory", "inventory.view", "inventory.create", "inventory.update", "inventory.delete"},
    "drivers": {"drivers", "drivers.view", "drivers.create", "drivers.update", "drivers.delete"},
    "sales": {"sales", "sales.view", "sales.create", "sales.update", "sales.delete"},
}


def bundle_role_perms(keys):
    result = set(keys)
    for key in list(keys):
        extras = ALIASES.get(key)
        if extras:
            result.update(extras)
    return result


ROLE_PERMS = {
    role: (perms if ALL in perms else bundle_role_perms(perms))
    for role, perms in ROLE_PERMS.items()
}

PERMISSION_GROUPS = [
    {
        "id": "access",
        "label": "الوصول",
        "permissions": [
            ("dashboard", "الشاشة الرئيسية"),
            ("print", "الطباعة"),
            ("attachments", "المرفقات"),
            ("periods", "إقفال الفترات"),
            ("treasury.main", "استخدام الخزنة الرئيسية"),
        ],
    },
    {
        "id": "accounts_reports",
        "label": "الحسابات والتقارير",
        "permissions": [
            ("accounts.view", "عرض الحسابات"),
            ("accounts.create", "إضافة حسابات"),
            ("accounts.update", "تعديل الحسابات"),
            ("accounts.delete", "حذف الحسابات"),
            ("reports", "التقارير"),
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
        "label": "المستخلصات والمقايسات",
        "permissions": [
            ("progress.view", "عرض المستخلصات"),
            ("progress.create", "إضافة مستخلصات"),
            ("progress.update", "تعديل المستخلصات"),
            ("progress.delete", "حذف المستخلصات"),
            ("estimations.view", "عرض المقايسات"),
            ("estimations.create", "إضافة مقايسات"),
            ("estimations.update", "تعديل المقايسات"),
            ("estimations.delete", "حذف المقايسات"),
        ],
    },
    {
        "id": "parties",
        "label": "الموردون والعملاء",
        "permissions": [
            ("suppliers.view", "عرض الموردين"),
            ("suppliers.create", "إضافة موردين"),
            ("suppliers.update", "تعديل الموردين"),
            ("suppliers.delete", "حذف الموردين"),
            ("subcontractors.view", "عرض مقاولي الباطن"),
            ("subcontractors.create", "إضافة مقاولين"),
            ("subcontractors.update", "تعديل المقاولين"),
            ("subcontractors.delete", "حذف المقاولين"),
            ("receipts.view", "عرض تحصيل العملاء"),
            ("receipts.create", "إضافة تحصيل"),
            ("receipts.update", "تعديل التحصيل"),
            ("receipts.delete", "حذف التحصيل"),
            ("supplier_payments.view", "عرض سداد الموردين"),
            ("supplier_payments.create", "إضافة سداد"),
            ("supplier_payments.update", "تعديل السداد"),
            ("supplier_payments.delete", "حذف السداد"),
        ],
    },
    {
        "id": "hr",
        "label": "الموظفون والمرتبات",
        "permissions": [
            ("hr.view", "عرض الموظفين والمرتبات"),
            ("hr.create", "إضافة موظفين ومرتبات"),
            ("hr.update", "تعديل الموظفين والمرتبات"),
            ("hr.delete", "حذف الموظفين والمرتبات"),
        ],
    },
    {
        "id": "operations",
        "label": "التشغيل والمخازن",
        "permissions": [
            ("projects.view", "عرض المشاريع"),
            ("projects.create", "إضافة مشاريع"),
            ("projects.update", "تعديل المشاريع"),
            ("projects.delete", "حذف المشاريع"),
            ("purchase_orders.view", "عرض أوامر الشراء"),
            ("purchase_orders.create", "إضافة أوامر شراء"),
            ("purchase_orders.update", "تعديل أوامر الشراء"),
            ("purchase_orders.delete", "حذف أوامر الشراء"),
            ("inventory.view", "عرض المخزون"),
            ("inventory.create", "إضافة حركة مخزون"),
            ("inventory.update", "تعديل حركة المخزون"),
            ("inventory.delete", "حذف حركة المخزون"),
            ("labor.view", "عرض العمالة"),
            ("labor.create", "إضافة عمالة"),
            ("labor.update", "تعديل العمالة"),
            ("labor.delete", "حذف العمالة"),
            ("equipment.view", "عرض المعدات"),
            ("equipment.create", "إضافة معدات"),
            ("equipment.update", "تعديل المعدات"),
            ("equipment.delete", "حذف المعدات"),
            ("custody.view", "عرض تسوية العهد"),
            ("custody.create", "إضافة تسوية عهد"),
            ("custody.update", "تعديل تسوية العهد"),
            ("custody.delete", "حذف تسوية العهد"),
            ("drivers.view", "عرض محاسبة السواقين"),
            ("drivers.create", "إضافة محاسبة سائق"),
            ("drivers.update", "تعديل محاسبة السائق"),
            ("drivers.delete", "حذف محاسبة السائق"),
            ("sales.view", "عرض المبيعات"),
            ("sales.create", "إضافة مبيعات"),
            ("sales.update", "تعديل المبيعات"),
            ("sales.delete", "حذف المبيعات"),
        ],
    },
]

ALLOWED_PERMISSION_KEYS = {key for group in PERMISSION_GROUPS for key, _label in group["permissions"]}
ALLOWED_PERMISSION_KEYS.update({"dashboard", ALL, "accounts", "hr", "journal", "progress", "estimations", "suppliers", "subcontractors", "receipts", "supplier_payments", "projects", "purchase_orders", "inventory", "labor", "equipment", "custody", "drivers", "sales", "treasury.main"})

# الشاشات اللي كانت علامة واحدة: العلامة القديمة = الشاشة كاملة
SINGLE_TOGGLE_MODULES = {
    "inventory", "labor", "equipment", "custody", "drivers", "sales",
    "purchase_orders", "receipts", "supplier_payments",
}
# المقايسات كانت «إضافة/تعديل» في علامة واحدة
CREATE_IMPLIES_UPDATE_MODULES = {"estimations"}

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


def _permissions_payload(user):
    raw = (getattr(user, "permissions_json", None) or "").strip()
    if not raw:
        return {"perms": None, "hidden_treasuries": []}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {"perms": None, "hidden_treasuries": []}
    if isinstance(data, list):
        keys = {str(item).strip() for item in data if str(item).strip()}
        return {"perms": keys or None, "hidden_treasuries": []}
    if isinstance(data, dict):
        items = data.get("perms") or data.get("permissions") or []
        keys = {str(item).strip() for item in items if str(item).strip()}
        hidden = []
        for item in data.get("hidden_treasuries") or []:
            try:
                hidden.append(int(item))
            except (TypeError, ValueError):
                continue
        return {"perms": keys or None, "hidden_treasuries": hidden}
    return {"perms": None, "hidden_treasuries": []}


def parse_custom_permissions(user):
    return _permissions_payload(user)["perms"]


def _main_treasury_ids():
    ids = set()
    rows = ChartOfAccount.query.filter(
        (ChartOfAccount.code == "TRS-MAIN") | (ChartOfAccount.name == "الخزنة الرئيسية")
    ).all()
    for account in rows:
        code = (account.code or "").strip().upper()
        if code.startswith("CAT-"):
            continue
        ids.add(account.id)
    return ids


def hidden_treasury_ids(user=None):
    current = user if user is not None else (g.current_user if has_request_context() else None)
    if not current or getattr(current, "role", None) == ROLE_ADMIN:
        return set()
    hidden = set(_permissions_payload(current)["hidden_treasuries"])
    perms = effective_perms(current)
    if ALL not in perms and "treasury.main" not in perms:
        hidden.update(_main_treasury_ids())
    return hidden


def user_can_see_account(account_or_id, user=None):
    if account_or_id is None:
        return True
    account_id = getattr(account_or_id, "id", account_or_id)
    try:
        account_id = int(account_id)
    except (TypeError, ValueError):
        return True
    return account_id not in hidden_treasury_ids(user)


def filter_hidden_treasuries(accounts, user=None):
    hidden = hidden_treasury_ids(user)
    if not hidden:
        return list(accounts or [])
    return [account for account in (accounts or []) if getattr(account, "id", None) not in hidden]


def journal_is_visible(entry, user=None):
    if not entry:
        return False
    hidden = hidden_treasury_ids(user)
    if not hidden:
        return True
    return entry.debit_account_id not in hidden and entry.credit_account_id not in hidden


def filter_visible_journals(entries, user=None):
    hidden = hidden_treasury_ids(user)
    if not hidden:
        return list(entries or [])
    return [entry for entry in (entries or []) if journal_is_visible(entry, user)]


def touches_hidden_treasury(*account_ids):
    hidden = hidden_treasury_ids()
    if not hidden:
        return False
    for raw in account_ids:
        try:
            if int(raw) in hidden:
                return True
        except (TypeError, ValueError):
            continue
    return False


def expand_custom_permissions(keys):
    view_modules = {
        "journal", "progress", "estimations", "projects", "labor", "equipment", "custody", "hr",
        "accounts", "suppliers", "subcontractors", "receipts", "supplier_payments",
        "purchase_orders", "inventory", "drivers", "sales",
    }
    incoming = {item for item in (keys or []) if item}
    result = {item for item in incoming if item in ALLOWED_PERMISSION_KEYS}

    for key in list(result):
        if "." not in key:
            continue
        module = key.split(".", 1)[0]
        result.add(module)
        if module in view_modules:
            result.add(f"{module}.view")

    for module, extras in ALIASES.items():
        dotted = {item for item in extras if "." in item}
        present_children = result & dotted
        has_parent = module in result or module in incoming
        if module in SINGLE_TOGGLE_MODULES and has_parent and not present_children:
            result.update(extras)
        elif module in SINGLE_TOGGLE_MODULES and present_children == {f"{module}.create"}:
            result.update(extras)
        elif module in CREATE_IMPLIES_UPDATE_MODULES and f"{module}.create" in result:
            result.add(f"{module}.update")
            result.add(module)
            result.add(f"{module}.view")
        elif has_parent and not present_children:
            result.add(f"{module}.view")

    result = {item for item in result if item in ALLOWED_PERMISSION_KEYS or item in {module for module, _extras in ALIASES.items()}}
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
    base = custom if custom else role_default_perms(user.role)
    if ALL in base:
        return {ALL}
    return expand_custom_permissions(base)


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


def dump_permissions(keys, hidden_treasuries=None):
    payload = {
        "perms": sorted(set(keys)),
        "hidden_treasuries": sorted({int(item) for item in (hidden_treasuries or [])}),
    }
    return json.dumps(payload, ensure_ascii=False)


def current_perms():
    return effective_perms(getattr(g, "current_user", None))


def user_can(permission):
    perms = current_perms()
    if ALL in perms:
        return True
    if permission in perms:
        return True
    if permission and "." not in (permission or ""):
        return f"{permission}.view" in perms or f"{permission}.create" in perms
    return False


# أسماء دوال Flask (endpoint) → صلاحية واحدة أو مجموعة (أيّ واحدة تكفي)
ENDPOINT_PERMS = {
    "index": "dashboard",
    "alerts_inbox": (
        "dashboard", "hr", "hr.view", "accounts", "reports", "journal",
        "projects", "projects.view", "purchase_orders",
    ),
    "dismiss_user_alert": (
        "dashboard", "hr", "hr.view", "accounts", "reports", "journal",
        "projects", "projects.view", "purchase_orders",
    ),
    "users": ALL,
    "update_user": ALL,
    "delete_user": ALL,
    "update_user_permissions": ALL,
    "activity_log": ALL,
    "accounts": ("accounts", "accounts.view"),
    "update_account": ("accounts.update", "accounts"),
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
    "subcontractors": ("subcontractors", "subcontractors.view", "subcontractors.create"),
    "update_subcontractor": "subcontractors.update",
    "delete_subcontractor": "subcontractors.delete",
    "suppliers": ("suppliers", "suppliers.view", "suppliers.create"),
    "supplier_statement": ("suppliers", "suppliers.view"),
    "update_supplier": "suppliers.update",
    "delete_supplier": "suppliers.delete",
    "purchase_orders": ("purchase_orders", "purchase_orders.view", "purchase_orders.create"),
    "print_purchase_orders": ("purchase_orders", "purchase_orders.view", "print"),
    "update_purchase_order": ("purchase_orders", "purchase_orders.update", "purchase_orders.create"),
    "inventory": ("inventory", "inventory.view", "inventory.create"),
    "update_inventory_transaction": ("inventory", "inventory.update", "inventory.create"),
    "inventory_report": "reports",
    "print_setup": ("print", "reports", "sales.view", "drivers.view", "journal.view", "inventory.view", "progress.view", "purchase_orders.view", "accounts", "labor.view", "custody.view", "receipts.view", "supplier_payments.view"),
    "print_screen_report": ("print", "reports", "sales.view", "drivers.view", "journal.view", "inventory.view", "progress.view", "purchase_orders.view", "accounts", "labor.view", "custody.view", "receipts.view", "supplier_payments.view"),
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
    "labor": ("labor", "labor.view", "labor.create"),
    "update_labor_entry": ("labor", "labor.update", "labor.create"),
    "equipment": ("equipment", "equipment.view", "equipment.create"),
    "update_equipment": ("equipment", "equipment.update", "equipment.create"),
    "driver_compensation": ("drivers", "drivers.view", "drivers.create"),
    "driver_compensation_weekly": ("drivers", "drivers.view", "drivers.create"),
    "update_driver_compensation": ("drivers", "drivers.update", "drivers.create"),
    "print_driver_compensation": ("drivers", "drivers.view", "print"),
    "print_driver_sheet": ("drivers", "drivers.view", "print"),
    "sales_trips": ("sales", "sales.view", "sales.create"),
    "update_sales_trip": ("sales", "sales.update", "sales.create"),
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
    "client_receipts": ("receipts", "receipts.view", "receipts.create"),
    "update_client_receipt": ("receipts", "receipts.update", "receipts.create"),
    "supplier_payments": ("supplier_payments", "supplier_payments.view", "supplier_payments.create"),
    "update_supplier_payment": ("supplier_payments", "supplier_payments.update", "supplier_payments.create"),
    "employees": ("hr", "hr.view"),
    "update_employee": "hr.update",
    "delete_employee": "hr.delete",
    "hr_module": ("hr", "hr.view"),
    "employee_report": ("hr", "hr.view"),
    "print_employee_report": ("hr", "hr.view", "print"),
    "print_payslip": ("hr", "hr.view", "print"),
    "print_payroll_sheet": ("hr", "hr.view", "print"),
    "print_attendance_sheet": ("hr", "hr.view", "print"),
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
            if write_perm in ALLOWED_PERMISSION_KEYS:
                return False
            module = write_perm.split(".", 1)[0] if "." in write_perm else write_perm
            if not user_can(module) and not user_can(f"{module}.create"):
                return False
    return True


def _normalize_person_name(value):
    text = (value or "")
    for src, dst in (("ى", "ي"), ("أ", "ا"), ("إ", "ا"), ("آ", "ا")):
        text = text.replace(src, dst)
    return text.casefold()


def repair_legacy_custom_permissions():
    """يثبّت الصلاحيات القديمة: العلامة الواحدة تفضل فاتحة الشاشة، ومصطفى يفضل شايف أوامر الشراء."""
    from models import User, db

    changed = False
    for user in User.query.all():
        if getattr(user, "role", None) == ROLE_ADMIN:
            continue
        custom = parse_custom_permissions(user)
        payload = _permissions_payload(user)
        blob = _normalize_person_name(f"{user.full_name or ''} {user.username or ''}")
        if custom is None:
            if "مصطفي" not in blob and "mustafa" not in blob:
                continue
            custom = set(role_default_perms(user.role))
        needed = set(custom)
        if "مصطفي" in blob or "mustafa" in blob:
            needed.update({"purchase_orders", "purchase_orders.view", "purchase_orders.create", "purchase_orders.update"})
        new_keys = sanitize_permissions(needed, user)
        new_json = dump_permissions(new_keys, payload["hidden_treasuries"])
        if (user.permissions_json or "") != new_json:
            user.permissions_json = new_json
            changed = True
    if changed:
        db.session.commit()


def repair_missing_purchase_order_permissions():
    repair_legacy_custom_permissions()


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
