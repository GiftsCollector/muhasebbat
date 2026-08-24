from functools import wraps

from flask import flash, g, redirect, request, url_for

from models import ROLE_ACCOUNTANT, ROLE_ADMIN, ROLE_DATA_ENTRY, ROLE_LABELS, ROLE_PROJECT_MANAGER

ROLE_OPTIONS = [
    (ROLE_ADMIN, ROLE_LABELS[ROLE_ADMIN]),
    (ROLE_ACCOUNTANT, ROLE_LABELS[ROLE_ACCOUNTANT]),
    (ROLE_PROJECT_MANAGER, ROLE_LABELS[ROLE_PROJECT_MANAGER]),
    (ROLE_DATA_ENTRY, ROLE_LABELS[ROLE_DATA_ENTRY]),
]

ALL = "*"

ROLE_PERMS = {
    ROLE_ADMIN: {ALL},
    ROLE_ACCOUNTANT: {
        "dashboard", "journal", "journal.post", "journal.unpost", "accounts",
        "reports", "receipts", "supplier_payments", "custody", "drivers",
        "progress.view", "estimations.view", "suppliers", "subcontractors",
        "purchase_orders", "inventory", "periods", "attachments", "print",
        "labor.view", "equipment.view", "projects.view",
    },
    ROLE_PROJECT_MANAGER: {
        "dashboard", "projects", "estimations", "progress", "subcontractors",
        "suppliers", "reports", "labor", "equipment", "journal.view",
        "purchase_orders", "inventory", "attachments", "print", "custody.view",
        "drivers",
    },
    ROLE_DATA_ENTRY: {
        "dashboard", "progress.create", "estimations.create", "purchase_orders",
        "inventory", "labor", "equipment", "custody.create", "journal.create",
        "receipts.create", "supplier_payments.create", "attachments",
        "projects.view", "print", "drivers",
    },
}

# توسيع الاختصارات: progress يشمل العرض والإنشاء، وهكذا
ALIASES = {
    "progress": {"progress", "progress.view", "progress.create", "progress.delete"},
    "estimations": {"estimations", "estimations.view", "estimations.create"},
    "journal": {"journal", "journal.view", "journal.create", "journal.post"},
    "custody": {"custody", "custody.view", "custody.create"},
    "labor": {"labor", "labor.view"},
    "equipment": {"equipment", "equipment.view"},
    "reports": {"reports", "reports.projects"},
    "receipts": {"receipts", "receipts.create"},
    "supplier_payments": {"supplier_payments", "supplier_payments.create"},
}


def current_perms():
    user = getattr(g, "current_user", None)
    if not user:
        return set()
    if user.role == "user":
        return ROLE_PERMS.get(ROLE_DATA_ENTRY, set())
    return set(ROLE_PERMS.get(user.role, set()))


def user_can(permission):
    perms = current_perms()
    if ALL in perms:
        return True
    if permission in perms:
        return True
    for key, extras in ALIASES.items():
        if key in perms and permission in extras:
            return True
    return False


# أسماء دوال Flask (endpoint) → صلاحية واحدة أو مجموعة (أيّ واحدة تكفي)
ENDPOINT_PERMS = {
    "index": "dashboard",
    "logout": "dashboard",
    "change_password": "dashboard",
    "users": ALL,
    "update_user": ALL,
    "activity_log": ALL,
    "accounts": "accounts",
    "update_account": "accounts",
    "delete_account": "accounts",
    "account_statement": ("accounts", "reports"),
    "projects": ("projects", "projects.view"),
    "update_project": "projects",
    "project_detail": ("projects", "projects.view"),
    "update_boq_item": "projects",
    "progress_payments": ("progress", "progress.create", "progress.view"),
    "update_progress_payment": ("progress", "progress.create"),
    "progress_payment_detail": ("progress", "progress.view", "progress.create"),
    "create_subcontractor_payment": ("progress", "progress.create"),
    "subcontractor_statement": ("subcontractors", "progress.view"),
    "subcontractors": "subcontractors",
    "update_subcontractor": "subcontractors",
    "suppliers": "suppliers",
    "supplier_statement": "suppliers",
    "update_supplier": "suppliers",
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
    "journal": ("journal", "journal.view", "journal.create"),
    "journal_entry_detail": ("journal", "journal.view", "journal.create"),
    "update_journal_entry": ("journal", "journal.create"),
    "copy_journal_entry": ("journal", "journal.create"),
    "reverse_journal_entry": "journal",
    "delete_journal_entry": "journal",
    "post_journal_entry": "journal.post",
    "unpost_journal_entry": "journal.unpost",
    "post_all_journal_drafts": "journal.post",
    "estimations": ("estimations", "estimations.view", "estimations.create"),
    "estimation_detail": ("estimations", "estimations.view", "estimations.create"),
    "update_estimation": ("estimations", "estimations.create"),
    "update_estimation_status": ("estimations", "estimations.create"),
    "active_custody_report": "reports",
    "estimation_profitability_report": "reports",
    "client_progress_payments": ("progress", "progress.create", "progress.view"),
    "client_receipts": ("receipts", "receipts.create"),
    "supplier_payments": ("supplier_payments", "supplier_payments.create"),
    "delete_operating_document": (
        "progress", "progress.create", "receipts", "receipts.create",
        "supplier_payments", "supplier_payments.create", "purchase_orders",
        "inventory", "custody", "custody.create", "labor", "equipment",
        "estimations", "estimations.create", "drivers",
    ),
    "accounting_periods": "periods",
    "close_accounting_period": "periods",
    "reopen_accounting_period": ALL,
    "upload_attachment": "attachments",
    "download_attachment": "attachments",
    "print_document": "print",
    "download_backup": ALL,
    "restore_backup": ALL,
}


def endpoint_allowed(endpoint):
    if not endpoint:
        return True
    required = ENDPOINT_PERMS.get(endpoint)
    if required is None:
        return True
    if required == ALL:
        return user_can(ALL)
    if isinstance(required, (list, tuple, set)):
        return any(user_can(item) for item in required)
    return user_can(required)


def require_perm(*permissions):
    def decorator(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            if any(user_can(item) for item in permissions):
                return fn(*args, **kwargs)
            flash("ليست لديك صلاحية لتنفيذ هذا الإجراء", "danger")
            if request.endpoint and request.endpoint != "index":
                return redirect(url_for("index"))
            return redirect(url_for("login"))
        return wrapped
    return decorator


def normalize_legacy_role(role):
    if role in ROLE_LABELS:
        return role
    if role == "user":
        return ROLE_DATA_ENTRY
    return ROLE_DATA_ENTRY
