"""شاشات المبيعات بتقسيم شيت الإكسل: لوحة المؤشرات، العملاء والمشاريع، قائمة البنود.

المرحلة الأولى: عرض وإدخال بيانات مرجعية بس، من غير أي قيد محاسبي.
الأرقام بتتجمع بنفس مصادر البرنامج (مستخلصات العملاء، النقلات، تحصيل العملاء)،
ورصيد كشف الحساب بيتقرا من نفس الرصيد المحفوظ اللي شاشة الحسابات بتعرضه.
"""
from collections import defaultdict

from flask import flash, redirect, render_template, request, url_for
from sqlalchemy.orm import joinedload

from models import ChartOfAccount, JournalEntry, ProgressPayment, Project, SalesItem, SalesTrip, db
from services.accounting import (
    UNIT_OPTIONS, as_float, canonical_client_name, client_name_from_account, client_progress_query,
    find_client_account, is_treasury_account, party_screen_balances,
)
from services.paging import listing_page

from services.sales_invoices import (  # noqa: E402
    PAYMENT_STATUSES, SALES_ITEM_CATEGORIES, SALES_OPERATION_TYPES, SALES_UNITS, collected_by_invoice,
    operation_label, payment_status,
)


def _client_key(name):
    return (canonical_client_name(name) or "").strip().casefold()


def _sales_figures():
    """مجاميع المبيعات من مصادر البرنامج نفسها، باستعلام واحد لكل مصدر."""
    payments = client_progress_query().options(joinedload(ProgressPayment.project)).all()
    trips = SalesTrip.query.with_entities(SalesTrip.client_name, SalesTrip.total_amount).all()
    # المحصل من دفتر الأستاذ: كل قيد مرحّل مدين خزنة/بنك ودائن حساب العميل.
    # سند التحصيل نفسه بيعمل القيد ده، فكده التحصيل اليدوي والسندات متحسبين مرة واحدة.
    accounts = ChartOfAccount.query.with_entities(
        ChartOfAccount.id, ChartOfAccount.code, ChartOfAccount.name, ChartOfAccount.category,
    ).all()
    treasury_ids = [row.id for row in accounts if is_treasury_account(row)]
    client_names = {row.id: client_name_from_account(row) for row in accounts if row.category == "العملاء"}
    client_names = {key: value for key, value in client_names.items() if value}
    receipts = []
    if treasury_ids and client_names:
        receipts = [
            (client_names.get(credit_id), amount)
            for credit_id, amount in db.session.query(JournalEntry.credit_account_id, JournalEntry.amount).filter(
                JournalEntry.status == "مرحل",
                JournalEntry.debit_account_id.in_(treasury_ids),
                JournalEntry.credit_account_id.in_(list(client_names)),
            ).all()
        ]

    per_client = defaultdict(lambda: {"name": "", "works": 0.0, "net": 0.0, "collected": 0.0, "count": 0})

    def bucket(name):
        key = _client_key(name)
        row = per_client[key]
        if not row["name"]:
            row["name"] = (canonical_client_name(name) or name or "").strip() or "بدون اسم"
        return row

    invoice_paid = collected_by_invoice([item.id for item in payments])
    by_type = defaultdict(lambda: {"count": 0, "net": 0.0, "collected": 0.0})
    by_status = {status: {"count": 0, "net": 0.0} for status in PAYMENT_STATUSES}
    pp_works = pp_net = pp_retention = pp_tax = pp_vat = 0.0
    for item in payments:
        works, net = as_float(item.total_value), as_float(item.net_value)
        pp_works += works
        pp_net += net
        pp_retention += as_float(item.discount_insurance)
        pp_tax += as_float(item.tax)
        pp_vat += as_float(item.vat_value)
        paid = invoice_paid.get(item.id, 0.0)
        kind = by_type[operation_label(item)]
        kind["count"] += 1
        kind["net"] += net
        kind["collected"] += paid
        status = payment_status(net, paid)
        if status in by_status:
            by_status[status]["count"] += 1
            by_status[status]["net"] += net
        row = bucket(item.project.client_name if item.project else "")
        row["works"] += works
        row["net"] += net
        row["count"] += 1

    trips_total = 0.0
    for client_name, amount in trips:
        value = as_float(amount)
        trips_total += value
        row = bucket(client_name)
        row["works"] += value
        row["net"] += value
        row["count"] += 1

    collected = 0.0
    for client_name, amount in receipts:
        value = as_float(amount)
        collected += value
        bucket(client_name)["collected"] += value

    net_due = pp_net + trips_total
    return {
        "works": round(pp_works + trips_total, 2),
        "invoice_works": round(pp_works, 2),
        "net_due": round(net_due, 2),
        "collected": round(collected, 2),
        "remaining": round(net_due - collected, 2),
        "collection_rate": (collected / net_due * 100) if net_due else 0.0,
        "vat": round(pp_vat, 2),
        "retention": round(pp_retention, 2),
        "tax_deducted": round(pp_tax, 2),
        "count": len(payments) + len(trips),
        "by_type": [
            {"label": label, "count": row["count"], "net": round(row["net"], 2),
             "collected": round(row["collected"], 2), "remaining": round(row["net"] - row["collected"], 2)}
            for label, row in sorted(by_type.items(), key=lambda kv: SALES_OPERATION_TYPES.index(kv[0]) if kv[0] in SALES_OPERATION_TYPES else 99)
        ] + [{"label": "النقلات (شيت النقل)", "count": len(trips), "net": round(trips_total, 2), "collected": None, "remaining": None}],
        "by_status": [{"label": key, "count": value["count"], "net": round(value["net"], 2)} for key, value in by_status.items()],
        "per_client": per_client,
    }


