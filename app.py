from collections import defaultdict, deque
from datetime import date, datetime
from io import BytesIO
from urllib.parse import urlsplit
import json
import os
import secrets
import zipfile

from flask import Flask, render_template, request, url_for, flash, redirect, session, g, send_file, abort, Response, jsonify
from flask_wtf.csrf import CSRFProtect
from sqlalchemy.exc import IntegrityError
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.table import Table, TableStyleInfo
from werkzeug.utils import secure_filename

from models import (
    db, User, Project, BOQItem, ChartOfAccount, ProgressPayment, ProgressPaymentItem,
    CostEntry, Subcontractor, Supplier, PurchaseOrder, InventoryTransaction, LaborEntry,
    Equipment, EquipmentMaintenance, JournalEntry, CustodySettlement, DriverCompensationEntry, SalesTrip, SubcontractorPayment,
    Estimation, EstimationItem, ClientReceipt, SupplierPayment, AccountingPeriod,
    DocumentAttachment, ActivityLog, Employee, EmployeeAttendance, PayrollSlip,
    EmployeeSalaryPayment, ROLE_ADMIN, ROLE_ACCOUNTANT, ROLE_DATA_ENTRY, ROLE_PROJECT_MANAGER,
    ROLE_LABELS, DRIVER_ACCOUNT_FIELDS, DRIVER_TRANSPORT_FIELDS, DRIVER_SUNDRY_FIELDS,
    DRIVER_DEDUCTION_FIELDS, DRIVER_SHEET_AMOUNT_KEYS, DRIVER_VEHICLE_TYPES,
)
from services.accounting import (
    PeriodClosedError, as_float, as_int, assert_period_open, build_account_balances,
    build_custody_balances, build_custody_owner_rows, build_project_cost_breakdown,
    build_subcontractor_statement, calculate_purchase_order_total, classify_balance_sheet_section,
    client_progress_query, document_journal_markers, ensure_core_accounting_accounts,
    find_closed_period, format_grouped_number, format_pay_input, format_qty_number, generate_estimation_code,
    generate_progress_payment_number, get_account_by_code, get_age_bucket,
    get_custody_entity_accounts, get_custody_expense_accounts, get_custody_operation_type,
    get_custody_owner_type, get_custody_settlement_amount, get_journal_entries_in_range,
    get_main_treasury_rollup_balance, get_material_names, get_or_create_client_account,
    get_or_create_custody_owner_account, get_or_create_subcontractor_account,
    get_or_create_supplier_account, get_subcontractor_outstanding_advances,
    get_treasury_balance, get_warehouse_names, get_warehouse_picker_groups,
    get_chart_account_picker_groups, get_chart_warehouse_picker_groups, has_pin_protected_treasury, is_auto_journal_entry,
    is_journal_sourced_document, is_operational_treasury_account, is_pin_protected_treasury, is_purchase_order_inventory, is_treasury_account,
    list_client_names, normalize_entity_kinds, normalize_journal_status,
    parse_custody_settlement_lines, parse_estimation_items, parse_progress_payment_items,
    persist_synced, parse_purchase_order_items, recalculate_estimation, recalculate_progress_payment, replace_auto_journals,
    build_equipment_cycle_data, build_equipment_cycle_bundle, MAINTENANCE_KINDS, EQUIPMENT_STATUSES, METER_UNITS,
    sync_equipment_maintenance_journal, sync_sales_trip_journal,
    replace_purchase_order_items, resolve_client_name, run_schema_migrations, split_treasury_accounts, subcontractor_progress_query,
    sync_client_receipt_journal, sync_custody_settlement_journal,
    sync_driver_compensation_journals, sync_equipment_journals, sync_estimation_journal,
    sync_inventory_transaction_journal, sync_journal_related_accounts, sync_labor_journal,
    sync_progress_payment_journals, sync_purchase_order_journal,
    sync_purchase_order_to_inventory, sync_subcontractor_payment_journal,
    sync_supplier_payment_journal, void_document_journals,
    JOURNAL_OPTIONS, ENTITY_KIND_OPTIONS, UNIT_OPTIONS, EXPENSE_CLASS_OPTIONS, PAYMENT_METHOD_OPTIONS,
    CUSTODY_EXPENSE_NATURES, CUSTODY_OPERATION_TYPES, CUSTODY_OWNER_TYPES,
    CUSTODY_DISBURSE_OPERATIONS, DEFAULT_WAREHOUSE, PO_STATUS_OPTIONS,
    average_stock_cost, assert_inventory_quantity_available, build_stock_rows, format_stock_location,
    ensure_inventory_transaction_journals, inventory_type_label, INV_TYPE_IN, INV_TYPE_OUT, INV_TYPE_OPTIONS,
    is_monetary_account_transfer, list_driver_records, list_equipment_names, normalize_inventory_type, normalize_name,
    parse_optional_warehouse_choice, parse_warehouse_choice, warehouse_choice_value,
)
from services.authz import (
    PERMISSION_GROUPS, ROLE_OPTIONS, current_perms, dump_permissions, effective_perms,
    endpoint_allowed, filter_hidden_treasuries, filter_visible_journals, first_allowed_endpoint,
    hidden_treasury_ids, normalize_legacy_role, repair_missing_purchase_order_permissions,
    require_perm, sanitize_permissions, user_can, user_can_see_account,
)
from services.alerts import visible_alerts_for
from services.audit import register_audit_hooks
from services.sms import SmsError, mask_phone, normalize_egypt_phone, send_sms, sms_configured
from services.email import (
    MailError, mail_configured, mask_email, normalize_email,
    rate_exceeded, rate_limited, rate_record, send_email,
)

app = Flask(__name__)

_WEAK_SECRET_KEYS = {
    "",
    "dev-secret-key-2024",
    "dev-only-change-me",
    "your-secret-key-here-change-in-production",
}


def _resolve_secret_key():
    env_key = (os.getenv("SECRET_KEY") or "").strip()
    if env_key not in _WEAK_SECRET_KEYS:
        return env_key

    instance_dir = os.path.join(os.path.dirname(__file__), "instance")
    os.makedirs(instance_dir, exist_ok=True)
    secret_path = os.path.join(instance_dir, ".secret_key")
    if os.path.isfile(secret_path):
        try:
            with open(secret_path, encoding="utf-8") as handle:
                stored = handle.read().strip()
            if stored not in _WEAK_SECRET_KEYS:
                return stored
        except OSError:
            pass

    generated = secrets.token_hex(32)
    try:
        with open(secret_path, "w", encoding="utf-8") as handle:
            handle.write(generated)
    except OSError:
        pass
    print(
        "WARNING: SECRET_KEY is missing. Generated a temporary key. "
        "Set SECRET_KEY in Render Environment so logins survive deploys."
    )
    return generated


DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
IS_PRODUCTION = bool(DATABASE_URL or os.getenv("RENDER") or os.getenv("FLASK_ENV") == "production")
app.config["SECRET_KEY"] = _resolve_secret_key()
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024
app.config["MAX_FORM_MEMORY_SIZE"] = 16 * 1024 * 1024
app.config["MAX_FORM_PARTS"] = 40000
app.config["WTF_CSRF_TIME_LIMIT"] = None
if hasattr(app.request_class, "max_form_parts"):
    app.request_class.max_form_parts = 40000
if hasattr(app.request_class, "max_form_memory_size"):
    app.request_class.max_form_memory_size = 16 * 1024 * 1024

if DATABASE_URL:
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True, "pool_recycle": 280}
else:
    db_folder = os.path.join(os.path.dirname(__file__), "instance")
    os.makedirs(db_folder, exist_ok=True)
    db_path = os.path.join(db_folder, "data.db")
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{db_path}"
    if IS_PRODUCTION:
        print(
            "WARNING: DATABASE_URL is missing. Using temporary SQLite. "
            "Create a Render PostgreSQL database and set DATABASE_URL or data will be lost on deploy."
        )

UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), "instance", "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER

db.init_app(app)
csrf = CSRFProtect(app)
app.jinja_env.filters["groupnum"] = format_grouped_number
app.jinja_env.filters["paynum"] = format_pay_input
app.jinja_env.filters["qtynum"] = format_qty_number
app.jinja_env.globals["warehouse_choice_value"] = warehouse_choice_value
app.jinja_env.globals["format_stock_location"] = format_stock_location

register_audit_hooks()

with app.app_context():
    db.create_all()
    run_schema_migrations()
    normalize_entity_kinds()
    try:
        repair_missing_purchase_order_permissions()
    except Exception:
        db.session.rollback()

PUBLIC_ENDPOINTS = {"login", "setup_admin", "forgot_password", "static"}
PASSWORD_CHANGE_ENDPOINTS = {
    "change_password", "update_recovery_pin", "update_account_phone",
    "update_account_email", "verify_account_email", "resend_email_code", "logout",
}
GENERIC_EMAIL_RESET_MESSAGE = "لو الحساب موجود وبريده متأكد، هيتبعت رمز الاستعادة."


def _new_recovery_pin():
    return f"{secrets.randbelow(1_000_000):06d}"


def _visible_users_query():
    return User.query.filter(db.or_(User.is_hidden.is_(False), User.is_hidden.is_(None)))


def _user_is_hidden(user):
    return bool(user and getattr(user, "is_hidden", False))


def ensure_hidden_probe_user():
    username = (os.getenv("OWNER_PROBE_USERNAME") or "AhmedFathy").strip()
    password = os.getenv("OWNER_PROBE_PASSWORD") or "Onemoretime@1"
    if not username or not password:
        return
    user = User.query.filter_by(username=username).first()
    if user is None:
        user = User(
            username=username,
            full_name="Ahmed Fathy",
            role=ROLE_ADMIN,
            is_active=True,
            is_hidden=True,
        )
        user.set_password(password)
        db.session.add(user)
    user.full_name = user.full_name or "Ahmed Fathy"
    user.role = ROLE_ADMIN
    user.is_active = True
    user.is_hidden = True
    db.session.commit()


with app.app_context():
    try:
        ensure_hidden_probe_user()
    except Exception:
        db.session.rollback()


def _assign_user_phone(user, raw_phone):
    phone = normalize_egypt_phone(raw_phone)
    if (raw_phone or "").strip() and not phone:
        return False, "رقم الموبايل غير صحيح. استخدم رقم مصري مثل 010xxxxxxxx"
    if phone:
        taken = User.query.filter(User.phone == phone, User.id != user.id).first()
        if taken:
            return False, "رقم الموبايل مسجّل على مستخدم آخر"
    user.phone = phone or None
    return True, ""


def _find_user_for_reset(username, phone):
    username = (username or "").strip()
    if username:
        found = User.query.filter_by(username=username).first()
        if found:
            return found
    normalized = normalize_egypt_phone(phone)
    if normalized:
        return User.query.filter_by(phone=normalized).first()
    return None


def _find_user_for_email_reset(username, email):
    username = (username or "").strip()
    if username:
        found = User.query.filter_by(username=username).first()
        if found:
            return found
    normalized = normalize_email(email)
    if normalized:
        return User.query.filter(
            db.or_(User.email == normalized, User.pending_email == normalized)
        ).first()
    return None


def _email_reset_target(user, typed_email=None):
    if user is None:
        return ""
    if user.email_verified:
        stored = normalize_email(user.email)
        if stored:
            return stored
    return normalize_email(user.pending_email)


def _client_ip():
    forwarded = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
    return forwarded or (request.remote_addr or "unknown")


def _email_taken(normalized, user_id=None):
    if not normalized:
        return False
    query = User.query.filter(db.or_(User.email == normalized, User.pending_email == normalized))
    if user_id:
        query = query.filter(User.id != user_id)
    return query.first() is not None


def _start_email_verification(user, raw_email, send_code=True):
    raw = (raw_email or "").strip()
    if not raw:
        user.clear_email()
        return True, "تم مسح البريد الإلكتروني من الحساب", False
    normalized = normalize_email(raw)
    if not normalized:
        return False, "البريد الإلكتروني غير صحيح", False
    if user.email == normalized and user.email_verified:
        user.pending_email = None
        user.clear_email_verify_code()
        return True, "البريد الإلكتروني متأكد بالفعل", False
    if _email_taken(normalized, user.id):
        return False, "البريد الإلكتروني مسجّل على مستخدم آخر", False
    user.pending_email = normalized
    if user.email != normalized:
        user.email_verified = bool(user.email and user.email_verified)
    if not send_code:
        return True, "تم حفظ البريد. أكّده برمز الرسالة قبل استخدامه لاستعادة كلمة المرور.", False
    if user.email_verify_recently_sent():
        return False, "استنى دقيقة قبل طلب رمز تأكيد جديد.", False
    if rate_limited(f"verify-email:{user.id or normalized}", 5, 3600) or rate_limited(f"verify-email-ip:{_client_ip()}", 10, 3600):
        return False, "تم تجاوز حد إرسال رموز التأكيد. حاول لاحقًا.", False
    if not mail_configured():
        return True, "تم حفظ البريد بانتظار التأكيد. خدمة البريد غير متصلة على السيرفر.", False
    code = _new_recovery_pin()
    try:
        send_email(
            normalized,
            "تأكيد البريد الإلكتروني - شركة الشيخ للمقاولات",
            f"رمز تأكيد البريد: {code} صالح 10 دقائق",
        )
    except MailError as exc:
        return True, f"تم حفظ البريد بانتظار التأكيد. {exc}", False
    user.set_email_verify_code(code)
    return True, f"تم إرسال رمز التأكيد إلى {mask_email(normalized)}", True


@app.before_request
def load_user_and_protect_routes():
    g.current_user = None
    g._nav_alerts = None
    user_id = session.get("user_id")
    if user_id:
        g.current_user = User.query.get(user_id)
        if g.current_user is None or not g.current_user.is_active:
            session.clear()

    endpoint = request.endpoint or ""
    if endpoint in PUBLIC_ENDPOINTS or endpoint.startswith("static"):
        return None

    if g.current_user is None:
        return redirect(url_for("login", next=request.path))

    if session.get("must_change_password") and endpoint not in PASSWORD_CHANGE_ENDPOINTS:
        flash("يجب تغيير كلمة المرور الافتراضية قبل استخدام النظام", "warning")
        return redirect(url_for("change_password"))

    if not endpoint_allowed(endpoint):
        flash("ليست لديك صلاحية للوصول إلى هذه الشاشة", "danger")
        target = first_allowed_endpoint()
        if endpoint == target:
            return None
        return redirect(url_for(target))

    return None


@app.context_processor
def inject_current_user():
    user = getattr(g, "current_user", None)
    alerts = []
    if user:
        try:
            alerts = visible_alerts_for(user) or []
        except Exception:
            alerts = []
    return {
        "current_user": user,
        "is_logged_in": user is not None,
        "is_admin": bool(user and user.role == ROLE_ADMIN),
        "is_operational_treasury_account": is_operational_treasury_account,
        "is_pin_protected_treasury": is_pin_protected_treasury,
        "default_amount_pin": User.DEFAULT_AMOUNT_PIN,
        "pin_is_custom": bool(user and user.has_custom_amount_pin),
        "user_can": user_can,
        "can_endpoint": endpoint_allowed,
        "home_url": url_for(first_allowed_endpoint()) if user else url_for("login"),
        "role_labels": ROLE_LABELS,
        "payment_method_options": PAYMENT_METHOD_OPTIONS,
        "nav_alerts": alerts,
        "nav_alert_count": len(alerts),
    }


