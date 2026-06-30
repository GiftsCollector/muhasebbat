from collections import defaultdict, deque
from datetime import date
import os

from flask import Flask, render_template, request, url_for, flash, redirect
from models import db, Project, BOQItem, ChartOfAccount, ProgressPayment, ProgressPaymentItem, CostEntry, Subcontractor, Supplier, PurchaseOrder, InventoryTransaction, LaborEntry, Equipment, JournalEntry

# Create Flask app
app = Flask(__name__)

# Configuration
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-secret-key-2024")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# Database configuration - handles both local SQLite and production
DATABASE_URL = os.getenv("DATABASE_URL")
if DATABASE_URL:
    # Production - use PostgreSQL or other database
    app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
else:
    # Development - use SQLite
    db_folder = os.path.join(os.path.dirname(__file__), "instance")
    os.makedirs(db_folder, exist_ok=True)
    db_path = os.path.join(db_folder, "data.db")
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{db_path}"

# Initialize database
db.init_app(app)

# Create app context and initialize database
with app.app_context():
    db.create_all()


@app.route("/")
def index():
    projects = Project.query.count()
    accounts = ChartOfAccount.query.count()
    progress_payments = ProgressPayment.query.count()
    journal_entries = JournalEntry.query.count()
    subcontractors = Subcontractor.query.count()
    suppliers = Supplier.query.count()
    purchase_orders = PurchaseOrder.query.count()
    inventory_transactions = InventoryTransaction.query.count()
    labor_entries = LaborEntry.query.count()
    equipment_items = Equipment.query.count()
    return render_template(
        "index.html",
        projects=projects,
        accounts=accounts,
        progress_payments=progress_payments,
        journal_entries=journal_entries,
        subcontractors=subcontractors,
        suppliers=suppliers,
        purchase_orders=purchase_orders,
        inventory_transactions=inventory_transactions,
        labor_entries=labor_entries,
        equipment_items=equipment_items,
    )


@app.route("/accounts", methods=["GET", "POST"])
def accounts():
    if request.method == "POST":
        account = ChartOfAccount(
            code=request.form.get("code"),
            name=request.form.get("name"),
            category=request.form.get("category"),
            project_id=request.form.get("project_id") or None,
            boq_item_id=request.form.get("boq_item_id") or None,
            stage=request.form.get("stage"),
        )
        db.session.add(account)
        db.session.commit()
        flash("تم إضافة الحساب بنجاح", "success")
        return redirect(url_for("accounts"))

    charts = ChartOfAccount.query.order_by(ChartOfAccount.category, ChartOfAccount.code).all()
    projects = Project.query.order_by(Project.code).all()
    boq_items = BOQItem.query.order_by(BOQItem.name).all()
    return render_template("accounts.html", charts=charts, projects=projects, boq_items=boq_items)


@app.route("/projects", methods=["GET", "POST"])
def projects():
    if request.method == "POST":
        project = Project(
            code=request.form.get("code"),
            client_name=request.form.get("client_name"),
            contract_value=float(request.form.get("contract_value") or 0),
            start_date=request.form.get("start_date") or None,
            end_date=request.form.get("end_date") or None,
            contract_type=request.form.get("contract_type"),
        )
        db.session.add(project)
        db.session.commit()
        flash("تم إضافة المشروع بنجاح", "success")
        return redirect(url_for("projects"))

    items = Project.query.order_by(Project.start_date.desc()).all()
    return render_template("projects.html", items=items)


