from collections import defaultdict, deque
from datetime import date

from flask import render_template, request

from models import (
    BOQItem, ChartOfAccount, CustodySettlement, Estimation, InventoryTransaction,
    ProgressPayment, Project, db,
)
from services.accounting import (
    EXPENSE_CLASS_OPTIONS, as_float, build_account_balances, build_custody_balances,
    build_project_cost_breakdown, classify_balance_sheet_section, client_progress_query,
    get_age_bucket, get_journal_entries_in_range, get_main_treasury_rollup_balance,
    is_treasury_account, split_treasury_accounts, sync_journal_related_accounts,
)


def register(app):


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


    @app.route("/reports/general_ledger")
    def general_ledger_report():
        from_date = (request.args.get("from_date") or "").strip()
        to_date = (request.args.get("to_date") or "").strip()

        accounts = ChartOfAccount.query.order_by(ChartOfAccount.code, ChartOfAccount.name).all()
        account_map = {account.id: account for account in accounts}
        entries = get_journal_entries_in_range(from_date or None, to_date or None)

        totals_by_account = defaultdict(lambda: {"debit": 0.0, "credit": 0.0})
        movements_by_account = defaultdict(list)

        for entry in entries:
            amount = as_float(entry.amount)

            totals_by_account[entry.debit_account_id]["debit"] += amount
            movements_by_account[entry.debit_account_id].append(
                {
                    "date": entry.date,
                    "reference": entry.reference or entry.display_number,
                    "description": entry.description,
                    "project": entry.project.display_name if entry.project else "-",
                    "user_name": entry.updated_by_name or entry.created_by_name or "-",
                    "debit": amount,
                    "credit": 0.0,
                    "status": entry.status,
                }
            )

            totals_by_account[entry.credit_account_id]["credit"] += amount
            movements_by_account[entry.credit_account_id].append(
                {
                    "date": entry.date,
                    "reference": entry.reference or entry.display_number,
                    "description": entry.description,
                    "project": entry.project.display_name if entry.project else "-",
                    "user_name": entry.updated_by_name or entry.created_by_name or "-",
                    "debit": 0.0,
                    "credit": amount,
                    "status": entry.status,
                }
            )

        ledger_accounts = []
        for account in accounts:
            account_total = totals_by_account.get(account.id)
            account_movements = movements_by_account.get(account.id, [])
            if not account_total and not account_movements:
                continue

            opening_balance = as_float(getattr(account, "opening_balance", 0))
            debit_total = (account_total or {}).get("debit", 0.0)
            credit_total = (account_total or {}).get("credit", 0.0)
            closing_balance = opening_balance + debit_total - credit_total

            ledger_accounts.append(
                {
                    "account": account,
                    "opening_balance": opening_balance,
                    "debit_total": debit_total,
                    "credit_total": credit_total,
                    "closing_balance": closing_balance,
                    "movements": sorted(account_movements, key=lambda x: (x["date"] or "", x["reference"] or "")),
                }
            )

        return render_template(
            "general_ledger.html",
            ledger_accounts=ledger_accounts,
            from_date=from_date,
            to_date=to_date,
        )


    @app.route("/reports/trial_balance")
    def trial_balance_report():
        from_date = (request.args.get("from_date") or "").strip()
        to_date = (request.args.get("to_date") or "").strip()
        accounts = ChartOfAccount.query.order_by(ChartOfAccount.code, ChartOfAccount.name).all()
        entries = get_journal_entries_in_range(from_date or None, to_date or None)

        period_totals = defaultdict(lambda: {"debit": 0.0, "credit": 0.0})
        for entry in entries:
            amount = as_float(entry.amount)
            period_totals[entry.debit_account_id]["debit"] += amount
            period_totals[entry.credit_account_id]["credit"] += amount

        rows = []
        total_debit_balance = 0.0
        total_credit_balance = 0.0
        for account in accounts:
            opening_balance = as_float(getattr(account, "opening_balance", 0))
            period_debit = period_totals[account.id]["debit"]
            period_credit = period_totals[account.id]["credit"]
            closing_balance = opening_balance + period_debit - period_credit

            debit_balance = closing_balance if closing_balance > 0 else 0.0
            credit_balance = abs(closing_balance) if closing_balance < 0 else 0.0

            if abs(opening_balance) < 0.000001 and abs(period_debit) < 0.000001 and abs(period_credit) < 0.000001 and abs(closing_balance) < 0.000001:
                continue

            total_debit_balance += debit_balance
            total_credit_balance += credit_balance
            rows.append(
                {
                    "account": account,
                    "opening_balance": opening_balance,
                    "period_debit": period_debit,
                    "period_credit": period_credit,
                    "closing_balance": closing_balance,
                    "debit_balance": debit_balance,
                    "credit_balance": credit_balance,
                }
            )

        return render_template(
            "trial_balance.html",
            rows=rows,
            from_date=from_date,
            to_date=to_date,
            total_debit_balance=total_debit_balance,
            total_credit_balance=total_credit_balance,
        )


    @app.route("/reports/profit_loss")
    def profit_loss_report():
        from_date = (request.args.get("from_date") or "").strip()
        to_date = (request.args.get("to_date") or "").strip()
        entries = get_journal_entries_in_range(from_date or None, to_date or None)
        accounts = ChartOfAccount.query.order_by(ChartOfAccount.code, ChartOfAccount.name).all()
        account_map = {account.id: account for account in accounts}

        # حسابات مقاولي الباطن/الموردين هي حسابات أطراف (التزامات) وليست مصروفات،
        # المصروف يُسجل على حسابات المصروفات المبوبة حتى لا تتكرر التكلفة مرتين.
        revenue_categories = {"الإيرادات", "فروق أسعار"}
        expense_categories = {"المصروفات", "مواد", "عمالة مباشرة", "إيجار معدات"}
        net_by_account = defaultdict(float)

        for entry in entries:
            amount = as_float(entry.amount)
            net_by_account[entry.debit_account_id] += amount
            net_by_account[entry.credit_account_id] -= amount

        revenue_lines = []
        expense_lines = []
        revenue_total = 0.0
        expense_total = 0.0

        for account_id, net_amount in net_by_account.items():
            account = account_map.get(account_id)
            if not account:
                continue
            if account.category in revenue_categories:
                line_amount = max(-net_amount, 0.0)
                if line_amount > 0:
                    revenue_total += line_amount
                    revenue_lines.append({"account": account, "amount": line_amount})
            elif account.category in expense_categories:
                line_amount = max(net_amount, 0.0)
                if line_amount > 0:
                    expense_total += line_amount
                    expense_lines.append({"account": account, "amount": line_amount})

        net_profit = revenue_total - expense_total

        return render_template(
            "profit_loss.html",
            revenue_lines=sorted(revenue_lines, key=lambda x: (x["account"].code, x["account"].name)),
            expense_lines=sorted(expense_lines, key=lambda x: (x["account"].code, x["account"].name)),
            revenue_total=revenue_total,
            expense_total=expense_total,
            net_profit=net_profit,
            from_date=from_date,
            to_date=to_date,
        )


    @app.route("/reports/treasury_dynamics")
    def treasury_dynamics_report():
        from_date = (request.args.get("from_date") or "").strip()
        to_date = (request.args.get("to_date") or "").strip()
        accounts = ChartOfAccount.query.order_by(ChartOfAccount.code, ChartOfAccount.name).all()
        balances = build_account_balances(accounts)
        main_treasury_account, sub_treasury_accounts = split_treasury_accounts(accounts)
        treasury_accounts = ([main_treasury_account] if main_treasury_account else []) + sub_treasury_accounts
        entries = get_journal_entries_in_range(from_date or None, to_date or None)

        treasury_ids = {account.id for account in treasury_accounts}
        accounts_by_id = {account.id: account for account in accounts}
        movements = []
        total_inflow = 0.0
        total_outflow = 0.0
        # تبويب المصروفات المنصرفة من الخزن: مباشرة / غير مباشرة / إدارية (SRS 3.1)
        expense_class_totals = {label: 0.0 for label in EXPENSE_CLASS_OPTIONS}
        expense_class_totals["غير مبوبة"] = 0.0
        for entry in entries:
            amount = as_float(entry.amount)
            debit_is_treasury = entry.debit_account_id in treasury_ids
            credit_is_treasury = entry.credit_account_id in treasury_ids
            if not debit_is_treasury and not credit_is_treasury:
                continue

            # التحويل الداخلي بين خزنتين لا يغير السيولة الكلية، لذلك لا يُضاف لإجماليات المقبوض/المدفوع.
            is_internal_transfer = debit_is_treasury and credit_is_treasury
            inflow = 0.0 if is_internal_transfer else (amount if debit_is_treasury else 0.0)
            outflow = 0.0 if is_internal_transfer else (amount if credit_is_treasury else 0.0)
            total_inflow += inflow
            total_outflow += outflow

            expense_class = ""
            if outflow > 0:
                counter_account = accounts_by_id.get(entry.debit_account_id)
                if counter_account and (counter_account.category or "").strip() == "المصروفات":
                    expense_class = (counter_account.expense_class or "").strip() or "غير مبوبة"
                    expense_class_totals[expense_class] = expense_class_totals.get(expense_class, 0.0) + outflow

            movements.append(
                {
                    "expense_class": expense_class,
                    "date": entry.date,
                    "reference": entry.reference or entry.display_number,
                    "description": f"{entry.description} (تحويل داخلي)" if is_internal_transfer else entry.description,
                    "debit_account": entry.debit_account.name if entry.debit_account else "-",
                    "credit_account": entry.credit_account.name if entry.credit_account else "-",
                    "inflow": inflow,
                    "outflow": outflow,
                    "project": entry.project.display_name if entry.project else "-",
                    "user_name": entry.updated_by_name or entry.created_by_name or "-",
                }
            )

        main_own_balance = balances.get(main_treasury_account.id, 0.0) if main_treasury_account else 0.0
        sub_total_balance = sum(balances.get(account.id, 0.0) for account in sub_treasury_accounts)
        main_rollup_balance = main_own_balance + sub_total_balance

        treasury_rows = [
            {
                "account": account,
                "balance": balances.get(account.id, 0.0),
                "is_rollup": False,
            }
            for account in treasury_accounts
        ]
        if main_treasury_account:
            treasury_rows.insert(
                0,
                {
                    "account": main_treasury_account,
                    "balance": main_rollup_balance,
                    "is_rollup": True,
                },
            )

        return render_template(
            "treasury_dynamics.html",
            treasury_rows=treasury_rows,
            movements=movements,
            from_date=from_date,
            to_date=to_date,
            total_inflow=total_inflow,
            total_outflow=total_outflow,
            net_cash_flow=total_inflow - total_outflow,
            main_rollup_balance=main_rollup_balance,
            main_own_balance=main_own_balance,
            sub_total_balance=sub_total_balance,
            expense_class_totals={
                label: round(value, 2) for label, value in expense_class_totals.items() if value
            },
        )


    @app.route("/reports/entity_accounts")
    def entity_accounts_report():
        from_date = (request.args.get("from_date") or "").strip()
        to_date = (request.args.get("to_date") or "").strip()
        accounts = ChartOfAccount.query.order_by(ChartOfAccount.category, ChartOfAccount.code).all()
        balances = build_account_balances(accounts)
        entries = get_journal_entries_in_range(from_date or None, to_date or None)

        tracked_categories = ["المعدات", "السواقين", "المناديب", "الموردين", "مقاولي الباطن", "العملاء", "الموظفين"]
        tracked_ids = {account.id for account in accounts if account.category in tracked_categories}
        period_totals = defaultdict(lambda: {"debit": 0.0, "credit": 0.0})

        for entry in entries:
            amount = as_float(entry.amount)
            if entry.debit_account_id in tracked_ids:
                period_totals[entry.debit_account_id]["debit"] += amount
            if entry.credit_account_id in tracked_ids:
                period_totals[entry.credit_account_id]["credit"] += amount

        grouped_rows = defaultdict(list)
        for account in accounts:
            if account.category not in tracked_categories:
                continue
            grouped_rows[account.category].append(
                {
                    "account": account,
                    "period_debit": period_totals[account.id]["debit"],
                    "period_credit": period_totals[account.id]["credit"],
                    "current_balance": balances.get(account.id, 0.0),
                }
            )

        return render_template(
            "entity_accounts_report.html",
            grouped_rows=grouped_rows,
            from_date=from_date,
            to_date=to_date,
        )


    @app.route("/reports/aging")
    def aging_report():
        from_date = (request.args.get("from_date") or "").strip()
        to_date = (request.args.get("to_date") or "").strip()
        entries = get_journal_entries_in_range(from_date or None, to_date or None)
        # مقاول الباطن يُعامل ماليًا كمورد (SRS 2.1) فيدخل في أعمار الديون الدائنة
        payable_categories = ["الموردين", "موردين", "مقاولي الباطن", "الموظفين"]
        accounts = ChartOfAccount.query.filter(ChartOfAccount.category.in_(payable_categories + ["العملاء"]))\
            .order_by(ChartOfAccount.category, ChartOfAccount.code).all()
        account_map = {account.id: account for account in accounts}

        rows_map = {
            account.id: {
                "account": account,
                "current": 0.0,
                "30": 0.0,
                "60": 0.0,
                "90": 0.0,
                "total": 0.0,
            }
            for account in accounts
        }

        for entry in entries:
            amount = as_float(entry.amount)
            bucket = get_age_bucket(entry.date)

            if entry.debit_account_id in rows_map:
                account = account_map[entry.debit_account_id]
                sign = 1.0 if account.category == "العملاء" else -1.0
                rows_map[entry.debit_account_id][bucket] += amount * sign

            if entry.credit_account_id in rows_map:
                account = account_map[entry.credit_account_id]
                sign = 1.0 if account.category in payable_categories else -1.0
                rows_map[entry.credit_account_id][bucket] += amount * sign

        grouped_rows = defaultdict(list)
        for account_id, row in rows_map.items():
            row["total"] = row["current"] + row["30"] + row["60"] + row["90"]
            grouped_rows[row["account"].category].append(row)

        return render_template(
            "aging_report.html",
            grouped_rows=grouped_rows,
            from_date=from_date,
            to_date=to_date,
        )


    @app.route("/reports/balance_sheet")
    def balance_sheet_report():
        accounts = ChartOfAccount.query.order_by(ChartOfAccount.code, ChartOfAccount.name).all()
        balances = build_account_balances(accounts)

        assets = []
        liabilities = []
        equity = []
        for account in accounts:
            balance = balances.get(account.id, 0.0)
            section = classify_balance_sheet_section(account)
            row = {"account": account, "balance": balance}
            if section == "assets":
                assets.append(row)
            elif section == "liabilities":
                liabilities.append(row)
            elif section == "equity":
                equity.append(row)

        total_assets = sum(row["balance"] for row in assets)
        total_liabilities = sum(abs(row["balance"]) for row in liabilities)
        total_equity = sum(row["balance"] for row in equity)
        if abs(total_equity) < 0.000001:
            total_equity = total_assets - total_liabilities

        return render_template(
            "balance_sheet.html",
            assets=assets,
            liabilities=liabilities,
            equity=equity,
            total_assets=total_assets,
            total_liabilities=total_liabilities,
            total_equity=total_equity,
        )


    @app.route("/reports/cash_flow")
    def cash_flow_report():
        from_date = (request.args.get("from_date") or "").strip()
        to_date = (request.args.get("to_date") or "").strip()
        accounts = ChartOfAccount.query.order_by(ChartOfAccount.code, ChartOfAccount.name).all()
        treasury_ids = {account.id for account in accounts if is_treasury_account(account)}
        entries = get_journal_entries_in_range(from_date or None, to_date or None)

        operating_lines = []
        inflow_total = 0.0
        outflow_total = 0.0
        for entry in entries:
            amount = as_float(entry.amount)
            if entry.debit_account_id in treasury_ids:
                inflow_total += amount
                operating_lines.append(
                    {
                        "date": entry.date,
                        "reference": entry.reference or entry.display_number,
                        "description": entry.description,
                        "inflow": amount,
                        "outflow": 0.0,
                    }
                )
            if entry.credit_account_id in treasury_ids:
                outflow_total += amount
                operating_lines.append(
                    {
                        "date": entry.date,
                        "reference": entry.reference or entry.display_number,
                        "description": entry.description,
                        "inflow": 0.0,
                        "outflow": amount,
                    }
                )

        net_operating_cash = inflow_total - outflow_total
        return render_template(
            "cash_flow.html",
            operating_lines=operating_lines,
            inflow_total=inflow_total,
            outflow_total=outflow_total,
            net_operating_cash=net_operating_cash,
            from_date=from_date,
            to_date=to_date,
        )


    @app.route("/project_report")
    def project_report():
        projects = Project.query.order_by(Project.code).all()
        project_summaries = []
        for project in projects:
            breakdown = build_project_cost_breakdown(project)
            project_summaries.append({
                "project": project,
                "total_cost": breakdown["total_cost"],
                "labour_cost": breakdown["labor_cost"],
                "material_cost": breakdown["material_cost"],
                "equipment_cost": breakdown["equipment_cost"],
                "indirect_cost": breakdown["custody_cost"] + breakdown["driver_cost"],
                "supervision_cost": breakdown["subcontractor_cost"],
                "overhead_cost": breakdown["admin_allocation"],
                "breakdown": breakdown,
            })
        boq_items = BOQItem.query.order_by(BOQItem.name).all()
        item_summaries = []
        for item in boq_items:
            project_cost = build_project_cost_breakdown(item.project)["total_cost"] if item.project else 0
            share = project_cost * (as_float(item.execution_percentage) / 100.0) if as_float(item.execution_percentage) else 0
            quantity = as_float(item.quantity)
            item_summaries.append({
                "item": item,
                "total_cost": round(share, 2),
                "quantity": quantity,
                "cost_per_unit": round(share / quantity, 2) if quantity else 0,
            })
        return render_template("project_report.html", project_summaries=project_summaries, item_summaries=item_summaries)


    # ---------------------------------------------------------------------------
    # تقارير إضافية مطلوبة في SRS 5
    # ---------------------------------------------------------------------------


    @app.route("/reports/active_custody")
    def active_custody_report():
        """تقرير العهد النقدية النشطة للسواقين والمناديب (SRS 5)."""
        sync_journal_related_accounts()
        entity_filter = (request.args.get("entity_type") or "").strip()
        from_date = (request.args.get("from_date") or "").strip()
        to_date = (request.args.get("to_date") or "").strip()

        query = CustodySettlement.query
        if from_date:
            query = query.filter(CustodySettlement.date >= from_date)
        if to_date:
            query = query.filter(CustodySettlement.date <= to_date)
        settlements = query.order_by(CustodySettlement.date.asc(), CustodySettlement.id.asc()).all()

        rows = build_custody_balances(settlements)
        if entity_filter:
            rows = [row for row in rows if row["entity_type"] == entity_filter]

        today = date.today()
        for row in rows:
            days_open = 0
            if row["latest_date"]:
                try:
                    days_open = (today - date.fromisoformat(row["latest_date"])).days
                except ValueError:
                    days_open = 0
            row["days_since_last_move"] = days_open
            # عهدة نشطة = يوجد رصيد متبقٍ لم يُصفَّ بعد
            row["is_active"] = row["remaining"] > 0.009
            row["needs_audit"] = row["is_active"] and days_open > 7

        active_rows = [row for row in rows if row["is_active"]]
        totals = {
            "disbursed": round(sum(row["disbursed"] for row in rows), 2),
            "settled": round(sum(row["settled_total"] for row in rows), 2),
            "returned": round(sum(row["returned"] for row in rows), 2),
            "remaining": round(sum(row["remaining"] for row in rows), 2),
            "active_remaining": round(sum(row["remaining"] for row in active_rows), 2),
            "pending_audit": round(sum(row["remaining"] for row in rows if row["needs_audit"]), 2),
            "trip": round(sum(row["settled_trip"] for row in rows), 2),
            "daily": round(sum(row["settled_daily"] for row in rows), 2),
            "admin": round(sum(row["settled_admin"] for row in rows), 2),
        }

        return render_template(
            "active_custody_report.html",
            rows=rows,
            totals=totals,
            entity_filter=entity_filter,
            owner_types=CUSTODY_OWNER_TYPES,
            from_date=from_date,
            to_date=to_date,
        )


    @app.route("/reports/estimation_profitability")
    def estimation_profitability_report():
        """ربحية المقايسات والعمليات: القيمة المعتمدة من العميل مقابل التكلفة الفعلية (SRS 5)."""
        projects = Project.query.order_by(Project.code).all()
        project_rows = []
        for project in projects:
            approved_estimations = [
                item for item in Estimation.query.filter_by(project_id=project.id).all()
                if (item.status or "") == "معتمدة"
            ]
            billed_value = round(sum(
                as_float(item.total_value)
                for item in ProgressPayment.query.filter_by(project_id=project.id, subcontractor_id=None).all()
            ), 2)
            approved_value = round(sum(as_float(item.final_value) for item in approved_estimations), 2)
            if billed_value > 0:
                client_value = billed_value
                value_source = "مستخلصات العميل"
            elif approved_value > 0:
                client_value = approved_value
                value_source = "مقايسات معتمدة (مرجع)"
            else:
                client_value = as_float(project.contract_value)
                value_source = "قيمة العقد"
            breakdown = build_project_cost_breakdown(project)
            profit = round(client_value - breakdown["total_cost"], 2)
            margin = round((profit / client_value * 100.0), 2) if client_value else 0.0

            project_rows.append({
                "project": project,
                "approved_value": approved_value,
                "contract_value": as_float(project.contract_value),
                "client_value": client_value,
                "value_source": value_source,
                "estimations_count": len(approved_estimations),
                "profit": profit,
                "margin": margin,
                **breakdown,
            })

        estimation_rows = []
        for estimation in Estimation.query.order_by(Estimation.id.desc()).all():
            estimation_cost = None
            profit = None
            margin = None
            if estimation.project:
                breakdown = build_project_cost_breakdown(estimation.project)
                siblings = [
                    item for item in Estimation.query.filter_by(project_id=estimation.project_id).all()
                    if (item.status or "") == "معتمدة" or item.id == estimation.id
                ]
                sibling_total = sum(as_float(item.final_value) for item in siblings) or as_float(estimation.final_value) or 1
                estimation_cost = round(breakdown["total_cost"] * (as_float(estimation.final_value) / sibling_total), 2)
                profit = round(as_float(estimation.final_value) - estimation_cost, 2)
                margin = round(profit / as_float(estimation.final_value) * 100.0, 2) if as_float(estimation.final_value) else 0
            final_value = as_float(estimation.final_value)
            estimation_rows.append({
                "estimation": estimation,
                "final_value": final_value,
                "project_cost": estimation_cost,
                "profit": profit,
                "margin": margin,
            })

        totals = {
            "client_value": round(sum(row["client_value"] for row in project_rows), 2),
            "total_cost": round(sum(row["total_cost"] for row in project_rows), 2),
            "profit": round(sum(row["profit"] for row in project_rows), 2),
            "subcontractor_cost": round(sum(row["subcontractor_cost"] for row in project_rows), 2),
            "material_cost": round(sum(row["material_cost"] for row in project_rows), 2),
            "custody_cost": round(sum(row["custody_cost"] for row in project_rows), 2),
            "admin_allocation": round(sum(row["admin_allocation"] for row in project_rows), 2),
        }

        return render_template(
            "estimation_profitability.html",
            project_rows=project_rows,
            estimation_rows=estimation_rows,
            totals=totals,
        )
