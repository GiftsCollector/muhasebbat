import os
import re
import tempfile
from collections import Counter, defaultdict
from html.parser import HTMLParser
from urllib.parse import urlsplit, urlencode, parse_qsl

import pytest
from sqlalchemy import event

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix=".db")
os.close(_DB_FD)
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["DATABASE_URL"] = "sqlite:///" + _DB_PATH.replace("\\", "/")
os.environ.pop("RENDER", None)
os.environ["FLASK_ENV"] = "testing"

from app import app as flask_app
from models import (
    ROLE_ADMIN, ChartOfAccount, Employee, Equipment, JournalEntry, Project, Subcontractor, Supplier, User, db,
)
from services.accounting import sync_journal_related_accounts


class _Forms(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.forms = []
        self.links = []
        self.data_actions = []
        self.cur = None
        self.divs = []
        self.select = None
        self.option = None
        self.textarea = None
        self.button = None

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag in {"div"}:
            self.handle_endtag(tag)

    def handle_starttag(self, tag, attrs):
        a = {k: (v if v is not None else "") for k, v in attrs}
        if a.get("data-action"):
            self.data_actions.append(a)
        if tag == "div":
            self.divs.append("modal" in (a.get("class") or "").split())
            return
        if tag == "a" and a.get("href"):
            self.links.append(a["href"])
        if tag == "form":
            if self.cur is not None:
                return
            self.cur = {
                "action": a.get("action"), "method": (a.get("method") or "get").lower(),
                "fields": [], "buttons": [], "modal": any(self.divs), "onsubmit": a.get("onsubmit", ""),
            }
            self.forms.append(self.cur)
            return
        if self.cur is None:
            return
        if tag == "input":
            typ = (a.get("type") or "text").lower()
            name = a.get("name")
            if typ in {"submit", "image"}:
                self.cur["buttons"].append({
                    "name": name, "value": a.get("value", ""), "text": a.get("value", ""),
                    "formaction": a.get("formaction"), "formmethod": a.get("formmethod"),
                })
                return
            if not name or typ in {"button", "reset", "file"}:
                return
            if typ in {"checkbox", "radio"} and "checked" not in a:
                return
            self.cur["fields"].append({
                "name": name, "value": a.get("value", "on" if typ in {"checkbox", "radio"} else ""),
                "type": typ, "required": "required" in a, "disabled": "disabled" in a,
            })
        elif tag == "select":
            self.select = {"name": a.get("name"), "options": [], "multiple": "multiple" in a,
                           "required": "required" in a, "disabled": "disabled" in a}
        elif tag == "option" and self.select is not None:
            self.option = {"value": a.get("value"), "selected": "selected" in a, "text": "",
                           "disabled": "disabled" in a}
            self.select["options"].append(self.option)
        elif tag == "textarea":
            self.textarea = {"name": a.get("name"), "value": "", "type": "textarea",
                             "required": "required" in a, "disabled": "disabled" in a}
        elif tag == "button":
            typ = (a.get("type") or "submit").lower()
            if typ == "submit":
                self.button = {
                    "name": a.get("name"), "value": a.get("value", ""), "text": "",
                    "formaction": a.get("formaction"), "formmethod": a.get("formmethod"),
                }
                self.cur["buttons"].append(self.button)

    def handle_data(self, data):
        if self.option is not None:
            self.option["text"] += data
        if self.textarea is not None:
            self.textarea["value"] += data
        if self.button is not None:
            self.button["text"] += data

    def handle_endtag(self, tag):
        if tag == "div":
            if self.divs:
                self.divs.pop()
        elif tag == "form":
            self.cur = None
        elif tag == "option":
            self.option = None
        elif tag == "button":
            self.button = None
        elif tag == "textarea" and self.textarea is not None:
            if self.cur is not None and self.textarea["name"]:
                self.cur["fields"].append(self.textarea)
            self.textarea = None
        elif tag == "select" and self.select is not None:
            sel = self.select
            self.select = None
            if self.cur is None or not sel["name"] or sel["multiple"]:
                return
            opts = [o for o in sel["options"] if not o["disabled"]]
            chosen = next((o for o in opts if o["selected"]), opts[0] if opts else None)
            value = "" if chosen is None else (chosen["value"] if chosen["value"] is not None else chosen["text"].strip())
            fallback = next((o["value"] for o in opts if o["value"]), "")
            self.cur["fields"].append({"name": sel["name"], "value": value, "type": "select",
                                       "required": sel["required"], "disabled": sel["disabled"],
                                       "fallback": fallback})


def _parse(html):
    parser = _Forms()
    parser.feed(html)
    return parser


SKIP_POST = (
    "/logout", "/login", "/admin/restore", "/admin/backup", "/import-workbook", "/account/", "/admin/amounts",
    "/forgot-password", "/setup-admin", "/attachments/", "/alerts/dismiss",
)
SKIP_GET = (
    "/logout", "/static/", "/admin/backup", "/import-workbook/current", "/desktop-shortcut", "http", "#",
    "javascript:", "mailto:", "tel:",
)


def _fill(field, path):
    name = (field["name"] or "").lower()
    typ = field["type"]
    if typ == "select":
        return field.get("fallback", "")
    if typ == "date" or "date" in name or name.endswith("_from") or name.endswith("_to"):
        return "2026-10-15"
    if typ == "month" or "month" in name:
        return "2026-10"
    if typ == "email":
        return "probe@example.com"
    if typ == "password":
        return ""
    if typ == "number" or any(k in name for k in (
        "amount", "quantity", "price", "cost", "value", "percentage", "days", "salary", "rate", "km",
        "cubage", "hours", "qty", "total", "fee", "discount",
    )):
        return "100"
    if typ == "time":
        return "08:00"
    return "تجربة فحص"


def _accepts_post(path):
    try:
        flask_app.url_map.bind("localhost").match(path, method="POST")
        return True
    except Exception:
        return False


def _submissions(page_url, html):
    parsed = _parse(html)
    out = []
    shared = [f for f in parsed.forms if f["modal"] and not f["action"] and f["method"] == "post"]
    for form in parsed.forms:
        if form in shared and (parsed.data_actions or not _accepts_post(urlsplit(page_url).path)):
            continue
        base_action = form["action"] or page_url
        for btn in (form["buttons"] or [{"name": None, "value": "", "text": "", "formaction": None, "formmethod": None}]):
            url = btn.get("formaction") or base_action
            method = (btn.get("formmethod") or form["method"] or "get").lower()
            data = []
            for f in form["fields"]:
                if f.get("disabled"):
                    continue
                data.append((f["name"], f["value"], f))
            if btn.get("name"):
                data.append((btn["name"], btn.get("value", ""), {"name": btn["name"], "type": "button"}))
            path = urlsplit(url).path
            text = " ".join((btn.get("text") or "").split())
            kind = _classify(method, path, text, btn, form)
            out.append({
                "page": page_url, "url": url, "method": method, "data": data, "kind": kind,
                "text": text, "form": form, "button": btn,
            })
    for attrs in parsed.data_actions:
        url = attrs["data-action"]
        for form in shared:
            data = []
            for f in form["fields"]:
                if f.get("disabled"):
                    continue
                key = "data-" + (f["name"] or "").replace("_", "-")
                alt = key[:-3] if key.endswith("-id") else key + "-id"
                value = attrs.get(key, attrs.get(alt, attrs.get(key.replace("-name", ""), f["value"])))
                data.append((f["name"], value, f))
            out.append({
                "page": page_url, "url": url, "method": "post", "data": data, "kind": "edit",
                "text": "data-action", "form": form, "button": {"name": None, "value": ""},
            })
    return parsed, out


def _classify(method, path, text, btn, form):
    value = (btn.get("value") or "").lower()
    if method == "get":
        return "filter"
    if "/delete" in path or "unlink" in path or value.startswith("delete") or "حذف" in text or "فك" in text:
        return "delete"
    if re.search(r"/(close|reopen|post|unpost|reverse|copy|status|permissions|weekly)(/|$)", path) or path.endswith("/post-all"):
        return "action"
    if "/update" in path or form["modal"]:
        return "edit"
    return "create"


def _shape(url):
    parts = urlsplit(url)
    keys = ",".join(sorted(k for k, _ in parse_qsl(parts.query, keep_blank_values=True)))
    return re.sub(r"\d+", "N", parts.path) + "?" + keys


def _counts():
    return {
        "project": Project.query.count(), "subcontractor": Subcontractor.query.count(),
        "supplier": Supplier.query.count(), "employee": Employee.query.count(),
        "equipment": Equipment.query.count(), "account": ChartOfAccount.query.count(),
        "journal": JournalEntry.query.count(),
    }


def _is_error(resp):
    if resp.status_code >= 500:
        return True
    try:
        body = resp.get_data(as_text=True)
    except Exception:
        return False
    return "Internal Server Error" in body or "Traceback (most recent call last)" in body


def _flashes(body):
    return re.findall(r'class="alert alert-(\w+)[^"]*"[^>]*>\s*([^<]{0,160})', body)


@pytest.fixture()
def client():
    saved_config = {key: flask_app.config.get(key) for key in ("TESTING", "WTF_CSRF_ENABLED", "PROPAGATE_EXCEPTIONS")}
    flask_app.config["TESTING"] = True
    flask_app.config["WTF_CSRF_ENABLED"] = False
    flask_app.config["PROPAGATE_EXCEPTIONS"] = False
    with flask_app.app_context():
        db.drop_all()
        db.create_all()

        def _fk_on(dbapi_conn, _record):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

        engine = db.engine
        event.listen(engine, "connect", _fk_on)
        engine.dispose()
        sync_journal_related_accounts()
        admin = User(username="admin", full_name="مدير النظام", role=ROLE_ADMIN, is_active=True)
        admin.set_password("secret12")
        db.session.add(admin)
        db.session.commit()
        admin_id = admin.id
        db.session.remove()
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess["user_id"] = admin_id
        sess["admin_amounts_visible"] = True
    c.admin_id = admin_id
    yield c
    event.remove(engine, "connect", _fk_on)
    engine.dispose()
    flask_app.config.update(saved_config)


def _seed(client):
    with flask_app.app_context():
        treasury = ChartOfAccount.query.filter_by(code="TRS-MAIN").first()
        expense = ChartOfAccount.query.filter_by(code="EXP-ELC").first() or ChartOfAccount.query.filter_by(category="المصروفات").first()
        tid, eid = treasury.id, expense.id
    post = lambda url, data: client.post(url, data=data, follow_redirects=True)
    results = []
    results.append(("project", post("/projects", {
        "code": "PRJ-A1", "project_name": "برج فحص", "client_name": "عميل فحص", "contract_type": "مقاولات عامة",
        "contract_value": "1000000", "start_date": "2026-09-01",
    })))
    results.append(("project2", post("/projects", {
        "code": "PRJ-A2", "project_name": "فيلا فحص", "client_name": "عميل تاني", "contract_type": "مقاولات عامة",
        "contract_value": "500000", "start_date": "2026-09-01",
    })))
    results.append(("project-empty", post("/projects", {
        "code": "", "project_name": "مشروع من غير كود", "client_name": "عميل فحص", "contract_type": "مقاولات عامة",
    })))
    results.append(("project-dup-code", post("/projects", {
        "code": "PRJ-A1", "project_name": "مشروع كود مكرر", "client_name": "عميل فحص", "contract_type": "مقاولات عامة",
    })))
    with flask_app.app_context():
        projects = Project.query.order_by(Project.id).all()
        if not projects:
            p = Project(code="PRJ-T1", project_name="برج فحص", client_name="عميل فحص",
                        contract_type="مقاولات عامة", contract_value=1000000)
            db.session.add(p)
            db.session.commit()
            projects = [p]
        pid = projects[0].id
    results.append(("subcontractor", post("/subcontractors", {"name": "مقاول فحص", "phone": "0100"})))
    results.append(("subcontractor2", post("/subcontractors", {"name": "مقاول فحص تاني"})))
    results.append(("supplier", post("/suppliers", {"name": "مورد فحص", "phone": "0101"})))
    results.append(("supplier2", post("/suppliers", {"name": "مورد فحص تاني"})))
    results.append(("employee", post("/employees", {"name": "موظف فحص", "tag": "موظف", "basic_salary": "3000"})))
    results.append(("employee2", post("/employees", {"name": "موظف فحص تاني", "tag": "موظف", "basic_salary": "2000"})))
    results.append(("equipment", post("/equipment", {
        "name": "لودر فحص", "plate_number": "1412", "purchase_cost": "10000", "operating_cost": "250", "maintenance": "40",
        "hours_used": "5", "project_id": str(pid),
    })))
    results.append(("equipment2", post("/equipment", {"name": "حفار فحص", "project_id": str(pid)})))
    with flask_app.app_context():
        sid = Subcontractor.query.order_by(Subcontractor.id).first().id
        supid = Supplier.query.order_by(Supplier.id).first().id
        empid = Employee.query.order_by(Employee.id).first().id
        eqid = Equipment.query.order_by(Equipment.id).first().id
    results.append(("sub_payment", post("/subcontractor_payments", {
        "subcontractor_id": str(sid), "project_id": str(pid), "amount": "2000", "payment_kind": "تحت الحساب",
        "payment_method": "نقدي", "treasury_account_id": str(tid), "date": "2026-09-05",
    })))
    results.append(("ipc", post("/progress_payments", {
        "project_id": str(pid), "subcontractor_id": str(sid), "date": "2026-09-06",
        "retention_percentage": "5", "tax_percentage": "0", "description": ["خرسانة"], "unit": ["م3"],
        "quantity": ["10"], "unit_price": ["1000"],
    })))
    results.append(("client_ipc", post("/client_progress_payments", {
        "project_id": str(pid), "date": "2026-09-07", "retention_percentage": "0", "tax_percentage": "0",
        "description": ["أعمال"], "unit": ["مقطوعية"], "quantity": ["1"], "unit_price": ["50000"],
    })))
    results.append(("client_receipt", post("/client_receipts", {
        "client_name": "عميل فحص", "project_id": str(pid), "amount": "1500", "payment_method": "نقدي",
        "treasury_account_id": str(tid), "date": "2026-09-08",
    })))
    results.append(("purchase_order", post("/purchase_orders", {
        "project_id": str(pid), "supplier_id": str(supid), "item_name": "حديد", "warehouse_name": "رئيسي",
        "quantity": "10", "unit_price": "100", "discount": "0", "date": "2026-09-09", "status": "مفتوح",
    })))
    results.append(("supplier_payment", post("/supplier_payments", {
        "supplier_id": str(supid), "date": "2026-09-10", "amount": "500", "payment_method": "نقدي",
        "treasury_account_id": str(tid), "reference": "PAY-1",
    })))
    results.append(("inventory_in", post("/inventory", {
        "project_id": str(pid), "warehouse_name": "رئيسي", "quantity": "100", "unit_cost": "40",
        "transaction_type": "وارد", "date": "2026-09-10", "material_name": "أسمنت", "unit": "شيكارة",
    })))
    results.append(("inventory_out", post("/inventory", {
        "project_id": str(pid), "warehouse_name": "رئيسي", "destination_warehouse": f"acc:{eid}",
        "quantity": "4", "unit_cost": "40", "transaction_type": "منصرف", "date": "2026-09-11",
        "material_name": "أسمنت", "unit": "شيكارة",
    })))
    results.append(("inventory_eq", post("/inventory", {
        "project_id": str(pid), "warehouse_name": "رئيسي", "quantity": "2", "unit_cost": "40",
        "transaction_type": "منصرف", "date": "2026-09-12", "material_name": "أسمنت", "unit": "شيكارة",
        "destination_warehouse": f"acc:{eid}", "equipment_id": str(eqid),
    })))
    results.append(("estimation", post("/estimations", {
        "client_name": "عميل فحص", "project_id": str(pid), "date": "2026-09-01", "discount_percentage": "0",
        "admin_percentage": "0", "admin_mode": "إضافة", "item_description": ["بند"], "item_unit": ["مقطوعية"],
        "item_quantity": ["1"], "item_unit_price": ["80000"], "item_discount_percentage": ["0"],
    })))
    results.append(("sales", post("/sales", {
        "line_date": ["2026-09-15"], "line_voucher_number": ["58927"], "line_trip_type": ["سيارة"],
        "line_client_name": ["الكتيبة"], "line_notes": ["كسارة"], "line_distance_km": ["110"],
        "line_driver_name": ["شعبان محمد"], "line_tractor_number": ["1412"], "line_cubage": ["64"],
        "line_discount": ["6"], "line_unit_price": ["185"], "line_advances": ["0"],
    })))
    results.append(("journal_post", post("/journal", {
        "date": "2026-09-15", "entry_action": "post", "line_description": ["مصروف فحص"],
        "line_debit_account_id": [str(eid)], "line_credit_account_id": [str(tid)], "line_amount": ["100"],
    })))
    results.append(("journal_draft", post("/journal", {
        "date": "2026-09-16", "entry_action": "draft", "line_description": ["مسودة فحص"],
        "line_debit_account_id": [str(eid)], "line_credit_account_id": [str(tid)], "line_amount": ["70"],
    })))
    results.append(("attendance", post("/hr", {
        "action": "save_attendance", "period_month": "2026-09", "attendance_from": "2026-09-01",
        "attendance_to": "2026-09-03", "att_employee_id": [str(empid)], "att_selected": [str(empid)],
        "att_status": ["حضور"], "att_from": ["2026-09-01"], "att_to": ["2026-09-03"], "att_check_in": [""],
        "att_check_out": [""], "att_notes": [""],
    })))
    results.append(("payroll", post("/hr", {
        "action": "post_payroll", "period_month": "2026-09", "posting_date": "2026-09-30",
        "employee_id": [str(empid)], "payroll_selected": [str(empid)], "work_days": ["30"],
        "vacation_days": ["0"], "basic_salary": ["3000"], "overtime": ["0"], "incentives": ["0"],
        "delay_deduction": ["0"], "permission_deduction": ["0"], "other_deductions": ["0"], "advances": ["0"],
    })))
    results.append(("salary_payment", post("/hr", {
        "action": "pay_salary", "period_month": "2026-09", "employee_id": str(empid), "date": "2026-09-30",
        "amount": "500", "payment_method": "نقدي", "treasury_account_id": str(tid),
    })))
    results.append(("user", post("/users", {
        "username": "clerk1", "full_name": "مدخل فحص", "password": "secret12", "role": "data_entry",
    })))
    results.append(("period", post("/periods", {"name": "أكتوبر", "from_date": "2026-10-01", "to_date": "2026-10-30"})))
    return results


def _crawl(client, failures, start_urls, limit=900):
    seen, pages = set(), []
    shapes = Counter()
    queue = list(start_urls)
    while queue and len(seen) < limit:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        resp = client.get(url)
        if _is_error(resp):
            failures.append(("GET", url, resp.status_code, ""))
            continue
        ctype = resp.headers.get("Content-Type", "")
        if resp.status_code != 200 or "text/html" not in ctype:
            continue
        html = resp.get_data(as_text=True)
        pages.append(url)
        for href in _parse(html).links:
            href = href.strip()
            if not href.startswith("/") or href.startswith(SKIP_GET):
                continue
            href = href.split("#")[0]
            if href in seen:
                continue
            shape = _shape(href)
            if shapes[shape] >= 3:
                continue
            shapes[shape] += 1
            queue.append(href)
    return pages


def _submit(client, sub):
    url = sub["url"]
    if sub["method"] == "get":
        sep = "&" if "?" in url else "?"
        return client.get(url + (sep + urlencode(sub["data"]) if sub["data"] else ""), follow_redirects=True)
    data = defaultdict(list)
    for k, v in sub["data"]:
        data[k].append(v)
    return client.post(url, data={k: (v if len(v) > 1 else v[0]) for k, v in data.items()}, follow_redirects=True)


def _sig(sub):
    hidden = sorted((f["name"], f["value"]) for f in sub["form"]["fields"] if f["type"] == "hidden" and f["name"] not in {"post_token", "csrf_token", "next"})
    return (sub["url"], tuple(hidden), sub["button"].get("value") or "")


def test_every_edit_and_delete_button_works_without_server_error(client):
    """يزور كل شاشة ويدوس كل زرار فلتر/إضافة/تعديل/إجراء/حذف: ممنوع 500، والتعديل ما يعملش سجلات شبح، والحذف يا يتم يا يترفض برسالة."""
    import traceback
    from flask import got_request_exception

    errors = []

    def _record(sender, exception, **extra):
        errors.append("".join(traceback.format_exception(type(exception), exception, exception.__traceback__))[-1800:])

    got_request_exception.connect(_record, flask_app)
    failures, notes = [], []
    for name, resp in _seed(client):
        if _is_error(resp):
            failures.append(("SEED", name, resp.status_code, ""))

    start = sorted({
        rule.rule for rule in flask_app.url_map.iter_rules()
        if "GET" in rule.methods and "<" not in rule.rule and not rule.rule.startswith(SKIP_GET)
    })
    pages = _crawl(client, failures, start)
    assert len(pages) > 40

    def page_subs(page, kind):
        resp = client.get(page)
        if resp.status_code != 200 or "text/html" not in resp.headers.get("Content-Type", ""):
            return []
        _, subs = _submissions(page, resp.get_data(as_text=True))
        return [s for s in subs if s["kind"] == kind]

    def run_phase(kind, per_shape=2, before=None):
        done = Counter()

        def key_of(sub):
            return (_shape(sub["url"]), sub["button"].get("name") or "", sub["button"].get("value") or "", sub["text"] if kind == "delete" else sub["page"].split("?")[0])

        def allowed(sub):
            path = urlsplit(sub["url"]).path
            if sub["method"] == "post" and path.startswith(SKIP_POST):
                return False
            if re.match(rf"^/users/{client.admin_id}/", path):
                return False
            if before and not before(sub):
                return False
            return done[key_of(sub)] < per_shape

        if kind == "delete":
            for page in list(pages):
                for _ in range(15):
                    candidates = [s for s in page_subs(page, kind) if allowed(s)]
                    if not candidates:
                        break
                    process(kind, candidates[0], done, key_of)
        else:
            subs_all = []
            for page in list(pages):
                subs_all.extend(page_subs(page, kind))
            for sub in subs_all:
                if allowed(sub):
                    process(kind, sub, done, key_of)
        return done

    def process(kind, sub, done, key_of):
        path = urlsplit(sub["url"]).path
        done[key_of(sub)] += 1
        if True:
            filled = []
            for k, v, f in sub["data"]:
                if not v and f["type"] not in {"button", "hidden"}:
                    if kind == "create" or (kind in {"edit", "action"} and f.get("required")):
                        v = _fill(f, path)
                if kind == "edit" and k in {"name", "project_name"} and v and f["type"] != "hidden":
                    v = f"{v} م"
                filled.append((k, v))
            sub["data"] = filled
            with flask_app.app_context():
                before_counts = _counts()
            raised_before = len(errors)
            resp = _submit(client, sub)
            body = resp.get_data(as_text=True) if resp.status_code < 600 else ""
            if _is_error(resp) or len(errors) > raised_before:
                failures.append((kind.upper(), f"{sub['method']} {sub['url']} [{sub['text']}] from {sub['page']}", resp.status_code, sub["data"][:12]))
                return
            if resp.status_code >= 400:
                notes.append((f"{kind}-http-{resp.status_code}", f"{sub['url']} [{sub['text']}] from {sub['page']}", sub["data"][:8]))
            fl = _flashes(body)
            with flask_app.app_context():
                after_counts = _counts()
            if kind == "edit":
                grown = {k: (before_counts[k], after_counts[k]) for k in before_counts if after_counts[k] > before_counts[k]}
                if grown:
                    notes.append(("edit-grew", f"{sub['url']} [{sub['text']}]", grown, fl))
            if kind == "delete":
                status = [c for c, _ in fl]
                if "success" in status:
                    check = client.get(sub["page"])
                    if check.status_code == 200:
                        _, again = _submissions(sub["page"], check.get_data(as_text=True))
                        if any(_sig(s) == _sig(sub) for s in again):
                            notes.append(("delete-not-stuck", f"{sub['url']} [{sub['text']}] from {sub['page']}", fl))
                elif not status:
                    notes.append(("delete-silent", f"{sub['url']} [{sub['text']}] -> {resp.status_code} {resp.request.path}", fl))

    try:
        run_phase("filter", per_shape=2)
        run_phase("create", per_shape=1)
        pages = _crawl(client, failures, start)
        run_phase("edit", per_shape=3)
        run_phase("action", per_shape=2, before=lambda s: "/close" not in s["url"])
        run_phase("delete", per_shape=3)
        run_phase("action", per_shape=2, before=lambda s: "/close" in s["url"])
    finally:
        got_request_exception.disconnect(_record, flask_app)

    assert not failures, failures
    assert not errors, errors[0]
    assert not notes, notes
