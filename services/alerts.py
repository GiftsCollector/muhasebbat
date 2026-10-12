from collections import defaultdict
from datetime import date, datetime, timedelta

from flask import g, has_app_context, has_request_context, url_for

from models import (
    AlertDismissal, ChartOfAccount, Employee, EmployeeAttendance, Equipment,
    JournalEntry, LaborEntry, ProgressPayment, Project, PurchaseOrder, db,
)
from services.accounting import (
    as_float, entry_touches_hidden_accounts, format_grouped_number, hidden_account_ids_for_query,
)
from services.authz import ALL, ALIASES, effective_perms

ABSENCE_LIMIT = 5
OVERDUE_DAYS = 30
CONTRACT_WARN_DAYS = 30
COST_WARN_RATIO = 0.9
ABSENT_STATUSES = {"غياب", "إجازة بدون أجر"}
PAYABLE_CATEGORIES = {"الموردين", "موردين", "مقاولي الباطن", "الموظفين"}
RECEIVABLE_CATEGORIES = {"العملاء"}
SEVERITY_ORDER = {"danger": 0, "warning": 1, "info": 2}


def _today():
    return date.today()


def _iso(value):
    return (value or "").strip()[:10]


def _parse_date(value):
    raw = _iso(value)
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _days_between(value, today=None):
    parsed = _parse_date(value)
    if not parsed:
        return None
    return ((today or _today()) - parsed).days


def _user_allows(user, *keys):
    have = effective_perms(user)
    if ALL in have:
        return True
    for key in keys:
        if key in have:
            return True
        for parent, extras in ALIASES.items():
            if parent in have and key in extras:
                return True
    return False


def _safe_url(endpoint, **values):
    if not has_app_context():
        return "/"
    try:
        return url_for(endpoint, **values)
    except Exception:
        return "/"


def _alert(key, kind, severity, title, detail, url, amount=None):
    item = {
        "key": key,
        "kind": kind,
        "severity": severity,
        "title": title,
        "detail": detail,
        "url": url,
    }
    if amount is not None:
        item["amount"] = round(as_float(amount), 2)
    return item


def _absence_alerts(today):
    month = today.strftime("%Y-%m")
    rows = (
        EmployeeAttendance.query.filter(
            EmployeeAttendance.date.like(f"{month}%"),
            EmployeeAttendance.status.in_(ABSENT_STATUSES),
            EmployeeAttendance.record_status != "مسودة",
        )
        .all()
    )
    counts = defaultdict(int)
    for row in rows:
        counts[row.employee_id] += 1
    if not counts:
        return []
    employees = {
        item.id: item
        for item in Employee.query.filter(Employee.id.in_(list(counts))).all()
    }
    alerts = []
    for employee_id, days in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
        if days < ABSENCE_LIMIT:
            continue
        employee = employees.get(employee_id)
        name = employee.display_name if employee else f"موظف #{employee_id}"
        alerts.append(_alert(
            f"absence:{employee_id}:{month}",
            "absence",
            "danger" if days >= 8 else "warning",
            f"{name} غاب {days} أيام هذا الشهر",
            "الحد 5 أيام غياب. افتح تقرير الموظف وراجع الأيام قبل ما الشهر يقفل.",
            _safe_url("employee_report", employee_id=employee_id, from_date=f"{month}-01", to_date=today.isoformat()),
        ))
    return alerts