def register(app):
    @app.route("/sales/dashboard")
    def sales_dashboard():
        figures = _sales_figures()
        top_clients = sorted(
            figures["per_client"].values(),
            key=lambda row: -(row["net"] - row["collected"]),
        )[:10]
        return render_template("sales_dashboard.html", figures=figures, top_clients=top_clients)

    @app.route("/sales/clients")
    def sales_clients():
        figures = _sales_figures()
        projects = Project.query.order_by(Project.client_name, Project.code).all()
        projects_by_client = defaultdict(list)
        for project in projects:
            projects_by_client[_client_key(project.client_name)].append(project)
        for key in projects_by_client:
            figures["per_client"][key]  # يضمن ظهور العميل حتى لو مالوش حركة
            if not figures["per_client"][key]["name"]:
                figures["per_client"][key]["name"] = projects_by_client[key][0].client_name

        search = (request.args.get("q") or "").strip().casefold()
        rows = []
        for key, row in figures["per_client"].items():
            if search and search not in row["name"].casefold():
                continue
            rows.append({
                **row,
                "remaining": round(row["net"] - row["collected"], 2),
                "projects": projects_by_client.get(key, []),
                "account": find_client_account(row["name"]),
            })
        rows.sort(key=lambda r: r["name"])
        statement_views = party_screen_balances([r["account"] for r in rows if r["account"]])
        totals = {
            "net": round(sum(r["net"] for r in rows), 2),
            "collected": round(sum(r["collected"] for r in rows), 2),
            "remaining": round(sum(r["remaining"] for r in rows), 2),
        }
        return render_template(
            "sales_clients.html", rows=rows, totals=totals, statement_views=statement_views,
            search_q=request.args.get("q") or "",
        )

    @app.route("/sales/projects/<int:project_id>/contact", methods=["POST"])
    def update_sales_project_contact(project_id):
        project = Project.query.get_or_404(project_id)
        project.engineer_name = (request.form.get("engineer_name") or "").strip() or None
        project.contact_phone = (request.form.get("contact_phone") or "").strip() or None
        db.session.commit()
        flash(f"تم حفظ بيانات التواصل للمشروع {project.display_name}", "success")
        return redirect(url_for("sales_clients", q=request.form.get("q") or None))

    @app.route("/sales/items", methods=["GET", "POST"])
    def sales_items():
        if request.method == "POST":
            name = (request.form.get("name") or "").strip()
            if not name:
                flash("اكتب اسم البند", "danger")
                return redirect(url_for("sales_items"))
            if SalesItem.query.filter(db.func.lower(SalesItem.name) == name.lower()).first():
                flash("البند ده متسجل قبل كده", "warning")
                return redirect(url_for("sales_items"))
            item = SalesItem(
                code=(request.form.get("code") or "").strip() or None,
                name=name,
                category=(request.form.get("category") or "").strip() or None,
                unit=(request.form.get("unit") or "").strip() or None,
                default_price=as_float(request.form.get("default_price")),
            )
            db.session.add(item)
            db.session.flush()
            if not item.code:
                item.code = f"ITM-{item.id:02d}"
            db.session.commit()
            flash(f"تم إضافة البند {item.name}", "success")
            return redirect(url_for("sales_items"))

        query = SalesItem.query
        search = (request.args.get("q") or "").strip()
        if search:
            like = f"%{search}%"
            query = query.filter(db.or_(SalesItem.name.ilike(like), SalesItem.code.ilike(like), SalesItem.category.ilike(like)))
        items, pager = listing_page(query.order_by(SalesItem.is_active.desc(), SalesItem.code, SalesItem.id))
        return render_template(
            "sales_items.html", items=items, pager=pager, search_q=search,
            categories=SALES_ITEM_CATEGORIES, units=list(dict.fromkeys(SALES_UNITS + UNIT_OPTIONS)),
        )

    @app.route("/sales/items/<int:item_id>/update", methods=["POST"])
    def update_sales_item(item_id):
        item = SalesItem.query.get_or_404(item_id)
        name = (request.form.get("name") or "").strip()
        if not name:
            flash("اكتب اسم البند", "danger")
            return redirect(url_for("sales_items"))
        item.name = name
        item.code = (request.form.get("code") or "").strip() or item.code
        item.category = (request.form.get("category") or "").strip() or None
        item.unit = (request.form.get("unit") or "").strip() or None
        item.default_price = as_float(request.form.get("default_price"))
        item.is_active = request.form.get("is_active") == "1"
        db.session.commit()
        flash(f"تم تعديل البند {item.name}", "success")
        return redirect(url_for("sales_items"))

    @app.route("/sales/items/<int:item_id>/delete", methods=["POST"])
    def delete_sales_item(item_id):
        item = SalesItem.query.get_or_404(item_id)
        name = item.name
        db.session.delete(item)
        db.session.commit()
        flash(f"تم حذف البند {name}", "success")
        return redirect(url_for("sales_items"))