@app.route("/projects/<int:project_id>", methods=["GET", "POST"])
def project_detail(project_id):
    project = Project.query.get_or_404(project_id)
    if request.method == "POST":
        boq_item = BOQItem(
            project_id=project.id,
            name=request.form.get("name"),
            estimated_cost=float(request.form.get("estimated_cost") or 0),
            quantity=float(request.form.get("quantity") or 0),
            execution_percentage=float(request.form.get("execution_percentage") or 0),
            stage=request.form.get("stage"),
        )
        db.session.add(boq_item)
        db.session.commit()
        flash("تم إضافة بند الأعمال بنجاح", "success")
        return redirect(url_for("project_detail", project_id=project.id))

    boq_items = BOQItem.query.filter_by(project_id=project.id).all()
    costs = CostEntry.query.filter_by(project_id=project.id).all()
    purchase_orders = PurchaseOrder.query.filter_by(project_id=project.id).all()
    inventory_transactions = InventoryTransaction.query.filter_by(project_id=project.id).all()
    cost_summary = {
        "total_cost": sum(cost.amount for cost in costs),
        "materials": sum(cost.amount for cost in costs if cost.cost_type == "مواد"),
        "labor": sum(cost.amount for cost in costs if cost.cost_type == "عمالة"),
        "equipment": sum(cost.amount for cost in costs if cost.cost_type == "معدات"),
        "indirect": sum(cost.amount for cost in costs if cost.cost_type == "غير مباشرة"),
        "supervision": sum(cost.amount for cost in costs if cost.cost_type == "اشراف"),
        "overhead": sum(cost.amount for cost in costs if cost.cost_type == "مصاريف ادارية"),
    }
    return render_template(
        "project_detail.html",
        project=project,
        boq_items=boq_items,
        costs=costs,
        purchase_orders=purchase_orders,
        inventory_transactions=inventory_transactions,
        cost_summary=cost_summary,
    )


@app.route("/progress_payments", methods=["GET", "POST"])
def progress_payments():
    projects = Project.query.order_by(Project.code).all()
    subcontractors = Subcontractor.query.order_by(Subcontractor.name).all()
    boq_items = BOQItem.query.order_by(BOQItem.name).all()
    if request.method == "POST":
        project_id = int(request.form.get("project_id"))
        subcontractor_id = request.form.get("subcontractor_id") or None
        payment = ProgressPayment(
            project_id=project_id,
            subcontractor_id=subcontractor_id,
            period_start=request.form.get("period_start") or None,
            period_end=request.form.get("period_end") or None,
            discount_insurance=float(request.form.get("discount_insurance") or 0),
            tax=float(request.form.get("tax") or 0),
            penalties=float(request.form.get("penalties") or 0),
            notes=request.form.get("notes"),
        )
        db.session.add(payment)
        db.session.commit()

        total_value = 0
        for index, boq_item_id in enumerate(request.form.getlist("boq_item_id")):
            try:
                quantity = float(request.form.getlist("quantity")[index] or 0)
                value = float(request.form.getlist("value")[index] or 0)
            except (ValueError, IndexError):
                quantity = 0
                value = 0
            if boq_item_id and value > 0:
                item = ProgressPaymentItem(
                    progress_payment_id=payment.id,
                    boq_item_id=int(boq_item_id),
                    description=request.form.getlist("description")[index],
                    quantity=quantity,
                    value=value,
                )
                total_value += value
                db.session.add(item)
        payment.total_value = total_value
        payment.net_value = total_value - payment.discount_insurance - payment.tax - payment.penalties
        if payment.net_value < 0:
            payment.net_value = 0
        db.session.commit()

        # قيد محاسبي تلقائي
        debit_account = ChartOfAccount.query.filter(ChartOfAccount.category == "المصروفات").first()
        credit_account = ChartOfAccount.query.filter(ChartOfAccount.category == "الالتزامات").first()
        if debit_account and credit_account:
            journal = JournalEntry(
                date=date.today(),
                description=f"مستخلص تقدم للمشروع {payment.project.code}",
                debit_account_id=debit_account.id,
                credit_account_id=credit_account.id,
                amount=payment.net_value,
                project_id=payment.project_id,
            )
            db.session.add(journal)
            db.session.commit()

        flash("تم إضافة المستخلص بنجاح وتم إنشاء قيد محاسبي تلقائي", "success")
        return redirect(url_for("progress_payments"))

    payments = ProgressPayment.query.order_by(ProgressPayment.id.desc()).all()
    return render_template(
        "progress_payments.html",
        payments=payments,
        projects=projects,
        subcontractors=subcontractors,
        boq_items=boq_items,
    )


@app.route("/subcontractors", methods=["GET", "POST"])
def subcontractors():
    if request.method == "POST":
        subcontractor = Subcontractor(
            name=request.form.get("name"),
            contract_value=float(request.form.get("contract_value") or 0),
            discount_percentage=float(request.form.get("discount_percentage") or 0),
            notes=request.form.get("notes"),
        )
        db.session.add(subcontractor)
        db.session.commit()
        flash("تم إضافة مقاول الباطن بنجاح", "success")
        return redirect(url_for("subcontractors"))
    items = Subcontractor.query.order_by(Subcontractor.name).all()
    return render_template("subcontractors.html", items=items)