def _attendance_draft_alerts(today):
    month = today.strftime("%Y-%m")
    previous = (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    alerts = []
    for period, force in ((previous, True), (month, today.day >= 25)):
        if not force:
            continue
        count = EmployeeAttendance.query.filter(
            EmployeeAttendance.date.like(f"{period}%"),
            EmployeeAttendance.record_status == "مسودة",
        ).count()
        if not count:
            continue
        title = (
            f"{count} أيام حضور لسه مسودة من شهر {period}"
            if period != month
            else f"{count} أيام حضور مسودة محتاجة ترحيل قبل قفل الشهر"
        )
        alerts.append(_alert(
            f"attendance_drafts:{period}",
            "attendance_drafts",
            "warning",
            title,
            "المسودة مش بتدخل المرتب. رحّلها من شاشة الحضور لما تتأكد من الأيام.",
            _safe_url("hr_module", period_month=period, tab="attendance"),
        ))
    return alerts


def _journal_draft_alerts(today):
    drafts = JournalEntry.query.filter(JournalEntry.status != "مرحل").all()
    hidden = hidden_account_ids_for_query()
    if hidden:
        drafts = [entry for entry in drafts if not entry_touches_hidden_accounts(entry, hidden)]
    if not drafts:
        return []
    oldest = min((_parse_date(entry.date) or today) for entry in drafts)
    age = (today - oldest).days
    if age < 2 and today.day < 25:
        return []
    return [_alert(
        f"journal_drafts:{today.strftime('%Y-%m')}",
        "journal_drafts",
        "warning" if age >= 3 or today.day >= 25 else "info",
        f"{len(drafts)} قيود مسودة لسه مترحتش",
        "المسودة مش بتسمع في الأرصدة. ارحلها من اليومية بعد المراجعة.",
        _safe_url("journal"),
    )]


def _consume_layers(layers, amount):
    left = as_float(amount)
    kept = []
    for item_date, item_amount in layers:
        if left <= 0.0001:
            kept.append([item_date, item_amount])
            continue
        take = min(item_amount, left)
        item_amount -= take
        left -= take
        if item_amount > 0.0001:
            kept.append([item_date, item_amount])
    return kept


def _oldest_outstanding(account, entries, is_receivable):
    opening = as_float(getattr(account, "opening_balance", 0))
    layers = []
    if is_receivable and opening > 0:
        layers.append(["", opening])
    elif (not is_receivable) and opening < 0:
        layers.append(["", -opening])

    for entry in entries:
        amount = as_float(entry.amount)
        if is_receivable:
            if entry.debit_account_id == account.id:
                layers.append([entry.date or "", amount])
            elif entry.credit_account_id == account.id:
                layers = _consume_layers(layers, amount)
        else:
            if entry.credit_account_id == account.id:
                layers.append([entry.date or "", amount])
            elif entry.debit_account_id == account.id:
                layers = _consume_layers(layers, amount)
    if not layers:
        return None, 0.0
    remaining = round(sum(item[1] for item in layers), 2)
    return layers[0][0], remaining


def _overdue_alerts(today):
    categories = PAYABLE_CATEGORIES | RECEIVABLE_CATEGORIES
    accounts = (
        ChartOfAccount.query.filter(ChartOfAccount.category.in_(categories))
        .order_by(ChartOfAccount.category, ChartOfAccount.name)
        .all()
    )
    if not accounts:
        return []
    account_map = {account.id: account for account in accounts}
    hidden = hidden_account_ids_for_query()
    entries = (
        JournalEntry.query.filter(JournalEntry.status == "مرحل")
        .order_by(JournalEntry.date.asc(), JournalEntry.id.asc())
        .all()
    )
    by_account = defaultdict(list)
    for entry in entries:
        if entry.debit_account_id in account_map and entry.debit_account_id not in hidden:
            by_account[entry.debit_account_id].append(entry)
        if (
            entry.credit_account_id in account_map
            and entry.credit_account_id not in hidden
            and entry.credit_account_id != entry.debit_account_id
        ):
            by_account[entry.credit_account_id].append(entry)

    alerts = []
    for account in accounts:
        is_receivable = account.category in RECEIVABLE_CATEGORIES
        oldest, remaining = _oldest_outstanding(account, by_account.get(account.id, []), is_receivable)
        if remaining <= 0.0001:
            continue
        age = _days_between(oldest, today)
        if oldest == "" or age is None:
            age = OVERDUE_DAYS
        if age < OVERDUE_DAYS:
            continue
        kind = "receivable_overdue" if is_receivable else "payable_overdue"
        money = format_grouped_number(remaining)
        title = (
            f"لنا {money} عند {account.name} من {age} يوم"
            if is_receivable
            else f"علينا {money} لـ {account.name} من {age} يوم"
        )
        detail = (
            "افتح كشف الحساب وحصّل المتأخر."
            if is_receivable
            else "افتح كشف الحساب وراجع السداد."
        )
        alerts.append(_alert(
            f"{kind}:{account.id}:{oldest or 'opening'}",
            kind,
            "danger" if age >= 90 else "warning",
            title,
            detail,
            _safe_url("account_statement", account_id=account.id),
            remaining,
        ))
    alerts.sort(key=lambda item: (-as_float(item.get("amount")), item["title"]))
    return alerts[:16]


def _contract_alerts(today):
    alerts = []
    for project in Project.query.order_by(Project.end_date).all():
        end_date = _parse_date(project.end_date)
        if not end_date:
            continue
        remaining = (end_date - today).days
        if remaining > CONTRACT_WARN_DAYS:
            continue
        ended = remaining < 0
        alerts.append(_alert(
            f"contract:{project.id}:{end_date.isoformat()}",
            "contract_ended" if ended else "contract_ending",
            "danger" if ended else "warning",
            f"عقد {project.display_name} {'انتهى' if ended else 'قرب ينتهي'}",
            (
                f"تاريخ النهاية {end_date.isoformat()} — متأخر {abs(remaining)} يوم."
                if ended
                else f"باقي {remaining} يوم على {end_date.isoformat()}."
            ),
            _safe_url("project_detail", project_id=project.id),
        ))
    return alerts


def _project_cost_map():
    costs = defaultdict(float)
    for project_id, total in (
        db.session.query(ProgressPayment.project_id, ProgressPayment.total_value)
        .filter(ProgressPayment.subcontractor_id.isnot(None))
        .all()
    ):
        costs[project_id] += as_float(total)
    for item in PurchaseOrder.query.all():
        if (item.status or "") in {"مغلق", "مدفوع"}:
            costs[item.project_id] += as_float(item.total_value)
    for project_id, amount in db.session.query(LaborEntry.project_id, LaborEntry.amount).all():
        costs[project_id] += as_float(amount)
    for item in Equipment.query.all():
        if item.project_id:
            costs[item.project_id] += as_float(item.operating_cost) + as_float(item.maintenance)
    return costs


def _cost_overrun_alerts():
    costs = _project_cost_map()
    alerts = []
    for project in Project.query.order_by(Project.code).all():
        limit = as_float(project.contract_value)
        if limit <= 0:
            continue
        cost = round(as_float(costs.get(project.id, 0.0)), 2)
        if cost < limit * COST_WARN_RATIO:
            continue
        ratio = cost / limit
        over = cost > limit
        alerts.append(_alert(
            f"cost:{project.id}:{int(limit)}",
            "cost_overrun",
            "danger" if over else "warning",
            (
                f"تكلفة {project.display_name} عدّت قيمة العقد"
                if over
                else f"تكلفة {project.display_name} قربت من قيمة العقد"
            ),
            f"التكلفة {format_grouped_number(cost)} من أصل العقد {format_grouped_number(limit)} ({ratio:.0%}).",
            _safe_url("project_detail", project_id=project.id),
            cost,
        ))
    return alerts


def _open_purchase_alerts(today):
    open_orders = []
    for item in PurchaseOrder.query.order_by(PurchaseOrder.date, PurchaseOrder.id).all():
        if (item.status or "") in {"مغلق", "مدفوع"}:
            continue
        age = _days_between(item.date, today)
        if age is None or age < OVERDUE_DAYS:
            continue
        open_orders.append((item, age))
    if not open_orders:
        return []
    oldest = open_orders[0][0]
    names = "، ".join((item.items_label or item.document_number) for item, _age in open_orders[:3])
    extra = f" وغيرها {len(open_orders) - 3}" if len(open_orders) > 3 else ""
    return [_alert(
        f"open_po:{oldest.id}:{len(open_orders)}",
        "open_purchase",
        "warning",
        f"{len(open_orders)} أوامر شراء مفتوحة من أكتر من 30 يوم",
        f"{names}{extra}. راجعها من المشتريات لو الفاتورة اتأخرت.",
        _safe_url("purchase_orders"),
    )]


def collect_raw_alerts(user, today=None):
    today = today or _today()
    alerts = []
    if _user_allows(user, "hr", "hr.view"):
        alerts.extend(_absence_alerts(today))
        alerts.extend(_attendance_draft_alerts(today))
    if _user_allows(user, "accounts", "reports", "receipts", "supplier_payments"):
        alerts.extend(_overdue_alerts(today))
    if _user_allows(user, "journal", "journal.view", "journal.create"):
        alerts.extend(_journal_draft_alerts(today))
    if _user_allows(user, "projects", "projects.view"):
        alerts.extend(_contract_alerts(today))
        alerts.extend(_cost_overrun_alerts())
    if _user_allows(user, "purchase_orders", "purchase_orders.create"):
        alerts.extend(_open_purchase_alerts(today))
    alerts.sort(key=lambda item: (SEVERITY_ORDER.get(item["severity"], 9), item["title"]))
    return alerts


def visible_alerts_for(user, today=None):
    if not user:
        return []
    if has_request_context() and getattr(g, "_nav_alerts", None) is not None:
        return g._nav_alerts
    raw = collect_raw_alerts(user, today=today)
    dismissed = {
        row.alert_key
        for row in AlertDismissal.query.filter_by(user_id=user.id).all()
    }
    visible = [item for item in raw if item["key"] not in dismissed]
    if has_request_context():
        g._nav_alerts = visible
    return visible


def dismiss_alert(user, alert_key):
    key = (alert_key or "").strip()[:160]
    if not user or not key:
        return False
    live_keys = {item["key"] for item in collect_raw_alerts(user)}
    if key not in live_keys:
        return False
    existing = AlertDismissal.query.filter_by(user_id=user.id, alert_key=key).first()
    if existing:
        return True
    db.session.add(AlertDismissal(
        user_id=user.id,
        alert_key=key,
        dismissed_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    ))
    db.session.commit()
    return True