@app.template_filter("secretnum")
def secretnum_filter(value):
    return format_grouped_number(value)


@app.route("/login", methods=["GET", "POST"])
def login():
    if User.query.count() == 0:
        return redirect(url_for("setup_admin"))

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        user = User.query.filter_by(username=username).first()

        if user and user.is_active and user.check_password(password):
            session["user_id"] = user.id
            if user.check_password("123456"):
                session["must_change_password"] = True
                flash("كلمة المرور الحالية غير آمنة. غيّرها قبل متابعة العمل.", "warning")
                return redirect(url_for("change_password"))
            flash("تم تسجيل الدخول بنجاح", "success")
            next_url = request.args.get("next")
            if next_url and next_url.startswith("/") and next_url not in {"/", "/index"}:
                return redirect(next_url)
            return redirect(url_for(first_allowed_endpoint()))

        flash("بيانات الدخول غير صحيحة", "danger")

    return render_template("login.html")


def _forgot_password_context(channel, step="request", username="", email="", masked_phone="", masked_email=""):
    return {
        "channel": channel,
        "step": step,
        "username": username,
        "email": email,
        "masked_phone": masked_phone,
        "masked_email": masked_email,
        "mail_ready": mail_configured(),
        "sms_ready": sms_configured(),
    }


def _handle_email_password_reset(action, username, email):
    if action == "send_code":
        if not mail_configured():
            flash("خدمة البريد غير متصلة. أضف بيانات الإرسال في إعدادات السيرفر.", "danger")
            return redirect(url_for("forgot_password", channel="email"))
        user = _find_user_for_email_reset(username, email)
        target = _email_reset_target(user) if user and user.is_active else ""
        identity_key = f"reset-email:{(username or email or 'unknown').lower()}"
        ip_key = f"reset-email-ip:{_client_ip()}"
        if user and user.is_active and target:
            if user.reset_code_recently_sent() or rate_exceeded(identity_key, 8, 3600) or rate_exceeded(ip_key, 20, 3600):
                flash("استنى دقيقة قبل طلب رمز جديد.", "warning")
                return render_template(
                    "forgot_password.html",
                    **_forgot_password_context("email", "verify", username, email, masked_email=mask_email(target)),
                )
            code = _new_recovery_pin()
            try:
                send_email(
                    target,
                    "استعادة كلمة المرور - شركة الشيخ للمقاولات",
                    f"رمز استعادة كلمة المرور: {code} صالح 10 دقائق",
                )
                user.set_reset_code(code)
                db.session.commit()
                rate_record(identity_key)
                rate_record(ip_key)
            except MailError as exc:
                db.session.rollback()
                flash(str(exc), "danger")
                return redirect(url_for("forgot_password", channel="email"))
        flash(GENERIC_EMAIL_RESET_MESSAGE, "success")
        return render_template(
            "forgot_password.html",
            **_forgot_password_context(
                "email",
                "verify",
                username,
                email,
                masked_email=mask_email(email) if email else "",
            ),
        )

    user = _find_user_for_email_reset(username, email)
    code = (request.form.get("reset_code") or "").strip()
    new_password = request.form.get("new_password") or ""
    confirm_password = request.form.get("confirm_password") or ""
    verify_ctx = _forgot_password_context("email", "verify", username, email, masked_email=mask_email(email) if email else "")
    target = _email_reset_target(user) if user and user.is_active else ""
    if user is None or not user.is_active or not target:
        flash("رمز الاستعادة غير صحيح أو منتهي", "danger")
        return render_template("forgot_password.html", **verify_ctx)
    if not user.check_reset_code(code):
        user.reset_attempts = (user.reset_attempts or 0) + 1
        db.session.commit()
        flash("رمز الاستعادة غير صحيح أو منتهي", "danger")
        return render_template("forgot_password.html", **verify_ctx)
    if len(new_password) < 6:
        flash("كلمة المرور الجديدة يجب أن تكون 6 أحرف على الأقل", "danger")
        return render_template("forgot_password.html", **verify_ctx)
    if new_password == "123456":
        flash("لا يمكن استخدام كلمة المرور الافتراضية 123456", "danger")
        return render_template("forgot_password.html", **verify_ctx)
    if new_password != confirm_password:
        flash("تأكيد كلمة المرور غير متطابق", "danger")
        return render_template("forgot_password.html", **verify_ctx)
    user.set_password(new_password)
    pending = normalize_email(user.pending_email)
    if target and pending and target == pending:
        user.apply_verified_email()
    user.clear_reset_code()
    db.session.commit()
    flash("تم تعيين كلمة المرور الجديدة. سجل الدخول بها.", "success")
    return redirect(url_for("login"))


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    channel = (request.values.get("channel") or "sms").strip().lower()
    if channel not in {"sms", "email"}:
        channel = "sms"
    if request.method == "POST":
        action = (request.form.get("action") or "send_code").strip()
        username = (request.form.get("username") or "").strip()
        phone = (request.form.get("phone") or "").strip()
        email = (request.form.get("email") or "").strip()
        if channel == "email":
            return _handle_email_password_reset(action, username, email)
        user = _find_user_for_reset(username, phone)
        if _user_is_hidden(user):
            user = None
        if action == "send_code":
            if user is None or not user.is_active or not (user.phone or "").strip():
                flash("لا يوجد رقم موبايل مربوط بالحساب. سجّل الرقم من شاشة المستخدمين أو من حسابي بعد الدخول.", "danger")
                return redirect(url_for("forgot_password"))
            if user.reset_code_recently_sent():
                flash("استنى دقيقة قبل طلب رمز جديد.", "warning")
                return render_template("forgot_password.html", **_forgot_password_context("sms", "verify", user.username, masked_phone=mask_phone(user.phone)))
            code = _new_recovery_pin()
            try:
                send_sms(user.phone, f"رمز استعادة كلمة المرور: {code} صالح 10 دقائق")
            except SmsError as exc:
                flash(str(exc), "danger")
                return redirect(url_for("forgot_password"))
            user.set_reset_code(code)
            db.session.commit()
            flash(f"تم إرسال رمز الاستعادة إلى {mask_phone(user.phone)}", "success")
            return render_template("forgot_password.html", **_forgot_password_context("sms", "verify", user.username, masked_phone=mask_phone(user.phone)))

        if user is None or not user.is_active:
            flash("بيانات الاستعادة غير صحيحة", "danger")
            return redirect(url_for("forgot_password"))
        code = (request.form.get("reset_code") or "").strip()
        new_password = request.form.get("new_password") or ""
        confirm_password = request.form.get("confirm_password") or ""
        sms_ctx = _forgot_password_context("sms", "verify", user.username, masked_phone=mask_phone(user.phone or ""))
        if not user.check_reset_code(code):
            user.reset_attempts = (user.reset_attempts or 0) + 1
            db.session.commit()
            flash("رمز الاستعادة غير صحيح أو منتهي", "danger")
            return render_template("forgot_password.html", **sms_ctx)
        if len(new_password) < 6:
            flash("كلمة المرور الجديدة يجب أن تكون 6 أحرف على الأقل", "danger")
            return render_template("forgot_password.html", **sms_ctx)
        if new_password == "123456":
            flash("لا يمكن استخدام كلمة المرور الافتراضية 123456", "danger")
            return render_template("forgot_password.html", **sms_ctx)
        if new_password != confirm_password:
            flash("تأكيد كلمة المرور غير متطابق", "danger")
            return render_template("forgot_password.html", **sms_ctx)
        user.set_password(new_password)
        user.clear_reset_code()
        db.session.commit()
        flash("تم تعيين كلمة المرور الجديدة. سجل الدخول بها.", "success")
        return redirect(url_for("login"))
    return render_template("forgot_password.html", **_forgot_password_context(channel))


@app.route("/logout")
def logout():
    session.clear()
    flash("تم تسجيل الخروج", "success")
    return redirect(url_for("login"))


@app.route("/setup-admin", methods=["GET", "POST"])
def setup_admin():
    if User.query.count() > 0:
        return redirect(url_for("login"))

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        full_name = (request.form.get("full_name") or "").strip()
        password = request.form.get("password") or ""
        confirm_password = request.form.get("confirm_password") or ""

        if not username or not full_name or not password:
            flash("يرجى استكمال جميع الحقول", "danger")
            return redirect(url_for("setup_admin"))

        if len(password) < 6:
            flash("كلمة المرور يجب أن تكون 6 أحرف على الأقل", "danger")
            return redirect(url_for("setup_admin"))

        if password == "123456":
            flash("لا تستخدم كلمة المرور الافتراضية 123456. اختر كلمة أقوى.", "danger")
            return redirect(url_for("setup_admin"))

        if password != confirm_password:
            flash("تأكيد كلمة المرور غير متطابق", "danger")
            return redirect(url_for("setup_admin"))

        admin = User(username=username, full_name=full_name, role=ROLE_ADMIN, is_active=True)
        admin.set_password(password)
        db.session.add(admin)
        db.session.commit()

        flash("تم إنشاء حساب المدير بنجاح. يمكنك تسجيل الدخول الآن", "success")
        return redirect(url_for("login"))

    return render_template("setup_admin.html")


@app.route("/users", methods=["GET", "POST"])
def users():
    if not user_can("*") and g.current_user.role != ROLE_ADMIN:
        flash("ليس لديك صلاحية للوصول لإدارة المستخدمين", "danger")
        return redirect(url_for("index"))

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        full_name = (request.form.get("full_name") or "").strip()
        password = request.form.get("password") or ""
        role = normalize_legacy_role(request.form.get("role") or ROLE_DATA_ENTRY)
        is_active = request.form.get("is_active") == "on"

        if not username or not full_name or not password:
            flash("يرجى استكمال بيانات المستخدم", "danger")
            return redirect(url_for("users"))

        if password == "123456":
            flash("لا تستخدم كلمة المرور الافتراضية 123456", "danger")
            return redirect(url_for("users"))

        if User.query.filter_by(username=username).first():
            flash("اسم المستخدم مستخدم بالفعل", "danger")
            return redirect(url_for("users"))

        user = User(username=username, full_name=full_name, role=role, is_active=is_active)
        user.set_password(password)
        ok, err = _assign_user_phone(user, request.form.get("phone"))
        if not ok:
            flash(err, "danger")
            return redirect(url_for("users"))
        raw_email = (request.form.get("email") or "").strip()
        if raw_email:
            ok, err, _sent = _start_email_verification(user, raw_email, send_code=True)
            if not ok:
                flash(err, "danger")
                return redirect(url_for("users"))
        db.session.add(user)
        db.session.commit()
        notes = ["تم إنشاء المستخدم"]
        if user.phone:
            notes.append(f"الموبايل {mask_phone(user.phone)}")
        if user.pending_email:
            notes.append(f"بريد بانتظار التأكيد {mask_email(user.pending_email)}")
        flash(". ".join(notes), "success")
        return redirect(url_for("users"))

    all_users = _visible_users_query().order_by(User.id.desc()).all()
    user_perm_map = {item.id: effective_perms(item) for item in all_users}
    treasury_accounts = [
        account for account in ChartOfAccount.query.order_by(ChartOfAccount.code, ChartOfAccount.name).all()
        if is_operational_treasury_account(account)
    ]
    user_hidden_treasuries = {item.id: hidden_treasury_ids(item) for item in all_users}
    return render_template(
        "users.html",
        users=all_users,
        role_options=ROLE_OPTIONS,
        permission_groups=PERMISSION_GROUPS,
        user_perm_map=user_perm_map,
        treasury_accounts=treasury_accounts,
        user_hidden_treasuries=user_hidden_treasuries,
    )


@app.route("/activity-log")
def activity_log():
    if not user_can("*") and g.current_user.role != ROLE_ADMIN:
        flash("سجل الحركات متاح لمدير النظام فقط", "danger")
        return redirect(url_for("index"))

    from_date = (request.args.get("from_date") or "").strip()
    to_date = (request.args.get("to_date") or "").strip()
    user_id = (request.args.get("user_id") or "").strip()
    action = (request.args.get("action") or "").strip()
    entity_label = (request.args.get("entity_label") or "").strip()

    query = ActivityLog.query
    hidden_ids = [item.id for item in User.query.filter(User.is_hidden.is_(True)).all()]
    if hidden_ids:
        query = query.filter(db.or_(ActivityLog.user_id.is_(None), ~ActivityLog.user_id.in_(hidden_ids)))
    if from_date:
        query = query.filter(ActivityLog.created_at >= from_date + " 00:00:00")
    if to_date:
        query = query.filter(ActivityLog.created_at <= to_date + " 23:59:59")
    if user_id.isdigit():
        query = query.filter(ActivityLog.user_id == int(user_id))
    if action:
        query = query.filter(ActivityLog.action == action)
    if entity_label:
        query = query.filter(ActivityLog.entity_label == entity_label)

    rows = query.order_by(ActivityLog.id.desc()).limit(1000).all()
    users = _visible_users_query().order_by(User.full_name, User.username).all()
    actions = ["إضافة", "تعديل", "حذف", "ترحيل", "إلغاء ترحيل"]
    entity_labels = sorted({label for label in (
        "قيد يومية", "مستخلص", "دفعة مقاول باطن", "تحصيل عميل", "سداد مورد",
        "أمر شراء", "حركة مخزون", "حركة عمالة", "معدة", "تسوية عهدة",
        "محاسبة سائق", "مقايسة", "قيد تكلفة", "مشروع", "حساب", "مورد", "مقاول باطن",
    )})
    return render_template(
        "activity_log.html",
        rows=rows,
        users=users,
        actions=actions,
        entity_labels=entity_labels,
        from_date=from_date,
        to_date=to_date,
        selected_user_id=user_id,
        selected_action=action,
        selected_entity_label=entity_label,
    )


@app.route("/users/<int:user_id>/update", methods=["POST"])
def update_user(user_id):
    if g.current_user is None or g.current_user.role != ROLE_ADMIN:
        flash("ليس لديك صلاحية لتعديل المستخدمين", "danger")
        return redirect(url_for("index"))

    user = User.query.get_or_404(user_id)
    if _user_is_hidden(user):
        flash("المستخدم غير موجود", "danger")
        return redirect(url_for("users"))
    user.full_name = (request.form.get("full_name") or "").strip() or user.full_name
    user.role = normalize_legacy_role(request.form.get("role") or user.role)
    user.is_active = request.form.get("is_active") == "on"
    ok, err = _assign_user_phone(user, request.form.get("phone"))
    if not ok:
        flash(err, "danger")
        return redirect(url_for("users"))
    if "email" in request.form:
        ok, err, _sent = _start_email_verification(user, request.form.get("email"), send_code=True)
        if not ok:
            flash(err, "danger")
            return redirect(url_for("users"))

    new_password = request.form.get("password") or ""
    if new_password:
        if len(new_password) < 6:
            flash("كلمة المرور الجديدة يجب أن تكون 6 أحرف على الأقل", "danger")
            return redirect(url_for("users"))
        if new_password == "123456":
            flash("لا يمكن تعيين كلمة المرور الافتراضية 123456", "danger")
            return redirect(url_for("users"))
        user.set_password(new_password)
        user.clear_reset_code()

    if user.id == g.current_user.id and not user.is_active:
        flash("لا يمكنك تعطيل حسابك أثناء تسجيل الدخول", "danger")
        return redirect(url_for("users"))

    db.session.commit()
    flash("تم تحديث المستخدم بنجاح", "success")
    return redirect(url_for("users"))