@app.route("/suppliers", methods=["GET", "POST"])
def suppliers():
    if request.method == "POST":
        supplier = Supplier(
            name=request.form.get("name"),
            contact_info=request.form.get("contact_info"),
            notes=request.form.get("notes"),
        )
        db.session.add(supplier)
        db.session.commit()
        flash("تم إضافة مورد بنجاح", "success")
        return redirect(url_for("suppliers"))
    items = Supplier.query.order_by(Supplier.name).all()
    return render_template("suppliers.html", items=items)


@app.route("/purchase_orders", methods=["GET", "POST"])
def purchase_orders():
    projects = Project.query.order_by(Project.code).all()
    suppliers = Supplier.query.order_by(Supplier.name).all()
    if request.method == "POST":
        order = PurchaseOrder(
            project_id=int(request.form.get("project_id")),
            supplier_id=request.form.get("supplier_id") or None,
            order_number=request.form.get("order_number"),
            invoice_number=request.form.get("invoice_number"),
            date=request.form.get("date") or None,
            status=request.form.get("status"),
            total_value=float(request.form.get("total_value") or 0),
            notes=request.form.get("notes"),
        )
        db.session.add(order)
        db.session.commit()

        # قيد محاسبي تلقائي لأمر الشراء
        debit_account = ChartOfAccount.query.filter(ChartOfAccount.category.in_(["مواد", "الأصول"])) .first()
        credit_account = ChartOfAccount.query.filter(ChartOfAccount.category == "موردين").first()
        if debit_account and credit_account and order.total_value > 0:
            journal = JournalEntry(
                date=order.date or date.today().isoformat(),
                description=f"أمر شراء {order.order_number or order.invoice_number} للمشروع {order.project.code}",
                debit_account_id=debit_account.id,
                credit_account_id=credit_account.id,
                amount=order.total_value,
                project_id=order.project_id,
            )
            db.session.add(journal)
            db.session.commit()

        flash("تم حفظ أمر الشراء بنجاح", "success")
        return redirect(url_for("purchase_orders"))
    items = PurchaseOrder.query.order_by(PurchaseOrder.date.desc()).all()
    return render_template("purchase_orders.html", items=items, projects=projects, suppliers=suppliers)


@app.route("/inventory", methods=["GET", "POST"])
def inventory():
    projects = Project.query.order_by(Project.code).all()
    if request.method == "POST":
        destination_warehouse = request.form.get("destination_warehouse") or ""
        transaction = InventoryTransaction(
            project_id=int(request.form.get("project_id")),
            warehouse_name=request.form.get("warehouse_name"),
            material_name=request.form.get("material_name"),
            quantity=float(request.form.get("quantity") or 0),
            unit_cost=float(request.form.get("unit_cost") or 0),
            transaction_type=request.form.get("transaction_type"),
            date=request.form.get("date") or None,
            notes=request.form.get("notes") or "",
        )
        if request.form.get("transaction_type") == "تحويل" and destination_warehouse:
            transaction.destination_warehouse = destination_warehouse
            transaction.notes = f"تحويل إلى {destination_warehouse} " + transaction.notes
        db.session.add(transaction)
        db.session.commit()

        # قيد محاسبي تلقائي لحركة المخزون
        amount = transaction.quantity * transaction.unit_cost
        if amount > 0:
            if transaction.transaction_type == "إضافة":
                debit_account = ChartOfAccount.query.filter(ChartOfAccount.category.in_(["مواد", "الأصول"])).first()
                credit_account = ChartOfAccount.query.filter(ChartOfAccount.category == "موردين").first()
            elif transaction.transaction_type == "سحب":
                debit_account = ChartOfAccount.query.filter(ChartOfAccount.category == "المصروفات").first()
                credit_account = ChartOfAccount.query.filter(ChartOfAccount.category.in_(["مواد", "الأصول"])).first()
            else:
                debit_account = None
                credit_account = None

            if debit_account and credit_account:
                journal = JournalEntry(
                    date=transaction.date or date.today().isoformat(),
                    description=f"حركة مخزون {transaction.transaction_type} للمادة {transaction.material_name}",
                    debit_account_id=debit_account.id,
                    credit_account_id=credit_account.id,
                    amount=amount,
                    project_id=transaction.project_id,
                )
                db.session.add(journal)
                db.session.commit()

        flash("تم تسجيل حركة المخزون بنجاح", "success")
        return redirect(url_for("inventory"))
    items = InventoryTransaction.query.order_by(InventoryTransaction.date.desc()).all()
    return render_template("inventory.html", items=items, projects=projects)


