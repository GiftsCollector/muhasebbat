from collections import defaultdict, deque
from datetime import date, datetime

from flask import flash, redirect, render_template, request, url_for

from models import (
    BOQItem, ChartOfAccount, ClientReceipt, CustodySettlement, Estimation,
    InventoryTransaction, JournalEntry, LaborEntry, ProgressPayment, Project,
    Subcontractor, Supplier, SupplierPayment, db,
)
from services.accounting import (
    EXPENSE_CLASS_OPTIONS, as_float, as_int, build_account_balances, build_custody_balances,
    build_inventory_valuation_rows, build_project_cost_breakdown, classify_balance_sheet_section,
    project_revenue_and_result,
    client_progress_query, format_grouped_number, get_age_bucket, get_journal_entries_in_range,
    get_main_treasury_rollup_balance, is_treasury_account, list_client_names, list_driver_names,
    split_treasury_accounts, sync_journal_related_accounts,
)
from services.authz import filter_hidden_treasuries, filter_visible_journals


def register(app):


    @app.route("/inventory_report")
    def inventory_report():
        material_reports = build_inventory_valuation_rows()
        projects = sorted(
            {row["project"] for row in material_reports if row.get("project")},
            key=lambda project: project.display_name,
        )
        return render_template(
            "inventory_report.html",
            material_reports=material_reports,
            projects=projects,
        )


    @app.route("/reports/general_ledger")
    def general_ledger_report():
        from_date = (request.args.get("from_date") or "").strip()
        to_date = (request.args.get("to_date") or "").strip()

        accounts = filter_hidden_treasuries(ChartOfAccount.query.order_by(ChartOfAccount.code, ChartOfAccount.name).all())
        account_map = {account.id: account for account in accounts}
        entries = filter_visible_journals(get_journal_entries_in_range(from_date or None, to_date or None))

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
        accounts = filter_hidden_treasuries(ChartOfAccount.query.order_by(ChartOfAccount.code, ChartOfAccount.name).all())
        entries = filter_visible_journals(get_journal_entries_in_range(from_date or None, to_date or None))

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
        entries = filter_visible_journals(get_journal_entries_in_range(from_date or None, to_date or None))
        accounts = filter_hidden_treasuries(ChartOfAccount.query.order_by(ChartOfAccount.code, ChartOfAccount.name).all())
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
        treasury_accounts = filter_hidden_treasuries(
            ([main_treasury_account] if main_treasury_account else []) + sub_treasury_accounts
        )
        if main_treasury_account and main_treasury_account not in treasury_accounts:
            main_treasury_account = None
        sub_treasury_accounts = [account for account in sub_treasury_accounts if account in treasury_accounts]
        entries = filter_visible_journals(get_journal_entries_in_range(from_date or None, to_date or None))

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
            main_treasury_account=main_treasury_account,
            expense_class_totals={
                label: round(value, 2) for label, value in expense_class_totals.items() if value
            },
        )


    @app.route("/reports/entity_accounts")
    def entity_accounts_report():
        from_date = (request.args.get("from_date") or "").strip()
        to_date = (request.args.get("to_date") or "").strip()
        accounts = filter_hidden_treasuries(ChartOfAccount.query.order_by(ChartOfAccount.category, ChartOfAccount.code).all())
        balances = build_account_balances(accounts)
        entries = filter_visible_journals(get_journal_entries_in_range(from_date or None, to_date or None))

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
        entries = filter_visible_journals(get_journal_entries_in_range(from_date or None, to_date or None))
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
        accounts = filter_hidden_treasuries(ChartOfAccount.query.order_by(ChartOfAccount.code, ChartOfAccount.name).all())
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
        accounts = filter_hidden_treasuries(ChartOfAccount.query.order_by(ChartOfAccount.code, ChartOfAccount.name).all())
        treasury_ids = {account.id for account in accounts if is_treasury_account(account)}
        entries = filter_visible_journals(get_journal_entries_in_range(from_date or None, to_date or None))

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
            result = project_revenue_and_result(project, breakdown)
            project_summaries.append({
                "project": project,
                "total_cost": breakdown["total_cost"],
                "labour_cost": breakdown["labor_cost"],
                "material_cost": breakdown["material_cost"],
                "equipment_cost": breakdown["equipment_cost"],
                "indirect_cost": breakdown["custody_cost"] + breakdown["driver_cost"],
                "supervision_cost": breakdown["subcontractor_cost"],
                "overhead_cost": breakdown["admin_allocation"],
                "other_cost": breakdown["other_cost"],
                "journal_revenue": result["journal_revenue"],
                "total_revenue": result["total_revenue"],
                "profit": result["profit"],
                "result_label": result["result_label"],
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
            breakdown = build_project_cost_breakdown(project)
            result = project_revenue_and_result(project, breakdown)

            project_rows.append({
                "project": project,
                "approved_value": result["approved_value"],
                "contract_value": result["contract_value"],
                "client_value": result["client_value"],
                "value_source": result["value_source"],
                "estimations_count": len(approved_estimations),
                "profit": result["profit"],
                "margin": result["margin"],
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
            "client_value": round(sum(row["client_value"] + as_float(row.get("journal_revenue")) for row in project_rows), 2),
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


    def _print_stamp(title):
        now = datetime.now()
        today_iso = date.today().isoformat()
        hour = now.hour % 12 or 12
        suffix = "م" if now.hour >= 12 else "ص"
        weekdays = ["الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]
        return {
            "company_name": "شركة السيد الشيخ للمقاولات العمومية",
            "print_date": today_iso,
            "print_time": f"{hour}:{now.strftime('%M')} {suffix}",
            "weekday_name": weekdays[date.today().weekday()],
            "print_title": title,
        }


    def _print_scope_args():
        scope = (request.args.get("scope") or "period").strip()
        if scope == "all":
            return "", "", True
        return (request.args.get("from_date") or "").strip(), (request.args.get("to_date") or "").strip(), False


    PRINT_SCREENS = {
        "sales": {
            "title": "تقرير المبيعات",
            "hint": "يُطبع شيت النقلات الخاص بشاشة المبيعات: العميل، الكميات، السعر، والإجمالي.",
            "back": "sales_trips",
            "party": "client",
        },
        "purchases": {
            "title": "تقرير المشتريات",
            "hint": "يُطبع شيت أوامر الشراء والكميات المستلمة من شاشة المشتريات.",
            "back": "purchase_orders",
            "party": "supplier",
        },
        "drivers": {
            "title": "تقرير محاسبة السواقين",
            "hint": "يُطبع كشوف السواقين والمستحقات والاستقطاعات والمسدد.",
            "back": "driver_compensation",
            "party": "driver",
        },
        "inventory": {
            "title": "تقرير المخزون",
            "hint": "يُطبع حركات المخزن والتحويلات من شاشة المخزون.",
            "back": "inventory",
            "party": None,
        },
        "journal": {
            "title": "تقرير القيود اليومية",
            "hint": "يُطبع قيود اليومية: المدين والدائن والمبلغ.",
            "back": "journal",
            "party": None,
        },
        "progress": {
            "title": "تقرير مستخلصات الباطن",
            "hint": "يُطبع مستخلصات مقاولي الباطن: الأعمال والاستقطاعات والصافي.",
            "back": "progress_payments",
            "party": "subcontractor",
        },
        "client_progress": {
            "title": "تقرير مستخلصات العملاء",
            "hint": "يُطبع مستخلصات العملاء المقدمة من شاشة مستخلصات العملاء.",
            "back": "client_progress_payments",
            "party": "client",
        },
        "accounts": {
            "title": "تقرير الحسابات",
            "hint": "اختَر حسابًا من الشجرة لكشف حركته، أو اطبع دليل الحسابات بالكامل.",
            "back": "accounts",
            "party": None,
        },
        "labor": {
            "title": "تقرير العمالة",
            "hint": "يُطبع سجلات ساعات العمل والمبالغ من شاشة العمالة.",
            "back": "labor",
            "party": "project",
        },
        "custody": {
            "title": "تقرير العهد",
            "hint": "يُطبع صرف وتسوية ورد العهد من شاشة العهد النقدية.",
            "back": "custody_settlements",
            "party": "custody",
        },
        "receipts": {
            "title": "تقرير تحصيل العملاء",
            "hint": "يُطبع سندات التحصيل من شاشة تحصيل العملاء.",
            "back": "client_receipts",
            "party": "client",
        },
        "supplier_payments": {
            "title": "تقرير سداد الموردين",
            "hint": "يُطبع سندات السداد من شاشة سداد الموردين.",
            "back": "supplier_payments",
            "party": "supplier",
        },
    }

    ACCOUNT_PARTY_PREFIXES = {
        "العملاء": "عميل - ",
        "السواقين": "سائق - ",
        "الموردين": "مورد - ",
        "مقاولي الباطن": "مقاول باطن - ",
    }


    def _chart_groups(accounts):
        groups = defaultdict(list)
        for account in accounts:
            groups[account.category or "أخرى"].append(account)
        return sorted(groups.items(), key=lambda item: item[0])


    def _party_name_from_account(account):
        if not account:
            return "", ""
        category = (account.category or "").strip()
        name = (account.name or "").strip()
        prefix = ACCOUNT_PARTY_PREFIXES.get(category)
        if prefix and name.startswith(prefix):
            name = name[len(prefix):].strip()
        return name, category


    @app.route("/reports/print-setup")
    def print_setup():
        screen = (request.args.get("screen") or "").strip()
        config = PRINT_SCREENS.get(screen)
        if not config:
            flash("شاشة الطباعة غير معروفة", "danger")
            return redirect(url_for("index"))
        accounts = filter_hidden_treasuries(
            ChartOfAccount.query.order_by(ChartOfAccount.category, ChartOfAccount.code, ChartOfAccount.name).all()
        )
        party = config.get("party")
        context = {
            "screen": screen,
            "screen_title": config["title"],
            "screen_hint": config["hint"],
            "back_endpoint": config["back"],
            "party": party,
            "from_date": (request.args.get("from_date") or "").strip(),
            "to_date": (request.args.get("to_date") or "").strip(),
            "chart_groups": _chart_groups(accounts),
            "client_options": list_client_names() if party == "client" else [],
            "driver_options": list_driver_names() if party == "driver" else [],
            "supplier_options": Supplier.query.order_by(Supplier.name).all() if party == "supplier" else [],
            "subcontractor_options": Subcontractor.query.order_by(Subcontractor.name).all() if party == "subcontractor" else [],
            "project_options": Project.query.order_by(Project.code).all() if party == "project" else [],
            "custody_options": [],
            "selected_supplier_id": as_int(request.args.get("supplier_id")) or "",
        }
        if party == "custody":
            context["custody_options"] = sorted({
                (item.entity_name or "").strip()
                for item in CustodySettlement.query.with_entities(CustodySettlement.entity_name).all()
                if (item[0] or "").strip()
            })
        return render_template("print_setup.html", **context)


    def _apply_date_filter(query, column, from_date, to_date, comprehensive):
        if comprehensive:
            return query
        if from_date:
            query = query.filter(column >= from_date)
        if to_date:
            query = query.filter(column <= to_date)
        return query


    def _render_generic_print(title, columns, rows, totals=None, filters=None):
        return render_template(
            "print_generic.html",
            columns=columns,
            rows=rows,
            totals=totals or [],
            filters=filters or {},
            **_print_stamp(title),
        )


    @app.route("/reports/print")
    def print_screen_report():
        screen = (request.args.get("screen") or "").strip()
        config = PRINT_SCREENS.get(screen)
        if not config:
            flash("شاشة الطباعة غير معروفة", "danger")
            return redirect(url_for("index"))
        from_date, to_date, comprehensive = _print_scope_args()
        account_id = as_int(request.args.get("account_id")) if not comprehensive else 0
        account = ChartOfAccount.query.get(account_id) if account_id else None
        party_name, party_category = _party_name_from_account(account)
        params = {}
        if not comprehensive:
            if from_date:
                params["from_date"] = from_date
            if to_date:
                params["to_date"] = to_date
            if account_id:
                params["account_id"] = account_id

        if screen == "sales":
            client_name = (request.args.get("client_name") or "").strip()
            if not client_name and party_category == "العملاء":
                client_name = party_name
            if client_name and not comprehensive:
                params["client_name"] = client_name
            params.pop("account_id", None)
            return redirect(url_for("print_sales_trips", **params))
        if screen == "purchases":
            supplier_id = as_int(request.args.get("supplier_id"))
            if not supplier_id and party_category == "الموردين" and party_name:
                supplier = Supplier.query.filter_by(name=party_name).first()
                if supplier:
                    supplier_id = supplier.id
            if supplier_id and not comprehensive:
                params["supplier_id"] = supplier_id
            params.pop("account_id", None)
            return redirect(url_for("print_purchase_orders", **params))
        if screen == "drivers":
            driver_name = (request.args.get("driver_name") or "").strip()
            if not driver_name and party_category == "السواقين":
                driver_name = party_name
            if driver_name and not comprehensive:
                params["driver_name"] = driver_name
            params.pop("account_id", None)
            return redirect(url_for("print_driver_compensation", **params))
        if screen == "accounts" and account_id:
            return redirect(url_for(
                "print_document",
                doc_type="account",
                doc_id=account_id,
                from_date=params.get("from_date"),
                to_date=params.get("to_date"),
            ))

        filters = {
            "from_date": "" if comprehensive else from_date,
            "to_date": "" if comprehensive else to_date,
            "entity_label": "",
        }

        if screen == "inventory":
            query = InventoryTransaction.query
            query = _apply_date_filter(query, InventoryTransaction.date, from_date, to_date, comprehensive)
            account_id = params.get("account_id")
            if account_id:
                query = query.filter(
                    db.or_(
                        InventoryTransaction.warehouse_account_id == account_id,
                        InventoryTransaction.destination_account_id == account_id,
                    )
                )
                account = ChartOfAccount.query.get(account_id)
                if account:
                    filters["entity_label"] = f"{account.code} — {account.name}"
            items = query.order_by(InventoryTransaction.date.desc(), InventoryTransaction.id.desc()).all()
            rows = [[
                item.date or "-",
                item.transaction_type or "-",
                item.material_name or "-",
                item.warehouse_name or "-",
                item.destination_warehouse or "-",
                format_grouped_number(item.quantity),
                format_grouped_number(item.unit_cost),
                format_grouped_number(as_float(item.quantity) * as_float(item.unit_cost)),
            ] for item in items]
            return _render_generic_print(
                config["title"],
                ["التاريخ", "النوع", "الصنف", "المصدر", "الوجهة", "الكمية / المبلغ", "التكلفة", "القيمة"],
                rows,
                [{"label": "عدد الحركات", "value": str(len(items))}],
                filters,
            )

        if screen == "journal":
            entries = filter_visible_journals(get_journal_entries_in_range(
                None if comprehensive else (from_date or None),
                None if comprehensive else (to_date or None),
            ))
            account_id = params.get("account_id")
            if account_id:
                entries = [item for item in entries if item.debit_account_id == account_id or item.credit_account_id == account_id]
                account = ChartOfAccount.query.get(account_id)
                if account:
                    filters["entity_label"] = f"{account.code} — {account.name}"
            rows = [[
                item.date or "-",
                item.display_number,
                item.reference or "-",
                item.project.display_name if item.project else (item.cost_center or "-"),
                (item.description or "").split(" - ", 1)[-1],
                f"{item.debit_account.code} — {item.debit_account.name}" if item.debit_account else "-",
                f"{item.credit_account.code} — {item.credit_account.name}" if item.credit_account else "-",
                format_grouped_number(item.amount),
                item.status or "-",
            ] for item in entries]
            return _render_generic_print(
                config["title"],
                ["التاريخ", "رقم القيد", "المرجع", "مركز التكلفة", "البيان", "مدين", "دائن", "المبلغ", "الحالة"],
                rows,
                [{"label": "عدد القيود", "value": str(len(entries))}, {"label": "الإجمالي", "value": format_grouped_number(sum(as_float(item.amount) for item in entries))}],
                filters,
            )

        if screen == "progress":
            query = ProgressPayment.query.filter(ProgressPayment.subcontractor_id.isnot(None))
            query = _apply_date_filter(query, ProgressPayment.date, from_date, to_date, comprehensive)
            subcontractor_id = as_int(request.args.get("subcontractor_id"))
            if not subcontractor_id and party_category == "مقاولي الباطن" and party_name:
                sub = Subcontractor.query.filter_by(name=party_name).first()
                if sub:
                    subcontractor_id = sub.id
            if subcontractor_id and not comprehensive:
                query = query.filter(ProgressPayment.subcontractor_id == subcontractor_id)
                sub = Subcontractor.query.get(subcontractor_id)
                if sub:
                    filters["entity_label"] = sub.name
            items = query.order_by(ProgressPayment.date.desc(), ProgressPayment.id.desc()).all()
            rows = [[
                item.document_number,
                item.date or "-",
                item.project.display_name if item.project else "-",
                item.subcontractor.name if item.subcontractor else "-",
                format_grouped_number(item.total_value),
                format_grouped_number(item.deductions_total),
                format_grouped_number(item.net_value),
            ] for item in items]
            return _render_generic_print(
                config["title"],
                ["رقم", "التاريخ", "المشروع", "المقاول", "الأعمال", "الاستقطاعات", "الصافي"],
                rows,
                [{"label": "عدد المستخلصات", "value": str(len(items))}, {"label": "الصافي", "value": format_grouped_number(sum(as_float(item.net_value) for item in items))}],
                filters,
            )

        if screen == "client_progress":
            query = client_progress_query()
            query = _apply_date_filter(query, ProgressPayment.date, from_date, to_date, comprehensive)
            client_name = (request.args.get("client_name") or "").strip()
            if not client_name and party_category == "العملاء":
                client_name = party_name
            if client_name and not comprehensive:
                query = query.join(Project, ProgressPayment.project_id == Project.id).filter(Project.client_name == client_name)
                filters["entity_label"] = client_name
            items = query.order_by(ProgressPayment.date.desc(), ProgressPayment.id.desc()).all()
            rows = [[
                item.document_number,
                item.date or "-",
                item.project.display_name if item.project else "-",
                item.project.client_name if item.project else "-",
                format_grouped_number(item.total_value),
                format_grouped_number(item.net_value),
            ] for item in items]
            return _render_generic_print(
                config["title"],
                ["رقم", "التاريخ", "المشروع", "العميل", "الأعمال", "الصافي"],
                rows,
                [{"label": "عدد المستخلصات", "value": str(len(items))}],
                filters,
            )

        if screen == "accounts":
            accounts = filter_hidden_treasuries(ChartOfAccount.query.order_by(ChartOfAccount.code, ChartOfAccount.name).all())
            balances = build_account_balances()
            rows = [[
                item.code,
                item.name,
                item.category or "-",
                format_grouped_number(balances.get(item.id, 0)),
            ] for item in accounts]
            return _render_generic_print(
                config["title"],
                ["الكود", "الحساب", "نوع الحساب", "الرصيد"],
                rows,
                [{"label": "عدد الحسابات", "value": str(len(accounts))}],
                filters,
            )

        if screen == "labor":
            query = LaborEntry.query
            query = _apply_date_filter(query, LaborEntry.date, from_date, to_date, comprehensive)
            project_id = as_int(request.args.get("project_id"))
            if project_id and not comprehensive:
                query = query.filter(LaborEntry.project_id == project_id)
                project = Project.query.get(project_id)
                if project:
                    filters["entity_label"] = project.display_name
            items = query.order_by(LaborEntry.date.desc(), LaborEntry.id.desc()).all()
            project_map = {item.id: item for item in Project.query.all()}
            rows = [[
                item.date or "-",
                project_map.get(item.project_id).display_name if project_map.get(item.project_id) else "-",
                item.description,
                format_grouped_number(item.hours),
                format_grouped_number(item.amount),
            ] for item in items]
            return _render_generic_print(
                config["title"],
                ["التاريخ", "المشروع", "البيان", "الساعات", "المبلغ"],
                rows,
                [{"label": "الإجمالي", "value": format_grouped_number(sum(as_float(item.amount) for item in items))}],
                filters,
            )

        if screen == "custody":
            query = CustodySettlement.query
            query = _apply_date_filter(query, CustodySettlement.date, from_date, to_date, comprehensive)
            entity_name = (request.args.get("entity_name") or "").strip()
            if entity_name and not comprehensive:
                query = query.filter(CustodySettlement.entity_name == entity_name)
                filters["entity_label"] = entity_name
            if account_id and not comprehensive:
                query = query.filter(db.or_(
                    CustodySettlement.treasury_account_id == account_id,
                    CustodySettlement.entity_account_id == account_id,
                ))
                if account:
                    filters["entity_label"] = (filters["entity_label"] + " · " if filters["entity_label"] else "") + f"{account.code} — {account.name}"
            items = query.order_by(CustodySettlement.date.desc(), CustodySettlement.id.desc()).all()
            rows = [[
                item.date or "-",
                item.entity_type or "-",
                item.entity_name or "-",
                item.operation_type or "-",
                format_grouped_number(item.amount),
            ] for item in items]
            return _render_generic_print(
                config["title"],
                ["التاريخ", "النوع", "الاسم", "العملية", "المبلغ"],
                rows,
                [{"label": "الإجمالي", "value": format_grouped_number(sum(as_float(item.amount) for item in items))}],
                filters,
            )

        if screen == "receipts":
            query = ClientReceipt.query
            query = _apply_date_filter(query, ClientReceipt.date, from_date, to_date, comprehensive)
            client_name = (request.args.get("client_name") or "").strip()
            if not client_name and party_category == "العملاء":
                client_name = party_name
            if client_name and not comprehensive:
                query = query.filter(ClientReceipt.client_name == client_name)
                filters["entity_label"] = client_name
            if account_id and account and not comprehensive and is_treasury_account(account):
                query = query.filter(ClientReceipt.treasury_account_id == account_id)
            items = query.order_by(ClientReceipt.date.desc(), ClientReceipt.id.desc()).all()
            rows = [[
                item.date or "-",
                item.document_number,
                item.client_name,
                format_grouped_number(item.amount),
                item.payment_method or "-",
            ] for item in items]
            return _render_generic_print(
                config["title"],
                ["التاريخ", "السند", "العميل", "المبلغ", "الطريقة"],
                rows,
                [{"label": "الإجمالي", "value": format_grouped_number(sum(as_float(item.amount) for item in items))}],
                filters,
            )

        if screen == "supplier_payments":
            query = SupplierPayment.query
            query = _apply_date_filter(query, SupplierPayment.date, from_date, to_date, comprehensive)
            supplier_id = as_int(request.args.get("supplier_id"))
            if not supplier_id and party_category == "الموردين" and party_name:
                supplier = Supplier.query.filter_by(name=party_name).first()
                if supplier:
                    supplier_id = supplier.id
            if supplier_id and not comprehensive:
                query = query.filter(SupplierPayment.supplier_id == supplier_id)
                supplier = Supplier.query.get(supplier_id)
                if supplier:
                    filters["entity_label"] = supplier.name
            if account_id and account and not comprehensive and is_treasury_account(account):
                query = query.filter(SupplierPayment.treasury_account_id == account_id)
            items = query.order_by(SupplierPayment.date.desc(), SupplierPayment.id.desc()).all()
            rows = [[
                item.date or "-",
                item.document_number,
                item.supplier.name if item.supplier else "-",
                format_grouped_number(item.amount),
                item.payment_method or "-",
            ] for item in items]
            return _render_generic_print(
                config["title"],
                ["التاريخ", "السند", "المورد", "المبلغ", "الطريقة"],
                rows,
                [{"label": "الإجمالي", "value": format_grouped_number(sum(as_float(item.amount) for item in items))}],
                filters,
            )

        flash("لا توجد طباعة لهذه الشاشة", "danger")
        return redirect(url_for(config["back"]))