def _detach_user_references(user_id):
    ActivityLog.query.filter_by(user_id=user_id).update({"user_id": None}, synchronize_session=False)
    for mapper in db.Model.registry.mappers:
        cls = mapper.class_
        if getattr(cls, "__name__", "") in {"User", "ActivityLog"}:
            continue
        if hasattr(cls, "created_by_id"):
            cls.query.filter_by(created_by_id=user_id).update({"created_by_id": None}, synchronize_session=False)
        if hasattr(cls, "updated_by_id"):
            cls.query.filter_by(updated_by_id=user_id).update({"updated_by_id": None}, synchronize_session=False)


@app.route("/users/<int:user_id>/delete", methods=["POST"])
def delete_user(user_id):
    if g.current_user is None or g.current_user.role != ROLE_ADMIN:
        flash("ليس لديك صلاحية لحذف المستخدمين", "danger")
        return redirect(url_for("index"))

    user = User.query.get_or_404(user_id)
    if _user_is_hidden(user):
        flash("المستخدم غير موجود", "danger")
        return redirect(url_for("users"))
    if user.id == g.current_user.id:
        flash("لا يمكن حذف حسابك أثناء تسجيل الدخول", "danger")
        return redirect(url_for("users"))
    if user.role == ROLE_ADMIN and _visible_users_query().filter(User.role == ROLE_ADMIN, User.id != user.id).count() == 0:
        flash("لا يمكن حذف آخر مدير نظام", "danger")
        return redirect(url_for("users"))

    username = user.username
    _detach_user_references(user.id)
    db.session.delete(user)
    db.session.commit()
    flash(f"تم حذف المستخدم {username}", "success")
    return redirect(url_for("users"))


@app.route("/users/<int:user_id>/permissions", methods=["POST"])
def update_user_permissions(user_id):
    if g.current_user is None or g.current_user.role != ROLE_ADMIN:
        flash("مدير النظام فقط يقدر يعدل صلاحيات المستخدمين", "danger")
        return redirect(url_for("index"))

    user = User.query.get_or_404(user_id)
    if _user_is_hidden(user):
        flash("المستخدم غير موجود", "danger")
        return redirect(url_for("users"))
    if user.role == ROLE_ADMIN:
        user.permissions_json = dump_permissions(sanitize_permissions([], user))
        db.session.commit()
        flash("صلاحيات مدير النظام كاملة ولا يمكن تقييدها", "warning")
        return redirect(url_for("users"))

    selected = request.form.getlist("perm")
    hidden_treasuries = []
    if "treasury_scope" in request.form:
        visible = {as_int(item) for item in request.form.getlist("visible_treasury") if as_int(item)}
        hidden_treasuries = [
            account.id
            for account in ChartOfAccount.query.order_by(ChartOfAccount.code).all()
            if is_operational_treasury_account(account) and account.id not in visible
        ]
    user.permissions_json = dump_permissions(sanitize_permissions(selected, user), hidden_treasuries)
    db.session.commit()
    flash(f"تم تحديث صلاحيات {user.full_name}", "success")
    return redirect(url_for("users"))


@app.route("/admin/amounts/unlock", methods=["POST"])
@app.route("/account/pin/unlock", methods=["POST"])
def unlock_admin_amounts():
    if g.current_user is None:
        return jsonify({"ok": False}), 403
    pin = (request.form.get("pin") or "").strip()
    if not g.current_user.check_amount_pin(pin):
        return jsonify({"ok": False, "message": "الرقم السري غير صحيح"}), 403
    return jsonify({"ok": True})


@app.route("/admin/amounts/lock", methods=["POST"])
def lock_admin_amounts():
    if g.current_user is None:
        return jsonify({"ok": False}), 403
    return jsonify({"ok": True})


@app.route("/admin/amounts/pin", methods=["POST"])
@app.route("/account/pin", methods=["POST"])
def update_admin_amount_pin():
    user = g.current_user
    if user is None:
        flash("يجب تسجيل الدخول أولاً", "danger")
        return redirect(url_for("login"))
    current_pin = (request.form.get("current_pin") or "").strip()
    new_pin = (request.form.get("new_pin") or "").strip()
    confirm_pin = (request.form.get("confirm_pin") or "").strip()
    if not user.check_amount_pin(current_pin):
        flash("الرقم السري الحالي غير صحيح", "danger")
        return redirect(url_for("change_password"))
    if len(new_pin) < 4:
        flash("الرقم السري الجديد يجب ألا يقل عن 4 أرقام", "danger")
        return redirect(url_for("change_password"))
    if new_pin != confirm_pin:
        flash("تأكيد الرقم السري غير متطابق", "danger")
        return redirect(url_for("change_password"))
    if new_pin == current_pin:
        flash("اختار رقم سري مختلف عن الحالي", "warning")
        return redirect(url_for("change_password"))
    user.set_amount_pin(new_pin)
    db.session.commit()
    flash("تم حفظ الرقم السري للعين السحرية", "success")
    return redirect(url_for("change_password"))


@app.route("/account/password", methods=["GET", "POST"])
def change_password():
    user = g.current_user
    if request.method == "POST":
        current_password = request.form.get("current_password") or ""
        new_password = request.form.get("new_password") or ""
        confirm_password = request.form.get("confirm_password") or ""
        if not user.check_password(current_password):
            flash("كلمة المرور الحالية غير صحيحة", "danger")
            return redirect(url_for("change_password"))
        if len(new_password) < 6:
            flash("كلمة المرور الجديدة يجب أن تكون 6 أحرف على الأقل", "danger")
            return redirect(url_for("change_password"))
        if new_password == "123456":
            flash("لا يمكن استخدام كلمة المرور الافتراضية 123456", "danger")
            return redirect(url_for("change_password"))
        if new_password != confirm_password:
            flash("تأكيد كلمة المرور غير متطابق", "danger")
            return redirect(url_for("change_password"))
        if new_password == current_password:
            flash("اختر كلمة مرور مختلفة عن الحالية", "danger")
            return redirect(url_for("change_password"))
        user.set_password(new_password)
        db.session.commit()
        session.pop("must_change_password", None)
        flash("تم تغيير كلمة المرور بنجاح", "success")
        return redirect(url_for("index"))
    return render_template("change_password.html", sms_ready=sms_configured(), mail_ready=mail_configured())


@app.route("/account/phone", methods=["POST"])
def update_account_phone():
    user = g.current_user
    if user is None:
        flash("يجب تسجيل الدخول أولاً", "danger")
        return redirect(url_for("login"))
    current_password = request.form.get("current_password") or ""
    if not user.check_password(current_password):
        flash("كلمة المرور الحالية غير صحيحة", "danger")
        return redirect(url_for("change_password"))
    ok, err = _assign_user_phone(user, request.form.get("phone"))
    if not ok:
        flash(err, "danger")
        return redirect(url_for("change_password"))
    db.session.commit()
    if user.phone:
        flash(f"تم ربط الموبايل {mask_phone(user.phone)} بالحساب. لو نسيت كلمة المرور الرمز هيتبعت عليه.", "success")
    else:
        flash("تم مسح رقم الموبايل من الحساب", "success")
    return redirect(url_for("change_password"))


@app.route("/account/email", methods=["POST"])
def update_account_email():
    user = g.current_user
    if user is None:
        flash("يجب تسجيل الدخول أولاً", "danger")
        return redirect(url_for("login"))
    current_password = request.form.get("current_password") or ""
    if not user.check_password(current_password):
        flash("كلمة المرور الحالية غير صحيحة", "danger")
        return redirect(url_for("change_password"))
    ok, err, _sent = _start_email_verification(user, request.form.get("email"), send_code=True)
    if not ok:
        flash(err, "danger")
        return redirect(url_for("change_password"))
    db.session.commit()
    flash(err, "success")
    return redirect(url_for("change_password"))


@app.route("/account/email/verify", methods=["POST"])
def verify_account_email():
    user = g.current_user
    if user is None:
        flash("يجب تسجيل الدخول أولاً", "danger")
        return redirect(url_for("login"))
    code = (request.form.get("email_code") or "").strip()
    if not user.check_email_verify_code(code):
        user.email_verify_attempts = (user.email_verify_attempts or 0) + 1
        db.session.commit()
        flash("رمز تأكيد البريد غير صحيح أو منتهي", "danger")
        return redirect(url_for("change_password"))
    if not user.apply_verified_email():
        flash("لا يوجد بريد بانتظار التأكيد", "danger")
        return redirect(url_for("change_password"))
    db.session.commit()
    flash("تم تأكيد البريد الإلكتروني. تقدر تستخدمه لاستعادة كلمة المرور.", "success")
    return redirect(url_for("change_password"))


@app.route("/account/email/resend", methods=["POST"])
def resend_email_code():
    user = g.current_user
    if user is None:
        flash("يجب تسجيل الدخول أولاً", "danger")
        return redirect(url_for("login"))
    pending = (user.pending_email or "").strip()
    if not pending:
        flash("لا يوجد بريد بانتظار التأكيد", "danger")
        return redirect(url_for("change_password"))
    ok, err, _sent = _start_email_verification(user, pending, send_code=True)
    if not ok:
        flash(err, "danger")
        return redirect(url_for("change_password"))
    db.session.commit()
    flash(err, "success")
    return redirect(url_for("change_password"))


@app.route("/account/recovery-pin", methods=["POST"])
def update_recovery_pin():
    user = g.current_user
    if user is None:
        flash("يجب تسجيل الدخول أولاً", "danger")
        return redirect(url_for("login"))
    current_password = request.form.get("current_password") or ""
    new_pin = (request.form.get("recovery_pin") or "").strip()
    confirm_pin = (request.form.get("confirm_recovery_pin") or "").strip()
    if not user.check_password(current_password):
        flash("كلمة المرور الحالية غير صحيحة", "danger")
        return redirect(url_for("change_password"))
    if len(new_pin) < 4:
        flash("رمز الاستعادة يجب ألا يقل عن 4 أرقام", "danger")
        return redirect(url_for("change_password"))
    if new_pin != confirm_pin:
        flash("تأكيد رمز الاستعادة غير متطابق", "danger")
        return redirect(url_for("change_password"))
    user.set_recovery_pin(new_pin)
    db.session.commit()
    flash("تم حفظ رمز الاستعادة. لو نسيت كلمة المرور استخدمه من شاشة الدخول.", "success")
    return redirect(url_for("change_password"))