@app.route("/inventory_report")
def inventory_report():
    transactions = InventoryTransaction.query.order_by(InventoryTransaction.material_name, InventoryTransaction.date).all()
    material_reports = []
    grouped = defaultdict(lambda: {
        "project": None,
        "material_name": None,
        "receipt_qty": 0,
        "receipt_value": 0,
        "issue_qty": 0,
        "issue_value": 0,
        "transfer_qty": 0,
        "fifo_remaining": deque(),
        "fifo_cost": 0,
    })

    for tx in transactions:
        key = (tx.project_id, tx.material_name)
        group = grouped[key]
        if group["project"] is None:
            group["project"] = tx.project
            group["material_name"] = tx.material_name
        if tx.transaction_type == "إضافة":
            group["receipt_qty"] += tx.quantity
            group["receipt_value"] += tx.quantity * tx.unit_cost
            group["fifo_remaining"].append([tx.quantity, tx.unit_cost])
        elif tx.transaction_type in ("سحب", "تحويل"):
            group["issue_qty"] += tx.quantity
            group["issue_value"] += tx.quantity * tx.unit_cost
            if tx.transaction_type == "تحويل":
                group["transfer_qty"] += tx.quantity
            quantity = tx.quantity
            while quantity > 0 and group["fifo_remaining"]:
                lot = group["fifo_remaining"][0]
                if quantity >= lot[0]:
                    group["fifo_cost"] += lot[0] * lot[1]
                    quantity -= lot[0]
                    group["fifo_remaining"].popleft()
                else:
                    group["fifo_cost"] += quantity * lot[1]
                    lot[0] -= quantity
                    quantity = 0

    for key, group in grouped.items():
        closing_qty = group["receipt_qty"] - group["issue_qty"]
        avg_cost = group["receipt_value"] / group["receipt_qty"] if group["receipt_qty"] else 0
        closing_value_avg = closing_qty * avg_cost
        fifo_value = sum(lot[0] * lot[1] for lot in group["fifo_remaining"])
        material_reports.append({
            "project": group["project"],
            "material_name": group["material_name"],
            "receipt_qty": group["receipt_qty"],
            "receipt_value": group["receipt_value"],
            "issue_qty": group["issue_qty"],
            "issue_value": group["issue_value"],
            "closing_qty": closing_qty,
            "closing_value_avg": closing_value_avg,
            "closing_value_fifo": fifo_value,
            "avg_cost": avg_cost,
        })
    return render_template("inventory_report.html", material_reports=material_reports)


@app.route("/costs", methods=["GET", "POST"])
def costs():
    projects = Project.query.order_by(Project.code).all()
    boq_items = BOQItem.query.order_by(BOQItem.name).all()
    if request.method == "POST":
        cost_entry = CostEntry(
            project_id=int(request.form.get("project_id")),
            boq_item_id=request.form.get("boq_item_id") or None,
            cost_type=request.form.get("cost_type"),
            amount=float(request.form.get("amount") or 0),
            description=request.form.get("description"),
            cost_center=request.form.get("cost_center"),
        )
        db.session.add(cost_entry)
        db.session.commit()
        flash("تم إضافة قيد تكلفة بنجاح", "success")
        return redirect(url_for("costs"))
    entries = CostEntry.query.order_by(CostEntry.id.desc()).all()
    return render_template("costs.html", entries=entries, projects=projects, boq_items=boq_items)


@app.route("/project_report")
def project_report():
    projects = Project.query.order_by(Project.code).all()
    project_summaries = []
    for project in projects:
        total_cost = sum(cost.amount for cost in project.cost_entries)
        project_summaries.append({
            "project": project,
            "total_cost": total_cost,
            "labour_cost": sum(cost.amount for cost in project.cost_entries if cost.cost_type == "عمالة"),
            "material_cost": sum(cost.amount for cost in project.cost_entries if cost.cost_type == "مواد"),
            "equipment_cost": sum(cost.amount for cost in project.cost_entries if cost.cost_type == "معدات"),
            "indirect_cost": sum(cost.amount for cost in project.cost_entries if cost.cost_type == "غير مباشرة"),
            "supervision_cost": sum(cost.amount for cost in project.cost_entries if cost.cost_type == "اشراف"),
            "overhead_cost": sum(cost.amount for cost in project.cost_entries if cost.cost_type == "مصاريف ادارية"),
        })
    boq_items = BOQItem.query.order_by(BOQItem.name).all()
    item_summaries = []
    for item in boq_items:
        item_summaries.append({
            "item": item,
            "total_cost": sum(cost.amount for cost in item.cost_entries),
            "quantity": item.quantity,
            "cost_per_unit": sum(cost.amount for cost in item.cost_entries) / item.quantity if item.quantity else 0,
        })
    return render_template("project_report.html", project_summaries=project_summaries, item_summaries=item_summaries)


@app.route("/labor", methods=["GET", "POST"])
def labor():
    projects = Project.query.order_by(Project.code).all()
    if request.method == "POST":
        labor = LaborEntry(
            project_id=int(request.form.get("project_id")),
            date=request.form.get("date") or None,
            description=request.form.get("description"),
            hours=float(request.form.get("hours") or 0),
            amount=float(request.form.get("amount") or 0),
            advances=float(request.form.get("advances") or 0),
            deductions=float(request.form.get("deductions") or 0),
        )
        db.session.add(labor)
        db.session.commit()
        flash("تم تسجيل العمالة بنجاح", "success")
        return redirect(url_for("labor"))
    entries = LaborEntry.query.order_by(LaborEntry.date.desc()).all()
    return render_template("labor.html", entries=entries, projects=projects)


@app.route("/equipment", methods=["GET", "POST"])
def equipment():
    projects = Project.query.order_by(Project.code).all()
    if request.method == "POST":
        equip = Equipment(
            name=request.form.get("name"),
            purchase_cost=float(request.form.get("purchase_cost") or 0),
            operating_cost=float(request.form.get("operating_cost") or 0),
            maintenance=float(request.form.get("maintenance") or 0),
            hours_used=float(request.form.get("hours_used") or 0),
            project_id=request.form.get("project_id") or None,
        )
        db.session.add(equip)
        db.session.commit()
        flash("تم حفظ بيانات المعدات بنجاح", "success")
        return redirect(url_for("equipment"))
    entries = Equipment.query.order_by(Equipment.name).all()
    return render_template("equipment.html", entries=entries, projects=projects)


@app.route("/journal", methods=["GET", "POST"])
def journal():
    if request.method == "POST":
        debit_account_id = int(request.form.get("debit_account_id") or 0)
        credit_account_id = int(request.form.get("credit_account_id") or 0)
        amount = float(request.form.get("amount") or 0)
        entry_date = request.form.get("date") or date.today().isoformat()
        description = (request.form.get("description") or "").strip()
        project_id = request.form.get("project_id") or None

        if not description:
            flash("يرجى إدخال وصف القيد", "danger")
            return redirect(url_for("journal"))
        if amount <= 0:
            flash("يرجى إدخال مبلغ أكبر من صفر", "danger")
            return redirect(url_for("journal"))
        if debit_account_id <= 0 or credit_account_id <= 0:
            flash("يرجى اختيار الحساب المدين والدائن", "danger")
            return redirect(url_for("journal"))
        if debit_account_id == credit_account_id:
            flash("لا يمكن أن يكون الحساب المدين هو نفسه الحساب الدائن", "danger")
            return redirect(url_for("journal"))

        entry = JournalEntry(
            date=entry_date,
            description=description,
            debit_account_id=debit_account_id,
            credit_account_id=credit_account_id,
            amount=amount,
            project_id=project_id,
        )
        db.session.add(entry)
        db.session.commit()
        flash("تم إضافة القيد اليدوي بنجاح", "success")
        return redirect(url_for("journal"))

    entries = JournalEntry.query.order_by(JournalEntry.date.desc(), JournalEntry.id.desc()).all()
    accounts = ChartOfAccount.query.order_by(ChartOfAccount.code, ChartOfAccount.name).all()
    projects = Project.query.order_by(Project.code).all()
    return render_template(
        "journal.html",
        entries=entries,
        accounts=accounts,
        projects=projects,
        today_date=date.today().isoformat(),
    )