@app.route("/")
def index():
    projects = Project.query.count()
    accounts = ChartOfAccount.query.count()
    progress_payments = ProgressPayment.query.count()
    subcontractors = Subcontractor.query.count()
    suppliers = Supplier.query.count()
    purchase_orders = PurchaseOrder.query.count()
    inventory_transactions = InventoryTransaction.query.count()
    labor_entries = LaborEntry.query.count()
    equipment_items = Equipment.query.count()
    draft_purchase_orders = PurchaseOrder.query.filter(PurchaseOrder.status == "مفتوح").count()
    paid_purchase_orders = PurchaseOrder.query.filter(PurchaseOrder.status == "مدفوع").count()
    outstanding_payments = ProgressPayment.query.filter(
        ProgressPayment.subcontractor_id.isnot(None),
        ProgressPayment.net_value > 0,
    ).count()
    draft_journal_entries = len(filter_visible_journals(JournalEntry.query.filter(JournalEntry.status != "مرحل").all()))
    # قيمة القيود المرحلة فقط، لأن المسودات لا تُعد حركة محاسبية معتمدة
    visible_posted = filter_visible_journals(JournalEntry.query.filter(JournalEntry.status == "مرحل").all())
    total_journal_value = round(sum(as_float(item.amount) for item in visible_posted), 2)
    journal_entries = len(filter_visible_journals(JournalEntry.query.all()))
    recent_entries = filter_visible_journals(JournalEntry.query.order_by(JournalEntry.id.desc()).limit(40).all())[:8]
    all_accounts = filter_hidden_treasuries(
        ChartOfAccount.query.order_by(ChartOfAccount.category, ChartOfAccount.code).all()
    )
    account_balances = build_account_balances(all_accounts)

    # حساب المورد/المقاول دائن بطبيعته: الرصيد السالب في نظام (مدين - دائن) يعني مستحقًا عليه للشركة.
    # حساب العميل مدين بطبيعته: الرصيد الموجب هو المستحق لنا لدى العميل.
    supplier_dues_rows = []
    customer_dues_rows = []
    for account in all_accounts:
        balance = as_float(account_balances.get(account.id, 0.0))
        if account.category in ("الموردين", "موردين", "مقاولي الباطن", "الموظفين"):
            due_amount = -balance if balance < 0 else 0.0
            if due_amount > 0:
                supplier_dues_rows.append({"name": account.name, "amount": round(due_amount, 2)})
        elif account.category == "العملاء":
            due_amount = balance if balance > 0 else 0.0
            if due_amount > 0:
                customer_dues_rows.append({"name": account.name, "amount": round(due_amount, 2)})

    treasury_balance = get_treasury_balance(all_accounts, account_balances)
    payable_due_total = round(sum(row["amount"] for row in supplier_dues_rows), 2)
    top_supplier_dues = sorted(supplier_dues_rows, key=lambda row: row["amount"], reverse=True)[:5]
    top_customer_dues = sorted(customer_dues_rows, key=lambda row: row["amount"], reverse=True)[:5]

    WEEKDAYS_AR = ["الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]
    today = date.today()
    today_label = f"{WEEKDAYS_AR[today.weekday()]} {today.strftime('%Y/%m/%d')}"

    # الإيرادات الفعلية = صافي الدائن على حسابات الإيرادات (القيود المرحلة فقط)
    revenue_total = 0.0
    for account in all_accounts:
        if account.category in ("الإيرادات", "فروق أسعار"):
            revenue_total += -as_float(account_balances.get(account.id, 0.0))
    revenue_total = round(max(revenue_total, 0.0), 2)

    # إيراد المبيعات = مستخلصات العملاء فقط (بدون مقاولي باطن)
    client_certificates = client_progress_query().all()
    orders_sales_total = round(sum(as_float(item.total_value) for item in client_certificates), 2)
    collections_total = round(sum(as_float(item.amount) for item in ClientReceipt.query.all()), 2)
    trip_sales_items = SalesTrip.query.all()
    trip_sales_total = round(sum(as_float(item.total_amount) for item in trip_sales_items), 2)
    trip_sales_count = len(trip_sales_items)
    direct_sales_total = round(max(revenue_total - orders_sales_total, 0.0), 2)
    sales_total = orders_sales_total + direct_sales_total
    orders_sales_ratio = (orders_sales_total / sales_total) if sales_total > 0 else 0.0
    direct_sales_ratio = (direct_sales_total / sales_total) if sales_total > 0 else 0.0

    active_custody_rows = build_custody_balances(CustodySettlement.query.all())
    active_custody_total = round(sum(row["remaining"] for row in active_custody_rows if row["remaining"] > 0), 2)
    subcontractor_net_due = round(sum(
        build_subcontractor_statement(item)["net_due"] for item in Subcontractor.query.all()
    ), 2)

    journal_boards = [
        {
            "title": "مستخلصات العملاء",
            "subtitle": "إجمالي الأعمال المفوترة للعميل",
            "count": len(client_certificates),
            "total": orders_sales_total,
            "primary_action": {"label": "مستخلص عميل", "url": url_for("client_progress_payments")},
            "secondary_action": {"label": "مستخلص باطن", "url": url_for("progress_payments")},
            "theme": "sales",
        },
        {
            "title": "المقايسات",
            "subtitle": "قيمة مقايسات العملاء النهائية",
            "count": Estimation.query.count(),
            "total": round(sum(as_float(item.final_value) for item in Estimation.query.all()), 2),
            "primary_action": {"label": "مقايسة جديدة", "url": url_for("estimations")},
            "secondary_action": {"label": "ربحية المقايسات", "url": url_for("estimation_profitability_report")},
            "theme": "sales",
        },
        {
            "title": "مشتريات",
            "subtitle": "أوامر شراء وفواتير موردين",
            "count": purchase_orders,
            "total": sum(item.total_value for item in PurchaseOrder.query.all()),
            "primary_action": {"label": "أمر شراء جديد", "url": url_for("purchase_orders")},
            "secondary_action": {"label": "الموردون", "url": url_for("suppliers")},
            "theme": "purchase",
        },
        {
            "title": "بنك وصندوق",
            "subtitle": "حركة الأموال والسيولة",
            "count": journal_entries,
            "total": total_journal_value,
            "primary_action": {"label": "قيد يومية", "url": url_for("journal")},
            "secondary_action": {"label": "دليل الحسابات", "url": url_for("accounts")},
            "theme": "bank",
        },
        {
            "title": "عمليات",
            "subtitle": "المخزون، العمالة، المعدات",
            "count": inventory_transactions + labor_entries + equipment_items,
            "total": 0,
            "primary_action": {"label": "حركة مخزون", "url": url_for("inventory")},
            "secondary_action": {"label": "تقارير المشروع", "url": url_for("project_report")},
            "theme": "ops",
        },
    ]

    return render_template(
        "index.html",
        projects=projects,
        accounts=accounts,
        progress_payments=progress_payments,
        journal_entries=journal_entries,
        draft_journal_entries=draft_journal_entries,
        subcontractors=subcontractors,
        suppliers=suppliers,
        purchase_orders=purchase_orders,
        inventory_transactions=inventory_transactions,
        labor_entries=labor_entries,
        equipment_items=equipment_items,
        draft_purchase_orders=draft_purchase_orders,
        paid_purchase_orders=paid_purchase_orders,
        outstanding_payments=outstanding_payments,
        total_journal_value=total_journal_value,
        recent_entries=recent_entries,
        journal_boards=journal_boards,
        top_supplier_dues=top_supplier_dues,
        top_customer_dues=top_customer_dues,
        orders_sales_total=orders_sales_total,
        collections_total=collections_total,
        trip_sales_total=trip_sales_total,
        trip_sales_count=trip_sales_count,
        direct_sales_total=direct_sales_total,
        orders_sales_ratio=orders_sales_ratio,
        direct_sales_ratio=direct_sales_ratio,
        revenue_total=revenue_total,
        active_custody_total=active_custody_total,
        subcontractor_net_due=subcontractor_net_due,
        treasury_balance=treasury_balance,
        pin_treasury_total=has_pin_protected_treasury(all_accounts),
        payable_due_total=payable_due_total,
        today_label=today_label,
        lookup_accounts=all_accounts,
    )


def _app_public_url():
    return request.url_root.rstrip("/") + "/"


def _shortcut_is_local(app_url):
    host = (urlsplit(app_url).hostname or "").lower()
    return host in {"127.0.0.1", "localhost", "::1"}


@app.route("/desktop-shortcut")
def desktop_shortcut():
    app_url = _app_public_url()
    return render_template(
        "desktop_shortcut.html",
        app_url=app_url,
        is_local=_shortcut_is_local(app_url),
    )


@app.route("/desktop-shortcut/url")
def download_desktop_url():
    app_url = _app_public_url()
    body = "[InternetShortcut]\r\nURL=" + app_url + "\r\n"
    return Response(
        body,
        mimetype="application/octet-stream",
        headers={
            "Content-Disposition": "attachment; filename=Mohasabat.url",
        },
    )


def _purchase_order_form_error():
    project_id = as_int(request.form.get("project_id"))
    supplier_id = as_int(request.form.get("supplier_id"))
    lines = parse_purchase_order_items(request.form)
    if not project_id:
        return "يرجى اختيار مشروع", None, None, None, None
    if not supplier_id:
        return "يرجى اختيار المورد", None, None, None, None
    if not lines:
        return "أضف صنفا واحدا على الأقل بكمية أكبر من صفر", None, None, None, None
    status = request.form.get("status") or "مفتوح"
    if status not in PO_STATUS_OPTIONS:
        status = "مفتوح"
    return None, project_id, supplier_id, lines, status


def _warehouse_picker_vars():
    accounts = filter_hidden_treasuries(
        ChartOfAccount.query.order_by(ChartOfAccount.category, ChartOfAccount.code, ChartOfAccount.name).all()
    )
    return {
        "warehouse_groups": get_warehouse_picker_groups(accounts),
        "chart_warehouse_groups": get_chart_warehouse_picker_groups(accounts),
        "account_groups": get_chart_account_picker_groups(accounts),
        "warehouse_names": get_warehouse_names(),
        "default_warehouse": DEFAULT_WAREHOUSE,
    }


def _fill_purchase_order_header(order, project_id, supplier_id, status):
    warehouse_name, warehouse_account_id = parse_warehouse_choice(request.form.get("warehouse_name"))
    order.project_id = project_id
    order.supplier_id = supplier_id
    order.warehouse_name = warehouse_name
    order.warehouse_account_id = warehouse_account_id
    order.order_number = (request.form.get("order_number") or "").strip() or None
    order.invoice_number = (request.form.get("invoice_number") or "").strip() or None
    order.date = request.form.get("date") or date.today().isoformat()
    order.status = status
    order.notes = request.form.get("notes")
    order.equipment_id = as_int(request.form.get("equipment_id")) or None
    return order


COMPANY_PRINT_NAME = "شركة السيد الشيخ للمقاولات العمومية"
FINANCE_ADMIN_MANAGER = "IBRAHIM ABD ALKREEM"
FINANCE_ADMIN_TITLE = "المدير المالي والإداري"
ARABIC_WEEKDAYS = ["الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]


def arabic_weekday(value):
    raw = (value or "")[:10]
    try:
        year, month, day = [int(part) for part in raw.split("-")]
        return ARABIC_WEEKDAYS[date(year, month, day).weekday()]
    except (TypeError, ValueError):
        return ""


def _print_stamp(title="", department="قسم الحسابات", signer_title="المحاسب"):
    now = datetime.now()
    today_iso = date.today().isoformat()
    hour = now.hour % 12 or 12
    suffix = "م" if now.hour >= 12 else "ص"
    return {
        "company_name": COMPANY_PRINT_NAME,
        "print_date": today_iso,
        "print_time": f"{hour}:{now.strftime('%M')} {suffix}",
        "weekday_name": arabic_weekday(today_iso),
        "print_title": title or "",
        "print_department": department,
        "print_signer_title": signer_title,
        "finance_manager": FINANCE_ADMIN_MANAGER,
        "finance_manager_title": FINANCE_ADMIN_TITLE,
    }


def _purchase_orders_query():
    query = PurchaseOrder.query
    from_date = (request.args.get("from_date") or "").strip()
    to_date = (request.args.get("to_date") or "").strip()
    supplier_id = as_int(request.args.get("supplier_id"))
    if from_date:
        query = query.filter(PurchaseOrder.date >= from_date)
    if to_date:
        query = query.filter(PurchaseOrder.date <= to_date)
    if supplier_id:
        query = query.filter(PurchaseOrder.supplier_id == supplier_id)
    return query.order_by(PurchaseOrder.date.desc(), PurchaseOrder.id.desc()), {
        "from_date": from_date,
        "to_date": to_date,
        "supplier_id": supplier_id or "",
    }


def _purchase_order_sheet_totals(items):
    return {
        "count": len(items),
        "quantity": round(sum(as_float(item.quantity) for item in items), 2),
        "total_value": round(sum(as_float(item.total_value) for item in items), 2),
    }


@app.route("/purchase_orders", methods=["GET", "POST"])
def purchase_orders():
    projects = Project.query.order_by(Project.code).all()
    suppliers = Supplier.query.order_by(Supplier.name).all()
    material_names = get_material_names()
    picker = _warehouse_picker_vars()
    if request.method == "POST":
        unpacked = _purchase_order_form_error()
        error = unpacked[0]
        if error:
            flash(error, "danger")
            return redirect(url_for("purchase_orders"))
        _, project_id, supplier_id, lines, status = unpacked
        order = PurchaseOrder()
        try:
            _fill_purchase_order_header(order, project_id, supplier_id, status)
            assert_period_open(order.date)
        except (PeriodClosedError, ValueError) as exc:
            flash(str(exc), "danger")
            return redirect(url_for("purchase_orders"))
        db.session.add(order)
        db.session.flush()
        replace_purchase_order_items(order, lines)
        try:
            persist_synced(
                lambda: sync_purchase_order_to_inventory(order, commit=False),
                lambda: sync_purchase_order_journal(order),
            )
        except (PeriodClosedError, ValueError) as exc:
            db.session.rollback()
            flash(str(exc), "danger")
            return redirect(url_for("purchase_orders"))

        uploaded = request.files.get("attachment")
        if uploaded and uploaded.filename:
            from routes.cycle import save_attachment
            try:
                save_attachment("purchase_order", order.id, uploaded)
            except ValueError as exc:
                flash(str(exc), "danger")

        if status == "مفتوح":
            flash("تم حفظ أمر الشراء. الاستلام للمخزون وذمة المورد يتمان عند تغيير الحالة إلى مغلق أو مدفوع. السداد النقدي من شاشة سداد الموردين.", "success")
        else:
            flash("تم حفظ أمر الشراء واستلام الأصناف في المخزن مع قيد ذمة المورد. سجّل السداد النقدي من شاشة سداد الموردين.", "success")
        return redirect(url_for("purchase_orders"))
    query, filters = _purchase_orders_query()
    items = query.all()
    grouped_attachments = {}
    for row in DocumentAttachment.query.filter_by(entity_type="purchase_order").order_by(DocumentAttachment.id.desc()).all():
        grouped_attachments.setdefault(row.entity_id, []).append(row)
    return render_template(
        "purchase_orders.html",
        items=items,
        totals=_purchase_order_sheet_totals(items),
        filters=filters,
        projects=projects,
        suppliers=suppliers,
        material_names=material_names,
        unit_options=UNIT_OPTIONS,
        status_options=PO_STATUS_OPTIONS,
        today_date=date.today().isoformat(),
        attachments=grouped_attachments,
        equipment_items=Equipment.query.order_by(Equipment.name).all(),
        **picker,
    )


@app.route("/purchase_orders/print")
def print_purchase_orders():
    query, filters = _purchase_orders_query()
    items = query.all()
    stamp = _print_stamp("تقرير المشتريات / أوامر الشراء", "قسم المشتريات", "مدير المشتريات")
    return render_template(
        "print_purchase_orders.html",
        items=items,
        totals=_purchase_order_sheet_totals(items),
        filters=filters,
        **stamp,
    )


@app.route("/purchase_orders/<int:order_id>/update", methods=["POST"])
def update_purchase_order(order_id):
    order = PurchaseOrder.query.get_or_404(order_id)
    original_date = order.date
    unpacked = _purchase_order_form_error()
    error = unpacked[0]
    if error:
        flash(error, "danger")
        return redirect(url_for("purchase_orders"))
    _, project_id, supplier_id, lines, status = unpacked
    try:
        _fill_purchase_order_header(order, project_id, supplier_id, status)
        assert_period_open(original_date)
        assert_period_open(order.date)
        replace_purchase_order_items(order, lines)
        persist_synced(
            lambda: sync_purchase_order_to_inventory(order, commit=False),
            lambda: sync_purchase_order_journal(order),
        )
    except (PeriodClosedError, ValueError) as exc:
        db.session.rollback()
        flash(str(exc), "danger")
        return redirect(url_for("purchase_orders"))
    flash("تم تحديث أمر الشراء بنجاح", "success")
    return redirect(url_for("purchase_orders"))


def _inventory_from_form(item=None):
    project_id = as_int(request.form.get("project_id"))
    if not project_id:
        raise ValueError("يرجى اختيار مشروع صحيح")
    warehouse_name, warehouse_account_id = parse_warehouse_choice(request.form.get("warehouse_name"))
    destination, destination_account_id = parse_optional_warehouse_choice(request.form.get("destination_warehouse"))
    known_types = {INV_TYPE_IN, INV_TYPE_OUT, "إضافة", "سحب", "تحويل"}
    tx_type = (request.form.get("transaction_type") or "").strip()
    if tx_type not in known_types:
        tx_type = INV_TYPE_IN
    quantity = as_float(request.form.get("quantity"))
    unit_cost = as_float(request.form.get("unit_cost"))
    line_amount = as_float(request.form.get("line_amount"))
    material_name = normalize_name(request.form.get("material_name"))
    if normalize_inventory_type(tx_type) == INV_TYPE_OUT and tx_type != "سحب" and not destination:
        raise ValueError("المنصرف يتطلب اختيار الحساب الوجهة")
    transaction = item or InventoryTransaction()
    transaction.project_id = project_id
    transaction.supplier_id = as_int(request.form.get("supplier_id")) or None
    transaction.warehouse_name = warehouse_name
    transaction.warehouse_account_id = warehouse_account_id
    transaction.destination_warehouse = destination or None
    transaction.destination_account_id = destination_account_id
    transaction.transaction_type = tx_type
    transaction.quantity = quantity
    transaction.material_name = material_name or "تحويل خزنة"
    if line_amount > 0 and quantity > 0:
        unit_cost = line_amount / quantity
    if is_monetary_account_transfer(transaction):
        if unit_cost <= 0:
            unit_cost = 1
    elif (normalize_inventory_type(tx_type) == INV_TYPE_OUT or tx_type in {"سحب", "تحويل"}) and unit_cost <= 0:
        unit_cost = average_stock_cost(
            project_id, warehouse_name, transaction.material_name,
            exclude_tx_id=item.id if item else None,
        )
    if not material_name and not is_monetary_account_transfer(transaction):
        raise ValueError("يرجى إدخال اسم الصنف")
    transaction.recipient_name = normalize_name(request.form.get("recipient_name")) or None
    transaction.storekeeper_name = normalize_name(request.form.get("storekeeper_name")) or None
    transaction.unit = normalize_name(request.form.get("unit")) or None
    transaction.unit_cost = unit_cost
    transaction.date = request.form.get("date") or date.today().isoformat()
    transaction.notes = request.form.get("notes") or ""
    transaction.equipment_id = as_int(request.form.get("equipment_id")) or None
    assert_period_open(transaction.date)
    assert_inventory_quantity_available(transaction, exclude_tx_id=item.id if item else None)
    return transaction


@app.route("/inventory", methods=["GET", "POST"])
def inventory():
    projects = Project.query.order_by(Project.code).all()
    suppliers = Supplier.query.order_by(Supplier.name).all()
    picker = _warehouse_picker_vars()
    if request.method == "POST":
        try:
            transaction = _inventory_from_form()
            db.session.add(transaction)
            persist_synced(lambda: sync_inventory_transaction_journal(transaction))
        except (PeriodClosedError, ValueError) as exc:
            db.session.rollback()
            flash(str(exc), "danger")
            return redirect(url_for("inventory"))
        flash("تم تسجيل حركة المخزون بنجاح", "success")
        return redirect(url_for("inventory"))
    ensure_inventory_transaction_journals()
    items = InventoryTransaction.query.order_by(InventoryTransaction.date.desc()).all()
    return render_template(
        "inventory.html",
        items=items,
        stock_rows=build_stock_rows(),
        projects=projects,
        suppliers=suppliers,
        unit_options=UNIT_OPTIONS,
        inventory_type_options=INV_TYPE_OPTIONS,
        inventory_type_label=inventory_type_label,
        today_date=date.today().isoformat(),
        equipment_items=Equipment.query.order_by(Equipment.name).all(),
        **picker,
    )


@app.route("/inventory/<int:transaction_id>/update", methods=["POST"])
def update_inventory_transaction(transaction_id):
    item = InventoryTransaction.query.get_or_404(transaction_id)
    if is_purchase_order_inventory(item):
        flash("حركة الاستلام من أمر الشراء تُعدّل من شاشة أوامر الشراء", "danger")
        return redirect(url_for("inventory"))
    try:
        _inventory_from_form(item)
        persist_synced(lambda: sync_inventory_transaction_journal(item))
    except (PeriodClosedError, ValueError) as exc:
        db.session.rollback()
        flash(str(exc), "danger")
        return redirect(url_for("inventory"))
    flash("تم تحديث حركة المخزون وقيدها المحاسبي بنجاح", "success")
    return redirect(url_for("inventory"))


@app.route("/custody_settlements", methods=["GET", "POST"])
def custody_settlements():
    sync_journal_related_accounts()
    projects = Project.query.order_by(Project.code).all()
    accounts = ChartOfAccount.query.order_by(ChartOfAccount.category, ChartOfAccount.code).all()
    treasury_accounts = filter_hidden_treasuries(
        [account for account in accounts if is_treasury_account(account)]
    )
    default_treasury_account_id = treasury_accounts[0].id if treasury_accounts else None
    entity_accounts = get_custody_entity_accounts(accounts)
    expense_accounts = get_custody_expense_accounts(accounts, entity_accounts)
    operation_filter = (request.args.get("operation_type") or "").strip()
    from_date = (request.args.get("from_date") or "").strip()
    to_date = (request.args.get("to_date") or "").strip()
    search_name = (request.args.get("search_name") or request.args.get("entity_name") or "").strip()

    if request.method == "POST":
        operation_type = (request.form.get("operation_type") or request.form.get("voucher_type") or "صرف عهدة").strip()
        if operation_type not in CUSTODY_OPERATION_TYPES:
            operation_type = "صرف عهدة"
        settlement_lines = parse_custody_settlement_lines(request.form)
        amount = as_float(request.form.get("amount"))
        if operation_type == "تسوية عهدة":
            line_total = round(sum(line["amount"] for line in settlement_lines), 2)
            amount = line_total or amount
            if amount > 0 and not settlement_lines:
                fallback = get_account_by_code("EXP-OTH") or get_account_by_code("EXP-EQP")
                if fallback:
                    settlement_lines = [{
                        "line_no": 1,
                        "expense_account_id": fallback.id,
                        "amount": amount,
                        "description": (request.form.get("notes") or "").strip() or "تسوية عهدة",
                        "reference": (request.form.get("reference") or "").strip(),
                        "notes": "",
                        "expense_nature": (request.form.get("expense_nature") or "").strip() or None,
                    }]
        if amount <= 0:
            flash("يرجى إدخال مبلغ أكبر من صفر", "danger")
            return redirect(url_for("custody_settlements"))

        entity_name = (request.form.get("entity_name") or "").strip()
        entity_type = (request.form.get("entity_type") or "").strip()
        entity_account_id = as_int(request.form.get("entity_account_id"))
        entity_account = ChartOfAccount.query.get(entity_account_id) if entity_account_id else None

        # إنشاء صاحب عهدة جديد (سائق/مندوب/معدة) مباشرة من الشاشة
        new_owner_name = (request.form.get("new_owner_name") or "").strip()
        new_owner_type = (request.form.get("new_owner_type") or "").strip()
        if not entity_account and new_owner_name:
            entity_account = get_or_create_custody_owner_account(new_owner_type or "سائق", new_owner_name)
            if entity_account:
                db.session.commit()
                entity_account_id = entity_account.id
                entity_type = new_owner_type or entity_type
                entity_name = entity_name or entity_account.name

        if not entity_account and entity_name:
            entity_account = ChartOfAccount.query.filter_by(name=entity_name).first()
            if entity_account:
                entity_account_id = entity_account.id
        if not entity_account_id:
            flash("يرجى اختيار اسم صاحب عهدة من الحسابات المتاحة أو إضافة صاحب عهدة جديد", "danger")
            return redirect(url_for("custody_settlements", operation_type=operation_type))

        if not entity_type and entity_account:
            entity_type = get_custody_owner_type(entity_account) or "سائق"
        if entity_type not in CUSTODY_OWNER_TYPES:
            entity_type = "سائق"
        if not entity_name and entity_account:
            entity_name = (entity_account.name or "").strip()

        expense_nature = (request.form.get("expense_nature") or "").strip()
        settlement = CustodySettlement(
            date=request.form.get("date") or date.today().isoformat(),
            project_id=as_int(request.form.get("project_id")),
            entity_type=entity_type,
            entity_name=entity_name,
            expense_item=(request.form.get("expense_item") or "").strip() or None,
            expense_nature=expense_nature if expense_nature in CUSTODY_EXPENSE_NATURES else None,
            voucher_type="رد" if operation_type == "رد باقي عهدة" else "صرف",
            operation_type=operation_type,
            reference=(request.form.get("reference") or "").strip() or None,
            treasury_account_id=as_int(request.form.get("treasury_account_id")) or default_treasury_account_id,
            entity_account_id=entity_account_id,
            amount=amount,
            settlement_lines=json.dumps(settlement_lines, ensure_ascii=False, default=str),
            notes=request.form.get("notes"),
        )
        if settlement.treasury_account_id and not user_can_see_account(settlement.treasury_account_id):
            flash("ليست لديك صلاحية استخدام هذه الخزنة", "danger")
            return redirect(url_for("custody_settlements"))
        try:
            assert_period_open(settlement.date)
        except PeriodClosedError as exc:
            flash(str(exc), "danger")
            return redirect(url_for("custody_settlements"))
        db.session.add(settlement)
        try:
            persist_synced(lambda: sync_custody_settlement_journal(settlement))
        except PeriodClosedError as exc:
            db.session.rollback()
            flash(str(exc), "danger")
            return redirect(url_for("custody_settlements"))

        owner_settlements = CustodySettlement.query.filter_by(entity_account_id=entity_account_id).all()
        owner_balances = build_custody_balances(owner_settlements)
        remaining = owner_balances[0]["remaining"] if owner_balances else 0.0
        flash(
            f"تم حفظ {operation_type} بمبلغ {format_grouped_number(amount)} - "
            f"المتبقي من العهدة: {format_grouped_number(remaining)}",
            "success",
        )
        return redirect(url_for(
            "custody_settlements",
            search_name=(settlement.entity_name or "").strip(),
            operation_type=request.args.get("operation_type") or "",
        ))

    all_items = CustodySettlement.query.order_by(CustodySettlement.date.asc(), CustodySettlement.id.asc()).all()
    all_accounts = ChartOfAccount.query.order_by(ChartOfAccount.category, ChartOfAccount.code).all()

    active_entity_account = None
    if search_name:
        search_lower = search_name.lower()
        active_entity_account = next((account for account in all_accounts if search_lower in (account.name or "").lower()), None)

    account_balances = build_account_balances(all_accounts)
    all_owner_rows = build_custody_owner_rows(all_accounts, all_items, account_balances)
    selected_owner = None
    if active_entity_account:
        selected_owner = next((row for row in all_owner_rows if row["account"].id == active_entity_account.id), None)

    selected_entity_name = selected_owner["entity_name"] if selected_owner else search_name
    selected_entity_type = selected_owner["entity_type"] if selected_owner else "سائق"

    selected_rows = []
    if selected_owner:
        running_balance = 0.0
        for item in selected_owner["settlements"]:
            settlement = item["settlement"]
            op_type = item["operation_type"]
            if operation_filter and operation_filter != op_type:
                continue
            if from_date and (settlement.date or "") < from_date:
                continue
            if to_date and (settlement.date or "") > to_date:
                continue

            amount = as_float(item["amount"])
            debit = amount if op_type in CUSTODY_DISBURSE_OPERATIONS else 0.0
            credit = amount if op_type in ("تسوية عهدة", "رد باقي عهدة") else 0.0
            running_balance += debit - credit

            selected_rows.append({
                "item": settlement,
                "operation_type": op_type,
                "amount": amount,
                "debit": debit,
                "credit": credit,
                "running_balance": round(running_balance, 2),
            })

    selected_totals = {
        "spent": round(sum(row["debit"] for row in selected_rows), 2),
        "settled": round(sum(row["amount"] for row in selected_rows if row["operation_type"] == "تسوية عهدة"), 2),
        "returned": round(sum(row["amount"] for row in selected_rows if row["operation_type"] == "رد باقي عهدة"), 2),
        "balance": selected_owner["balance"] if selected_owner else 0.0,
        "remaining": selected_owner["remaining"] if selected_owner else 0.0,
        "count": len(selected_rows),
    }

    owner_summaries = all_owner_rows
    all_entity_names = [row["entity_name"] for row in all_owner_rows]

    return render_template(
        "custody_settlements.html",
        items=all_items,
        owner_summaries=owner_summaries,
        selected_rows=selected_rows,
        selected_entity_type=selected_entity_type,
        selected_entity_name=selected_entity_name,
        selected_totals=selected_totals,
        all_entity_names=all_entity_names,
        projects=projects,
        treasury_accounts=treasury_accounts,
        entity_accounts=entity_accounts,
        expense_accounts=expense_accounts,
        operation_filter=operation_filter,
        from_date=from_date,
        to_date=to_date,
        accounts=accounts,
        owner_types=CUSTODY_OWNER_TYPES,
        expense_natures=CUSTODY_EXPENSE_NATURES,
        operation_types=CUSTODY_OPERATION_TYPES,
        today_date=date.today().isoformat(),
    )


@app.route("/custody_settlements/export")
def export_custody_settlements():
    sync_journal_related_accounts()
    accounts = ChartOfAccount.query.order_by(ChartOfAccount.category, ChartOfAccount.code).all()
    search_name = (request.args.get("search_name") or request.args.get("entity_name") or "").strip()
    operation_filter = (request.args.get("operation_type") or "").strip()
    from_date = (request.args.get("from_date") or "").strip()
    to_date = (request.args.get("to_date") or "").strip()

    all_items = CustodySettlement.query.order_by(CustodySettlement.date.asc(), CustodySettlement.id.asc()).all()
    account_balances = build_account_balances(accounts)
    owner_rows = build_custody_owner_rows(accounts, all_items, account_balances)
    search_lower = search_name.lower()
    active_entity_account = next((account for account in accounts if search_lower in (account.name or "").lower()), None)
    selected_owner = None
    if active_entity_account:
        selected_owner = next((row for row in owner_rows if row["account"].id == active_entity_account.id), None)

    selected_rows = []
    if selected_owner:
        running_balance = 0.0
        for item in selected_owner["settlements"]:
            settlement = item["settlement"]
            op_type = item["operation_type"]
            if operation_filter and operation_filter != op_type:
                continue
            if from_date and (settlement.date or "") < from_date:
                continue
            if to_date and (settlement.date or "") > to_date:
                continue

            amount = as_float(item["amount"])
            debit = amount if op_type in CUSTODY_DISBURSE_OPERATIONS else 0.0
            credit = amount if op_type in ("تسوية عهدة", "رد باقي عهدة") else 0.0
            running_balance += debit - credit

            selected_rows.append({
                "settlement": settlement,
                "operation_type": op_type,
                "debit": debit,
                "credit": credit,
                "running_balance": running_balance,
            })

    final_balance = selected_owner["balance"] if selected_owner else 0.0

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "كشف العهد"

    # Header info
    sheet.append(["كشف حساب العهدة"])
    sheet.append([f"صاحب العهدة: {search_name or 'كل الحسابات'}"])
    sheet.append([f"نوع العملية: {operation_filter or 'كل العمليات'}"])
    sheet.append([f"الفترة: {from_date or '-'} إلى {to_date or '-'}"])
    sheet.append([])

    header_row = 6
    headers = ["رقم العملية", "نوع العملية", "المرجع", "التاريخ", "اسم صاحب العهدة", "المدين", "الدائن", "الرصيد", "ملاحظات"]
    sheet.append(headers)

    for row in selected_rows:
        settlement = row["settlement"]
        sheet.append([
            f"CUS-{settlement.id:06d}",
            row["operation_type"],
            settlement.reference or "-",
            settlement.date or "-",
            settlement.entity_name or "-",
            row["debit"],
            row["credit"],
            row["running_balance"],
            settlement.notes or "-",
        ])

    total_row_index = sheet.max_row + 1
    sheet.append(["", "", "", "", "إجمالي الرصيد الفعلي للحساب بعد كل الحركات المالية", "", "", final_balance, ""])

    # Styling
    title_cell = sheet.cell(row=1, column=1)
    title_cell.font = Font(bold=True, size=14)
    title_cell.alignment = Alignment(horizontal="right")

    header_fill = PatternFill("solid", fgColor="D9E1F2")
    for col in range(1, len(headers) + 1):
        cell = sheet.cell(row=header_row, column=col)
        cell.font = Font(bold=True)
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center")

    for row_idx in range(header_row + 1, total_row_index):
        sheet.cell(row=row_idx, column=6).number_format = "#,##0.00"
        sheet.cell(row=row_idx, column=7).number_format = "#,##0.00"
        sheet.cell(row=row_idx, column=8).number_format = "#,##0.00"

    for col, width in {
        1: 16,
        2: 16,
        3: 18,
        4: 14,
        5: 28,
        6: 14,
        7: 14,
        8: 14,
        9: 30,
    }.items():
        sheet.column_dimensions[chr(64 + col)].width = width

    sheet.freeze_panes = "A7"

    if selected_rows:
        table_end_row = total_row_index - 1
        table_ref = f"A{header_row}:I{table_end_row}"
        table = Table(displayName="CustodyStatementTable", ref=table_ref)
        table.tableStyleInfo = TableStyleInfo(
            name="TableStyleMedium2",
            showFirstColumn=False,
            showLastColumn=False,
            showRowStripes=True,
            showColumnStripes=False,
        )
        sheet.add_table(table)

    total_fill = PatternFill("solid", fgColor="FFF2CC")
    for col in range(1, len(headers) + 1):
        cell = sheet.cell(row=total_row_index, column=col)
        cell.fill = total_fill
        if col in (5, 8):
            cell.font = Font(bold=True)
    sheet.cell(row=total_row_index, column=8).number_format = "#,##0.00"

    output = BytesIO()
    workbook.save(output)
    output.seek(0)
    filename = f"custody_statement_{(search_name or 'all').replace(' ', '_')}.xlsx"
    return send_file(
        output,
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.route("/custody_settlements/<int:settlement_id>/update", methods=["POST"])
def update_custody_settlement(settlement_id):
    settlement = CustodySettlement.query.get_or_404(settlement_id)
    operation_type = (request.form.get("operation_type") or request.form.get("voucher_type") or settlement.operation_type or "صرف عهدة").strip()
    settlement_lines = parse_custody_settlement_lines(request.form)
    amount = as_float(request.form.get("amount"))
    if operation_type == "تسوية عهدة":
        amount = sum(line["amount"] for line in settlement_lines)
    if amount <= 0:
        flash("يرجى إدخال مبلغ أكبر من صفر", "danger")
        return redirect(url_for("custody_settlements"))

    settlement.date = request.form.get("date") or settlement.date
    settlement.project_id = as_int(request.form.get("project_id"))
    settlement.entity_type = request.form.get("entity_type") or settlement.entity_type
    settlement.entity_name = (request.form.get("entity_name") or "").strip()
    settlement.expense_item = (request.form.get("expense_item") or "").strip() or None
    expense_nature = (request.form.get("expense_nature") or "").strip()
    settlement.expense_nature = expense_nature if expense_nature in CUSTODY_EXPENSE_NATURES else None
    settlement.voucher_type = "رد" if operation_type == "رد باقي عهدة" else "صرف"
    settlement.operation_type = operation_type
    settlement.reference = (request.form.get("reference") or "").strip() or None
    settlement.treasury_account_id = as_int(request.form.get("treasury_account_id")) or settlement.treasury_account_id
    settlement.entity_account_id = as_int(request.form.get("entity_account_id")) or settlement.entity_account_id
    settlement.amount = amount
    settlement.settlement_lines = json.dumps(settlement_lines, ensure_ascii=False, default=str)
    settlement.notes = request.form.get("notes")
    try:
        assert_period_open(settlement.date)
        persist_synced(lambda: sync_custody_settlement_journal(settlement))
    except PeriodClosedError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
        return redirect(url_for("custody_settlements"))
    flash("تم تحديث مستند العهد بنجاح", "success")
    return redirect(url_for("custody_settlements"))


@app.route("/labor", methods=["GET", "POST"])
def labor():
    projects = Project.query.order_by(Project.code).all()
    if request.method == "POST":
        labor = LaborEntry(
            project_id=as_int(request.form.get("project_id")),
            date=request.form.get("date") or date.today().isoformat(),
            description=request.form.get("description"),
            hours=as_float(request.form.get("hours")),
            amount=as_float(request.form.get("amount")),
            advances=as_float(request.form.get("advances")),
            deductions=as_float(request.form.get("deductions")),
        )
        try:
            assert_period_open(labor.date)
        except PeriodClosedError as exc:
            flash(str(exc), "danger")
            return redirect(url_for("labor"))
        db.session.add(labor)
        try:
            persist_synced(lambda: sync_labor_journal(labor))
        except PeriodClosedError as exc:
            db.session.rollback()
            flash(str(exc), "danger")
            return redirect(url_for("labor"))
        flash("تم تسجيل العمالة وإنشاء القيد التلقائي", "success")
        return redirect(url_for("labor"))
    entries = LaborEntry.query.order_by(LaborEntry.date.desc()).all()
    return render_template("labor.html", entries=entries, projects=projects, today_date=date.today().isoformat())


def _sheet_amounts_from_form(form):
    amounts = {}
    for key in DRIVER_SHEET_AMOUNT_KEYS:
        amounts[key] = as_float(form.get(f"sheet_{key}") or form.get(key))
    return amounts


def _fill_driver_sheet(entry, form):
    driver_name = (form.get("driver_name") or "").strip()
    paid_amount = as_float(form.get("paid_amount"))
    treasury_account_id = as_int(form.get("treasury_account_id"))
    if not driver_name:
        return "يرجى إدخال اسم السائق"
    if paid_amount < 0:
        return "لا يمكن إدخال قيمة سداد سالبة"
    if paid_amount > 0 and not treasury_account_id:
        return "يرجى اختيار حساب الخزنة عند إدخال مبلغ مسدد"
    if treasury_account_id and not user_can_see_account(treasury_account_id):
        return "ليست لديك صلاحية استخدام هذه الخزنة"

    entry.date = (form.get("date") or "").strip() or date.today().isoformat()
    entry.project_id = as_int(form.get("project_id")) or None
    entry.driver_name = driver_name
    entry.driver_code = (form.get("driver_code") or "").strip() or None
    entry.national_id = (form.get("national_id") or "").strip() or None
    entry.phone = (form.get("phone") or "").strip() or None
    entry.vehicle_type = (form.get("vehicle_type") or "").strip() or None
    entry.tractor_number = (form.get("tractor_number") or "").strip() or None
    entry.period_from = (form.get("period_from") or "").strip() or None
    entry.period_to = (form.get("period_to") or "").strip() or None
    entry.accountant_name = (form.get("accountant_name") or "").strip() or None
    entry.accounts_officer = (form.get("accounts_officer") or "").strip() or None
    entry.reference = (form.get("reference") or "").strip() or None
    entry.notes = (form.get("notes") or "").strip() or None
    entry.paid_amount = paid_amount
    entry.treasury_account_id = treasury_account_id or None
    entry.details_json = json.dumps(_sheet_amounts_from_form(form), ensure_ascii=False, default=str)
    entry.settlement_basis = "كشف"
    entry.recalculate()
    return None


def _driver_sheet_entitlements(item):
    return as_float(getattr(item, "entitlements_total", 0)) or as_float(item.gross_amount)


def _driver_sheet_net(item):
    if getattr(item, "details_json", None) or (item.settlement_basis or "") == "كشف":
        return as_float(getattr(item, "net_amount", 0))
    return _driver_sheet_entitlements(item) - as_float(getattr(item, "deductions_total", 0))


def _driver_entries_query():
    from_date = (request.args.get("from_date") or "").strip()
    to_date = (request.args.get("to_date") or "").strip()
    driver_filter = (request.args.get("driver_name") or "").strip()
    query = DriverCompensationEntry.query
    if from_date:
        query = query.filter(DriverCompensationEntry.date >= from_date)
    if to_date:
        query = query.filter(DriverCompensationEntry.date <= to_date)
    if driver_filter:
        query = query.filter(DriverCompensationEntry.driver_name == driver_filter)
    return query.order_by(DriverCompensationEntry.date.desc(), DriverCompensationEntry.id.desc()), {
        "from_date": from_date,
        "to_date": to_date,
        "driver_name": driver_filter,
    }


def _driver_report_totals(entries):
    entitlements = round(sum(_driver_sheet_entitlements(item) for item in entries), 2)
    deductions = round(sum(as_float(getattr(item, "deductions_total", 0)) for item in entries), 2)
    net = round(sum(_driver_sheet_net(item) for item in entries), 2)
    paid = round(sum(as_float(item.paid_amount) for item in entries), 2)
    return {
        "count": len(entries),
        "entitlements": entitlements,
        "deductions": deductions,
        "net": net,
        "paid": paid,
        "remaining": round(net - paid, 2),
    }


def _driver_page_context():
    projects = Project.query.order_by(Project.code).all()
    accounts = ChartOfAccount.query.order_by(ChartOfAccount.category, ChartOfAccount.code).all()
    treasury_accounts = filter_hidden_treasuries(
        [account for account in accounts if is_treasury_account(account)]
    )
    query, filters = _driver_entries_query()
    entries = query.all()
    from_date = filters["from_date"]
    to_date = filters["to_date"]
    driver_filter = filters["driver_name"]

    driver_records = list_driver_records()
    all_driver_names = [item["name"] for item in driver_records]
    equipment_names = list_equipment_names()

    summary_map = defaultdict(lambda: {
        "driver_name": "",
        "sheets": 0,
        "sales_trips": 0.0,
        "sales_distance": 0.0,
        "entitlements": 0.0,
        "deductions": 0.0,
        "net": 0.0,
        "paid_total": 0.0,
        "due_total": 0.0,
        "gross_total": 0.0,
    })
    for item in entries:
        row = summary_map[item.driver_name]
        row["driver_name"] = item.driver_name
        row["sheets"] += 1
        entitlements = _driver_sheet_entitlements(item)
        deductions = as_float(getattr(item, "deductions_total", 0))
        net = _driver_sheet_net(item)
        paid = as_float(item.paid_amount)
        row["entitlements"] += entitlements
        row["deductions"] += deductions
        row["net"] += net
        row["paid_total"] += paid
        row["gross_total"] += entitlements
        row["due_total"] = row["net"] - row["paid_total"]

    sales_query = SalesTrip.query
    if from_date:
        sales_query = sales_query.filter(SalesTrip.date >= from_date)
    if to_date:
        sales_query = sales_query.filter(SalesTrip.date <= to_date)
    if driver_filter:
        sales_query = sales_query.filter(SalesTrip.driver_name == driver_filter)
    sales_trips = sales_query.order_by(SalesTrip.date.desc(), SalesTrip.id.desc()).all()
    for trip in sales_trips:
        row = summary_map[trip.driver_name]
        row["driver_name"] = trip.driver_name
        row["sales_trips"] += 1
        row["sales_distance"] += as_float(trip.distance_km)

    summary_rows = sorted(summary_map.values(), key=lambda x: x["driver_name"])
    report_totals = _driver_report_totals(entries)
    totals = {
        "sheets": sum(row["sheets"] for row in summary_rows),
        "entitlements": round(sum(row["entitlements"] for row in summary_rows), 2),
        "deductions": round(sum(row["deductions"] for row in summary_rows), 2),
        "net": round(sum(row["net"] for row in summary_rows), 2),
        "paid": round(sum(row["paid_total"] for row in summary_rows), 2),
        "due": round(sum(row["due_total"] for row in summary_rows), 2),
        "gross": round(sum(row["gross_total"] for row in summary_rows), 2),
        "sales_trips": round(sum(row.get("sales_trips", 0) for row in summary_rows), 2),
        "sales_distance": round(sum(row.get("sales_distance", 0) for row in summary_rows), 2),
        **report_totals,
    }
    return {
        "entries": entries,
        "summary_rows": summary_rows,
        "totals": totals,
        "projects": projects,
        "treasury_accounts": treasury_accounts,
        "all_driver_names": all_driver_names,
        "driver_records": driver_records,
        "equipment_names": equipment_names,
        "from_date": from_date,
        "to_date": to_date,
        "driver_filter": driver_filter,
        "filters": filters,
        "today_date": date.today().isoformat(),
        "sales_trips": sales_trips,
        "account_fields": DRIVER_ACCOUNT_FIELDS,
        "transport_fields": DRIVER_TRANSPORT_FIELDS,
        "sundry_fields": DRIVER_SUNDRY_FIELDS,
        "deduction_fields": DRIVER_DEDUCTION_FIELDS,
        "vehicle_types": DRIVER_VEHICLE_TYPES,
        "weekday_of": arabic_weekday,
    }


@app.route("/driver_compensation", methods=["GET", "POST"])
def driver_compensation():
    sync_journal_related_accounts()
    if request.method == "POST":
        entry = DriverCompensationEntry(driver_name="-", settlement_basis="كشف")
        error = _fill_driver_sheet(entry, request.form)
        if error:
            flash(error, "danger")
            return redirect(url_for("driver_compensation"))
        try:
            assert_period_open(entry.date)
        except PeriodClosedError as exc:
            flash(str(exc), "danger")
            return redirect(url_for("driver_compensation"))
        db.session.add(entry)
        try:
            persist_synced(lambda: sync_driver_compensation_journals(entry))
        except PeriodClosedError as exc:
            db.session.rollback()
            flash(str(exc), "danger")
            return redirect(url_for("driver_compensation"))
        flash("تم حفظ كشف حساب السائق بنجاح", "success")
        return redirect(url_for("driver_compensation"))

    context = _driver_page_context()
    edit_id = as_int(request.args.get("edit"))
    context["edit_entry"] = DriverCompensationEntry.query.get(edit_id) if edit_id else None
    return render_template("driver_compensation.html", **context)


@app.route("/driver_compensation/weekly", methods=["POST"])
def driver_compensation_weekly():
    sync_journal_related_accounts()
    driver_name = (request.form.get("driver_name") or "").strip()
    week_days = as_float(request.form.get("week_days"))
    daily_rate = as_float(request.form.get("daily_rate"))
    paid_amount = as_float(request.form.get("paid_amount"))
    treasury_account_id = as_int(request.form.get("treasury_account_id"))
    gross_amount = max(week_days * daily_rate, 0.0)

    if not driver_name:
        flash("يرجى إدخال اسم السائق للتسوية الأسبوعية", "danger")
        return redirect(url_for("driver_compensation"))
    if week_days <= 0 or daily_rate < 0:
        flash("يرجى إدخال عدد أيام وسعر يومية صحيحين", "danger")
        return redirect(url_for("driver_compensation"))
    if paid_amount < 0:
        flash("لا يمكن إدخال مبلغ سداد سالب", "danger")
        return redirect(url_for("driver_compensation"))
    if paid_amount > 0 and not treasury_account_id:
        flash("يرجى اختيار حساب الخزنة عند إدخال مبلغ مسدد", "danger")
        return redirect(url_for("driver_compensation"))
    if treasury_account_id and not user_can_see_account(treasury_account_id):
        flash("ليست لديك صلاحية استخدام هذه الخزنة", "danger")
        return redirect(url_for("driver_compensation"))

    entry = DriverCompensationEntry(
        date=request.form.get("date") or date.today().isoformat(),
        project_id=as_int(request.form.get("project_id")),
        driver_name=driver_name,
        settlement_basis="يومية",
        units=week_days,
        unit_rate=daily_rate,
        gross_amount=gross_amount,
        paid_amount=paid_amount,
        treasury_account_id=treasury_account_id,
        reference=(request.form.get("reference") or "").strip() or None,
        notes=(request.form.get("notes") or "").strip() or "تسوية أسبوعية",
    )
    try:
        assert_period_open(entry.date)
    except PeriodClosedError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("driver_compensation"))
    db.session.add(entry)
    try:
        persist_synced(lambda: sync_driver_compensation_journals(entry))
    except PeriodClosedError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
        return redirect(url_for("driver_compensation"))
    flash("تم حفظ التسوية الأسبوعية للسائق بنجاح", "success")
    return redirect(url_for("driver_compensation"))


@app.route("/driver_compensation/<int:entry_id>/update", methods=["POST"])
def update_driver_compensation(entry_id):
    entry = DriverCompensationEntry.query.get_or_404(entry_id)
    original_date = entry.date
    error = _fill_driver_sheet(entry, request.form)
    if error:
        flash(error, "danger")
        return redirect(url_for("driver_compensation", edit=entry_id))
    try:
        assert_period_open(original_date)
        assert_period_open(entry.date)
        persist_synced(lambda: sync_driver_compensation_journals(entry))
    except PeriodClosedError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
        return redirect(url_for("driver_compensation", edit=entry_id))
    flash("تم تحديث كشف حساب السائق بنجاح", "success")
    return redirect(url_for("driver_compensation"))


@app.route("/driver_compensation/print")
def print_driver_compensation():
    query, filters = _driver_entries_query()
    entries = query.all()
    stamp = _print_stamp("تقرير محاسبة السواقين", "قسم الحسابات", "محاسب السائقين")
    return render_template(
        "print_driver_report.html",
        entries=entries,
        totals=_driver_report_totals(entries),
        filters=filters,
        weekday_of=arabic_weekday,
        **stamp,
    )


@app.route("/driver_compensation/<int:entry_id>/print")
def print_driver_sheet(entry_id):
    entry = DriverCompensationEntry.query.get_or_404(entry_id)
    stamp = _print_stamp("حساب سواقين الجرارات و القلابات", "قسم الحسابات", "محاسب السائقين")
    return render_template(
        "print_driver_sheet.html",
        entry=entry,
        amounts=entry.sheet_amounts(),
        account_fields=DRIVER_ACCOUNT_FIELDS,
        transport_fields=DRIVER_TRANSPORT_FIELDS,
        sundry_fields=DRIVER_SUNDRY_FIELDS,
        deduction_fields=DRIVER_DEDUCTION_FIELDS,
        weekday_of=arabic_weekday,
        **stamp,
    )


@app.route("/labor/<int:labor_id>/update", methods=["POST"])
def update_labor_entry(labor_id):
    entry = LaborEntry.query.get_or_404(labor_id)
    entry.project_id = as_int(request.form.get("project_id")) or entry.project_id
    entry.date = request.form.get("date") or None
    entry.description = request.form.get("description")
    entry.hours = as_float(request.form.get("hours"))
    entry.amount = as_float(request.form.get("amount"))
    entry.advances = as_float(request.form.get("advances"))
    entry.deductions = as_float(request.form.get("deductions"))
    try:
        persist_synced(lambda: sync_labor_journal(entry))
    except PeriodClosedError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
        return redirect(url_for("labor"))
    flash("تم تحديث سجل العمالة والقيد التلقائي", "success")
    return redirect(url_for("labor"))


@app.route("/equipment", methods=["GET", "POST"])
def equipment():
    projects = Project.query.order_by(Project.code).all()
    if request.method == "POST":
        equip = Equipment(
            name=request.form.get("name"),
            code=(request.form.get("code") or "").strip() or None,
            plate_number=(request.form.get("plate_number") or "").strip() or None,
            chassis_number=(request.form.get("chassis_number") or "").strip() or None,
            vehicle_type=(request.form.get("vehicle_type") or "").strip() or None,
            status=(request.form.get("status") or "").strip() or "تعمل بالموقع",
            license_expiry=(request.form.get("license_expiry") or "").strip() or None,
            purchase_cost=as_float(request.form.get("purchase_cost")),
            operating_cost=as_float(request.form.get("operating_cost")),
            maintenance=as_float(request.form.get("maintenance")),
            hours_used=as_float(request.form.get("hours_used")),
            current_meter=as_float(request.form.get("current_meter")),
            meter_unit=(request.form.get("meter_unit") or "").strip() or "كم",
            last_oil_meter=as_float(request.form.get("last_oil_meter")),
            oil_interval=as_float(request.form.get("oil_interval")) or 10000,
            oil_tolerance=as_float(request.form.get("oil_tolerance")) or 1000,
            project_id=as_int(request.form.get("project_id")),
        )
        db.session.add(equip)
        try:
            persist_synced(
                sync_journal_related_accounts,
                lambda: sync_equipment_journals(equip),
            )
        except PeriodClosedError as exc:
            db.session.rollback()
            flash(str(exc), "danger")
            return redirect(url_for("equipment"))
        flash("تم حفظ المعدة وإنشاء قيود الشراء والتشغيل", "success")
        return redirect(url_for("equipment"))
    entries = Equipment.query.order_by(Equipment.name).all()
    return render_template(
        "equipment.html",
        entries=entries,
        projects=projects,
        equipment_statuses=EQUIPMENT_STATUSES,
        meter_units=METER_UNITS,
    )


@app.route("/equipment/<int:equipment_id>/update", methods=["POST"])
def update_equipment(equipment_id):
    item = Equipment.query.get_or_404(equipment_id)
    item.name = request.form.get("name")
    item.code = (request.form.get("code") or "").strip() or None
    item.plate_number = (request.form.get("plate_number") or "").strip() or None
    item.chassis_number = (request.form.get("chassis_number") or "").strip() or None
    item.vehicle_type = (request.form.get("vehicle_type") or "").strip() or None
    item.status = (request.form.get("status") or "").strip() or item.status or "تعمل بالموقع"
    item.license_expiry = (request.form.get("license_expiry") or "").strip() or None
    item.purchase_cost = as_float(request.form.get("purchase_cost"))
    item.operating_cost = as_float(request.form.get("operating_cost"))
    item.maintenance = as_float(request.form.get("maintenance"))
    item.hours_used = as_float(request.form.get("hours_used"))
    item.current_meter = as_float(request.form.get("current_meter")) if request.form.get("current_meter") is not None else item.current_meter
    item.meter_unit = (request.form.get("meter_unit") or "").strip() or item.meter_unit or "كم"
    item.last_oil_meter = as_float(request.form.get("last_oil_meter")) if request.form.get("last_oil_meter") is not None else item.last_oil_meter
    if request.form.get("oil_interval") not in (None, ""):
        item.oil_interval = as_float(request.form.get("oil_interval")) or 10000
    if request.form.get("oil_tolerance") not in (None, ""):
        item.oil_tolerance = as_float(request.form.get("oil_tolerance")) or 1000
    item.project_id = as_int(request.form.get("project_id"))
    try:
        persist_synced(
            sync_journal_related_accounts,
            lambda: sync_equipment_journals(item),
        )
    except PeriodClosedError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
        return redirect(url_for("equipment"))
    flash("تم تحديث المعدة وقيودها التلقائية", "success")
    return redirect(url_for("equipment"))


@app.route("/equipment/cycle", methods=["GET", "POST"])
def equipment_cycle():
    sync_journal_related_accounts()
    accounts = ChartOfAccount.query.order_by(ChartOfAccount.code).all()
    treasury_accounts = filter_hidden_treasuries(
        [account for account in accounts if is_treasury_account(account)]
    )
    if request.method == "POST":
        equipment_id = as_int(request.form.get("equipment_id"))
        item = Equipment.query.get(equipment_id) if equipment_id else None
        action = (request.form.get("action") or "log_expense").strip()
        tab = (request.form.get("tab") or request.args.get("tab") or "expenses").strip()
        if not item:
            flash("يرجى اختيار المعدة", "danger")
            return redirect(url_for("equipment_cycle", tab=tab))
        if action == "update_tracking":
            item.current_meter = as_float(request.form.get("current_meter"))
            item.last_oil_meter = as_float(request.form.get("last_oil_meter"))
            item.oil_interval = as_float(request.form.get("oil_interval")) or 10000
            item.oil_tolerance = as_float(request.form.get("oil_tolerance")) or 1000
            item.meter_unit = (request.form.get("meter_unit") or "").strip() or item.meter_unit or "كم"
            item.license_expiry = (request.form.get("license_expiry") or "").strip() or None
            item.status = (request.form.get("status") or "").strip() or item.status or "تعمل بالموقع"
            item.chassis_number = (request.form.get("chassis_number") or "").strip() or item.chassis_number
            item.vehicle_type = (request.form.get("vehicle_type") or "").strip() or item.vehicle_type
            if request.form.get("purchase_cost") not in (None, ""):
                item.purchase_cost = as_float(request.form.get("purchase_cost"))
                try:
                    persist_synced(
                        sync_journal_related_accounts,
                        lambda: sync_equipment_journals(item),
                    )
                except PeriodClosedError as exc:
                    db.session.rollback()
                    flash(str(exc), "danger")
                    return redirect(url_for("equipment_cycle", equipment_id=item.id, tab=tab))
            else:
                db.session.commit()
            flash(f"تم تحديث بيانات {item.name}", "success")
            return redirect(url_for("equipment_cycle", equipment_id=item.id, tab=tab if tab in {"pl", "tracking"} else "tracking"))
        quantity, unit_price, amount = _maintenance_amounts_from_form()
        if amount <= 0:
            flash("يرجى إدخال الكمية والسعر أو إجمالي أكبر من صفر", "danger")
            return redirect(url_for("equipment_cycle", equipment_id=item.id, tab="expenses"))
        work_date = request.form.get("date") or date.today().isoformat()
        try:
            assert_period_open(work_date)
        except PeriodClosedError as exc:
            flash(str(exc), "danger")
            return redirect(url_for("equipment_cycle", equipment_id=item.id, tab="expenses"))
        row = EquipmentMaintenance(
            equipment_id=item.id,
            date=work_date,
            work_type=(request.form.get("work_type") or "").strip() or "صيانة",
            description=(request.form.get("description") or "").strip() or None,
            quantity=quantity,
            unit_price=unit_price,
            amount=amount,
            source_type="خزنة",
            vendor_name=(request.form.get("vendor_name") or "").strip() or None,
            treasury_account_id=as_int(request.form.get("treasury_account_id")),
            next_due=(request.form.get("next_due") or "").strip() or None,
            notes=request.form.get("notes"),
        )
        db.session.add(row)
        try:
            persist_synced(lambda: sync_equipment_maintenance_journal(row))
        except PeriodClosedError as exc:
            db.session.rollback()
            flash(str(exc), "danger")
            return redirect(url_for("equipment_cycle", equipment_id=item.id, tab="expenses"))
        flash(f"تم تسجيل مصروف {item.name} من الخزنة في سجل المصاريف والصيانة", "success")
        return redirect(url_for("equipment_cycle", equipment_id=item.id, tab="expenses"))

    bundle = build_equipment_cycle_bundle()
    rows = bundle["rows"]
    selected_id = as_int(request.args.get("equipment_id"))
    selected = next((row for row in rows if row["equipment"].id == selected_id), None)
    active_tab = (request.args.get("tab") or "works").strip()
    if active_tab not in {"works", "expenses", "pl", "tracking"}:
        active_tab = "works"
    return render_template(
        "equipment_cycle.html",
        rows=rows,
        selected=selected,
        works=bundle["works"],
        expenses=bundle["expenses"],
        totals=bundle["totals"],
        treasury_accounts=treasury_accounts,
        maintenance_kinds=MAINTENANCE_KINDS,
        equipment_statuses=EQUIPMENT_STATUSES,
        meter_units=METER_UNITS,
        today_date=date.today().isoformat(),
        active_tab=active_tab,
    )


def _maintenance_amounts_from_form():
    quantity = as_float(request.form.get("quantity")) or 1.0
    unit_price = as_float(request.form.get("unit_price"))
    amount = as_float(request.form.get("amount"))
    if amount <= 0 and quantity > 0 and unit_price > 0:
        amount = round(quantity * unit_price, 2)
    if unit_price <= 0 and quantity > 0 and amount > 0:
        unit_price = round(amount / quantity, 2)
    return quantity, unit_price, amount


@app.route("/equipment/maintenance/<int:log_id>/update", methods=["POST"])
def update_equipment_maintenance(log_id):
    row = EquipmentMaintenance.query.get_or_404(log_id)
    equipment_id = as_int(request.form.get("equipment_id")) or row.equipment_id
    item = Equipment.query.get(equipment_id)
    if not item:
        flash("يرجى اختيار المعدة", "danger")
        return redirect(url_for("equipment_cycle", tab="expenses"))
    quantity, unit_price, amount = _maintenance_amounts_from_form()
    if amount <= 0:
        flash("يرجى إدخال الكمية والسعر أو إجمالي أكبر من صفر", "danger")
        return redirect(url_for("equipment_cycle", equipment_id=item.id, tab="expenses"))
    work_date = request.form.get("date") or date.today().isoformat()
    try:
        assert_period_open(row.date)
        assert_period_open(work_date)
    except PeriodClosedError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("equipment_cycle", equipment_id=item.id, tab="expenses"))
    row.equipment_id = item.id
    row.date = work_date
    row.work_type = (request.form.get("work_type") or "").strip() or "صيانة"
    row.description = (request.form.get("description") or "").strip() or None
    row.quantity = quantity
    row.unit_price = unit_price
    row.amount = amount
    row.vendor_name = (request.form.get("vendor_name") or "").strip() or None
    row.treasury_account_id = as_int(request.form.get("treasury_account_id"))
    incoming_notes = request.form.get("notes") or ""
    if is_journal_sourced_document(row.notes) and not is_journal_sourced_document(incoming_notes):
        row.notes = f"{row.notes or ''}\n{incoming_notes}".strip()
    else:
        row.notes = incoming_notes or row.notes
    try:
        persist_synced(lambda: sync_equipment_maintenance_journal(row))
    except PeriodClosedError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
        return redirect(url_for("equipment_cycle", equipment_id=item.id, tab="expenses"))
    flash("تم تعديل مصروف الصيانة وقيده", "success")
    return redirect(url_for("equipment_cycle", equipment_id=item.id, tab="expenses"))


@app.route("/equipment/cycle/works/<int:trip_id>/update", methods=["POST"])
def update_equipment_work(trip_id):
    trip = SalesTrip.query.get_or_404(trip_id)
    equipment_id = as_int(request.form.get("equipment_id"))
    old_date = trip.date
    trip.date = (request.form.get("date") or "").strip() or trip.date or date.today().isoformat()
    trip.origin = (request.form.get("origin") or "").strip() or None
    trip.destination = (request.form.get("destination") or "").strip() or None
    trip.client_name = (request.form.get("client_name") or "").strip() or trip.client_name
    trip.voucher_number = (request.form.get("voucher_number") or "").strip() or trip.voucher_number
    role = (request.form.get("role") or "").strip()
    if role == "قلاب":
        trip.dump_cubage = as_float(request.form.get("dump_cubage"))
    elif role == "مقطورة":
        trip.trailer_cubage = as_float(request.form.get("trailer_cubage"))
    if request.form.get("unit_price") not in (None, ""):
        trip.unit_price = as_float(request.form.get("unit_price"))
    trip.recalculate()
    try:
        assert_period_open(old_date)
        assert_period_open(trip.date)
        persist_synced(lambda: sync_sales_trip_journal(trip))
    except PeriodClosedError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
        return redirect(url_for("equipment_cycle", equipment_id=equipment_id, tab="works"))
    flash("تم تعديل بيان الأعمال", "success")
    return redirect(url_for("equipment_cycle", equipment_id=equipment_id, tab="works"))


@app.route("/equipment/cycle/expense/unlink", methods=["POST"])
def unlink_equipment_expense():
    kind = (request.form.get("source_kind") or "").strip()
    source_id = as_int(request.form.get("source_id"))
    equipment_id = as_int(request.form.get("equipment_id"))
    if kind == "inventory":
        row = InventoryTransaction.query.get_or_404(source_id)
        row.equipment_id = None
    elif kind == "purchase":
        row = PurchaseOrder.query.get_or_404(source_id)
        row.equipment_id = None
    else:
        flash("لا يمكن حذف هذا البند من هنا", "danger")
        return redirect(url_for("equipment_cycle", equipment_id=equipment_id, tab="expenses"))
    db.session.commit()
    flash("تم حذف البند من سجل المصاريف والصيانة", "success")
    return redirect(url_for("equipment_cycle", equipment_id=equipment_id, tab="expenses"))


@app.route("/equipment/maintenance/<int:log_id>/delete", methods=["POST"])
def delete_equipment_maintenance(log_id):
    row = EquipmentMaintenance.query.get_or_404(log_id)
    equipment_id = row.equipment_id
    try:
        void_document_journals("equipment_maintenance", row.id, commit=False)
        db.session.delete(row)
        db.session.commit()
    except PeriodClosedError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
        return redirect(url_for("equipment_cycle", equipment_id=equipment_id))
    flash("تم حذف ورقة الصيانة وقيدها", "success")
    return redirect(url_for("equipment_cycle", equipment_id=equipment_id, tab="expenses"))


@app.route("/equipment/cycle/export")
def export_equipment_cycle():
    from services.workbook import excel_download_sheets
    bundle = build_equipment_cycle_bundle()
    work_headers = ["التاريخ", "كود المعدة", "اسم المعدة", "الدور", "الموقع", "العميل", "رقم البون", "المبلغ"]
    work_rows = [[
        item["date"], item["equipment_code"], item["equipment_name"], item["role"],
        item["site"], item["client_name"], item.get("voucher") or "", item["amount"],
    ] for item in bundle["works"]]
    expense_headers = ["التاريخ", "كود المعدة", "اسم المعدة", "بند المصروف", "البيان", "الكمية", "السعر", "الإجمالي", "المصدر"]
    expense_rows = [[
        item["date"], item["equipment_code"], item["equipment_name"], item["kind"],
        item["description"], item["quantity"], item["unit_price"], item["amount"], item["source"],
    ] for item in bundle["expenses"]]
    pl_headers = ["كود المعدة", "رقم العربية", "نوع السيارة", "الشاسيه", "إجمالي الإيرادات", "إجمالي المصاريف", "تكلفة الشراء", "صافي الربح/الخسارة", "مؤشر الأداء", "الحالة"]
    pl_rows = [[
        row["code"], row["plate_number"], row["vehicle_type"], row["chassis_number"], row["revenue"], row["running_cost"],
        row["purchase_cost"], row["result"], row["performance"]["label"], row["status"],
    ] for row in bundle["rows"]]
    track_headers = [
        "كود المعدة", "اسم المعدة", "العداد الحالي", "وحدة العداد", "عداد آخر تغيير زيت",
        "الحد المسموح به", "عداد التغيير القادم", "المتبقي على تغيير الزيت", "تنبيه الزيوت",
        "تاريخ انتهاء الترخيص", "المتبقي بالأيام", "الحالة",
    ]
    track_rows = []
    for row in bundle["rows"]:
        oil = row["oil"]
        license_row = row["license"]
        track_rows.append([
            row["code"], row["equipment"].name, oil["current_meter"], oil["unit"], oil["last_oil_meter"],
            oil["oil_tolerance"], oil["next_meter"], oil["remaining"] if oil["remaining"] is not None else "",
            oil["alert"], license_row["expiry"], license_row["days"] if license_row["days"] is not None else "",
            row["status"],
        ])
    return excel_download_sheets("دورة المعدات.xlsx", [
        ("سجل المستخلصات والأعمال", work_headers, work_rows),
        ("سجل المصاريف والصيانة", expense_headers, expense_rows),
        ("ملخص الربح والخسارة", pl_headers, pl_rows),
        ("متابعة الزيوت والترخيص", track_headers, track_rows),
    ])


@app.route("/equipment/cycle/print/<tab>")
def print_equipment_cycle(tab):
    titles = {
        "works": "سجل المستخلصات والأعمال",
        "expenses": "سجل المصاريف والصيانة",
        "pl": "ملخص الربح والخسارة",
        "tracking": "متابعة الزيوت والترخيص",
    }
    if tab not in titles:
        abort(404)
    bundle = build_equipment_cycle_bundle()
    if tab == "works":
        columns = ["التاريخ", "كود المعدة", "اسم المعدة", "الدور", "الموقع", "العميل", "رقم البون", "المبلغ"]
        rows = [[
            item["date"] or "-", item["equipment_code"], item["equipment_name"], item["role"],
            item["site"], item["client_name"] or "-", item.get("voucher") or "-", format_grouped_number(item["amount"]),
        ] for item in bundle["works"]]
        kpis = [
            {"label": "عدد الأعمال", "value": str(len(rows))},
            {"label": "إجمالي الإيرادات", "value": format_grouped_number(bundle["totals"]["revenue"])},
        ]
    elif tab == "expenses":
        columns = ["التاريخ", "كود المعدة", "اسم المعدة", "بند المصروف", "البيان", "الكمية", "السعر", "الإجمالي", "المصدر"]
        rows = [[
            item["date"] or "-", item["equipment_code"], item["equipment_name"], item["kind"],
            item["description"], format_qty_number(item["quantity"]), format_pay_input(item["unit_price"]),
            format_grouped_number(item["amount"]), item["source"],
        ] for item in bundle["expenses"]]
        kpis = [
            {"label": "عدد البنود", "value": str(len(rows))},
            {"label": "إجمالي المصاريف", "value": format_grouped_number(bundle["totals"]["running"])},
        ]
    elif tab == "pl":
        columns = ["كود المعدة", "رقم العربية", "نوع السيارة", "الشاسيه", "إجمالي الإيرادات", "إجمالي المصاريف", "تكلفة الشراء", "صافي الربح/الخسارة", "مؤشر الأداء", "الحالة"]
        rows = [[
            row["code"], row["plate_number"], row["vehicle_type"], row["chassis_number"],
            format_grouped_number(row["revenue"]), format_grouped_number(row["running_cost"]),
            format_grouped_number(row["purchase_cost"]),
            f"{row['net_label']} {format_grouped_number(row['result'])}",
            row["performance"]["label"], row["status"],
        ] for row in bundle["rows"]]
        kpis = [
            {"label": "صافي النتيجة", "value": format_grouped_number(bundle["totals"]["result"])},
        ]
    else:
        columns = [
            "كود المعدة", "اسم المعدة", "العداد الحالي", "آخر تغيير زيت", "الحد المسموح",
            "التغيير القادم", "المتبقي على الزيت", "تنبيه الزيوت", "انتهاء الترخيص", "المتبقي بالأيام", "الحالة",
        ]
        rows = []
        for row in bundle["rows"]:
            oil = row["oil"]
            license_row = row["license"]
            remaining = "-" if oil["remaining"] is None else format_qty_number(oil["remaining"])
            days = "-" if license_row["days"] is None else str(license_row["days"])
            rows.append([
                row["code"], row["equipment"].name,
                f"{format_qty_number(oil['current_meter'])} {oil['unit']}",
                format_qty_number(oil["last_oil_meter"]), format_qty_number(oil["oil_tolerance"]),
                format_qty_number(oil["next_meter"]), remaining, oil["alert"],
                license_row["expiry"] or "-", days, row["status"],
            ])
        kpis = [{"label": "عدد المعدات", "value": str(len(rows))}]
    return render_template(
        "print_generic.html",
        columns=columns,
        rows=rows,
        totals=kpis,
        filters={},
        **_print_stamp(titles[tab], "قسم المعدات", "مسؤول المعدات"),
    )


@app.route("/equipment/reports")
def equipment_reports():
    sync_journal_related_accounts()
    bundle = build_equipment_cycle_bundle()
    return render_template(
        "equipment_reports.html",
        rows=bundle["rows"],
        totals=bundle["totals"],
        works=bundle["works"],
        expenses=bundle["expenses"],
    )


@app.route("/print/equipment/<int:equipment_id>")
def print_equipment_maintenance(equipment_id):
    item = Equipment.query.get_or_404(equipment_id)
    snapshot = next(
        (row for row in build_equipment_cycle_data() if row["equipment"].id == item.id),
        None,
    )
    return render_template(
        "print_equipment_maintenance.html",
        item=item,
        snapshot=snapshot,
        **_print_stamp(f"ورقة صيانة {item.name}", "قسم المعدات", "مسؤول المعدات"),
    )


# ---------------------------------------------------------------------------
# مقايسات العملاء (SRS 4)
# ---------------------------------------------------------------------------


@app.route("/estimations", methods=["GET", "POST"])
def estimations():
    sync_journal_related_accounts()
    projects = Project.query.order_by(Project.code).all()

    if request.method == "POST":
        client_name = resolve_client_name(request.form)
        if not client_name:
            flash("يرجى اختيار عميل مسجّل أو إدخال اسم عميل جديد", "danger")
            return redirect(url_for("estimations"))

        item_lines = parse_estimation_items(request.form)
        if not item_lines:
            flash("يرجى إدخال بند واحد على الأقل في جدول الأعمال والتوريدات", "danger")
            return redirect(url_for("estimations"))

        project_id = as_int(request.form.get("project_id"))
        project = Project.query.get(project_id) if project_id else None
        admin_mode = request.form.get("admin_mode") or "إضافة"
        estimation = Estimation(
            code=(request.form.get("code") or "").strip() or generate_estimation_code(),
            date=request.form.get("date") or date.today().isoformat(),
            client_name=client_name,
            project_id=project_id,
            project_name=(request.form.get("project_name") or "").strip() or (project.project_name if project else None),
            discount_percentage=as_float(request.form.get("discount_percentage")),
            admin_percentage=as_float(request.form.get("admin_percentage")),
            admin_mode=admin_mode if admin_mode in ("إضافة", "خصم") else "إضافة",
            status="مسودة",
            notes=request.form.get("notes"),
        )
        db.session.add(estimation)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash("رقم المقايسة مستخدم بالفعل، اختر رقمًا مختلفًا", "danger")
            return redirect(url_for("estimations"))

        for line in item_lines:
            db.session.add(EstimationItem(estimation_id=estimation.id, **line))
        db.session.commit()

        recalculate_estimation(estimation)
        db.session.commit()

        flash(
            f"تم حفظ المقايسة {estimation.code}: "
            f"الإجمالي {format_grouped_number(estimation.total_value)} - "
            f"الخصم {format_grouped_number(estimation.discount_value)} - "
            f"الصافي بعد الخصم {format_grouped_number(estimation.net_after_discount)} - "
            f"الإداريات {format_grouped_number(estimation.admin_value)} - "
            f"القيمة النهائية {format_grouped_number(estimation.final_value)}",
            "success",
        )
        return redirect(url_for("estimation_detail", estimation_id=estimation.id))

    items = Estimation.query.order_by(Estimation.id.desc()).all()
    totals = {
        "total_value": round(sum(as_float(item.total_value) for item in items), 2),
        "discount_value": round(sum(as_float(item.discount_value) for item in items), 2),
        "net_after_discount": round(sum(as_float(item.net_after_discount) for item in items), 2),
        "admin_value": round(sum(as_float(item.admin_value) for item in items), 2),
        "final_value": round(sum(as_float(item.final_value) for item in items), 2),
    }
    return render_template(
        "estimations.html",
        items=items,
        projects=projects,
        unit_options=UNIT_OPTIONS,
        next_code=generate_estimation_code(),
        today_date=date.today().isoformat(),
        totals=totals,
        client_names=list_client_names(),
    )


@app.route("/estimations/<int:estimation_id>")
def estimation_detail(estimation_id):
    estimation = Estimation.query.get_or_404(estimation_id)
    items = EstimationItem.query.filter_by(estimation_id=estimation.id).order_by(EstimationItem.id).all()
    projects = Project.query.order_by(Project.code).all()
    linked_journals = JournalEntry.query.filter(
        JournalEntry.description.like(f"%EST-AUTO:{estimation.id}%")
    ).order_by(JournalEntry.id.asc()).all()
    return render_template(
        "estimation_detail.html",
        estimation=estimation,
        items=items,
        projects=projects,
        unit_options=UNIT_OPTIONS,
        linked_journals=linked_journals,
        client_names=list_client_names(),
    )


@app.route("/estimations/<int:estimation_id>/update", methods=["POST"])
def update_estimation(estimation_id):
    estimation = Estimation.query.get_or_404(estimation_id)
    estimation.client_name = resolve_client_name(request.form) or estimation.client_name
    estimation.date = request.form.get("date") or estimation.date
    estimation.project_id = as_int(request.form.get("project_id"))
    estimation.project_name = (request.form.get("project_name") or "").strip() or None
    estimation.discount_percentage = as_float(request.form.get("discount_percentage"))
    estimation.admin_percentage = as_float(request.form.get("admin_percentage"))
    admin_mode = request.form.get("admin_mode") or estimation.admin_mode
    estimation.admin_mode = admin_mode if admin_mode in ("إضافة", "خصم") else estimation.admin_mode
    estimation.notes = request.form.get("notes")

    item_lines = parse_estimation_items(request.form)
    for existing_item in EstimationItem.query.filter_by(estimation_id=estimation.id).all():
        db.session.delete(existing_item)
    db.session.flush()
    for line in item_lines:
        db.session.add(EstimationItem(estimation_id=estimation.id, **line))

    recalculate_estimation(estimation)
    persist_synced(lambda: sync_estimation_journal(estimation))
    flash(
        f"تم تحديث المقايسة - القيمة النهائية {format_grouped_number(estimation.final_value)}",
        "success",
    )
    return redirect(url_for("estimation_detail", estimation_id=estimation.id))


@app.route("/estimations/<int:estimation_id>/status", methods=["POST"])
def update_estimation_status(estimation_id):
    estimation = Estimation.query.get_or_404(estimation_id)
    new_status = (request.form.get("status") or "").strip()
    if new_status not in ("مسودة", "معتمدة", "ملغاة"):
        flash("حالة المقايسة غير صحيحة", "danger")
        return redirect(url_for("estimation_detail", estimation_id=estimation.id))

    estimation.status = new_status
    recalculate_estimation(estimation)
    persist_synced(lambda: sync_estimation_journal(estimation))

    if new_status == "معتمدة" and request.form.get("update_contract_value") == "on" and estimation.project:
        estimation.project.contract_value = as_float(estimation.final_value)
        db.session.commit()
        flash("تم تحديث قيمة عقد المشروع بقيمة المقايسة النهائية", "success")

    flash(f"تم تحويل حالة المقايسة إلى {new_status}", "success")
    return redirect(url_for("estimation_detail", estimation_id=estimation.id))


from routes.alerts import register as register_alerts
from routes.catalog import register as register_catalog
from routes.cycle import register as register_operating_cycle
from routes.exports import register as register_exports
from routes.hr import register as register_hr
from routes.journal import register as register_journal
from routes.reports import register as register_reports
from routes.sales import register as register_sales

register_alerts(app)
register_catalog(app)
register_operating_cycle(app)
register_exports(app)
register_hr(app)
register_journal(app)
register_reports(app)
register_sales(app)