@app.route("/journal/<int:entry_id>")
def journal_entry_detail(entry_id):
    entry = JournalEntry.query.get_or_404(entry_id)
    accounts = ChartOfAccount.query.order_by(ChartOfAccount.code, ChartOfAccount.name).all()
    projects = Project.query.order_by(Project.code).all()
    return render_template("journal_entry_detail.html", entry=entry, accounts=accounts, projects=projects)


@app.route("/journal/<int:entry_id>/update", methods=["POST"])
def update_journal_entry(entry_id):
    entry = JournalEntry.query.get_or_404(entry_id)

    debit_account_id = int(request.form.get("debit_account_id") or 0)
    credit_account_id = int(request.form.get("credit_account_id") or 0)
    amount = float(request.form.get("amount") or 0)
    entry_date = request.form.get("date") or entry.date
    description = (request.form.get("description") or "").strip()
    project_id = request.form.get("project_id") or None

    if not description:
        flash("يرجى إدخال وصف القيد", "danger")
        return redirect(url_for("journal_entry_detail", entry_id=entry.id))
    if amount <= 0:
        flash("يرجى إدخال مبلغ أكبر من صفر", "danger")
        return redirect(url_for("journal_entry_detail", entry_id=entry.id))
    if debit_account_id <= 0 or credit_account_id <= 0:
        flash("يرجى اختيار الحساب المدين والدائن", "danger")
        return redirect(url_for("journal_entry_detail", entry_id=entry.id))
    if debit_account_id == credit_account_id:
        flash("لا يمكن أن يكون الحساب المدين هو نفسه الحساب الدائن", "danger")
        return redirect(url_for("journal_entry_detail", entry_id=entry.id))

    entry.date = entry_date
    entry.description = description
    entry.debit_account_id = debit_account_id
    entry.credit_account_id = credit_account_id
    entry.amount = amount
    entry.project_id = project_id
    db.session.commit()
    flash("تم تحديث القيد بنجاح", "success")
    return redirect(url_for("journal_entry_detail", entry_id=entry.id))


@app.route("/journal/<int:entry_id>/copy", methods=["POST"])
def copy_journal_entry(entry_id):
    source = JournalEntry.query.get_or_404(entry_id)
    copied_entry = JournalEntry(
        date=date.today().isoformat(),
        description=f"نسخ من قيد رقم {source.id}: {source.description}",
        debit_account_id=source.debit_account_id,
        credit_account_id=source.credit_account_id,
        amount=source.amount,
        project_id=source.project_id,
    )
    db.session.add(copied_entry)
    db.session.commit()
    flash("تم نسخ القيد بنجاح", "success")
    return redirect(url_for("journal"))


@app.route("/journal/<int:entry_id>/reverse", methods=["POST"])
def reverse_journal_entry(entry_id):
    source = JournalEntry.query.get_or_404(entry_id)
    reversed_entry = JournalEntry(
        date=date.today().isoformat(),
        description=f"عكس قيد رقم {source.id}: {source.description}",
        debit_account_id=source.credit_account_id,
        credit_account_id=source.debit_account_id,
        amount=source.amount,
        project_id=source.project_id,
    )
    db.session.add(reversed_entry)
    db.session.commit()
    flash("تم إنشاء قيد عكسي بنجاح", "success")
    return redirect(url_for("journal"))


@app.route("/journal/<int:entry_id>/delete", methods=["POST"])
def delete_journal_entry(entry_id):
    entry = JournalEntry.query.get_or_404(entry_id)
    db.session.delete(entry)
    db.session.commit()
    flash("تم حذف القيد بنجاح", "success")
    return redirect(url_for("journal"))


if __name__ == "__main__":
    app.run(debug=True)
