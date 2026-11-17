"""تسجيل قيود المستندات القديمة اللي ما اتسجلتش في الخزنة (مرة واحدة)، بأمان.

الافتراضي: يسجل سداد الموردين ودفعات المقاولين بس (--kinds لتغيير ده).
الافتراضي كمان معاينة: ما بيكتبش حاجة.

التنفيذ الفعلي: --apply --i-have-a-backup
- كل التنفيذ في معاملة واحدة، وبعده بيتأكد إن:
    * كشف حساب أي مورد أو مقاول ما اتغيرش (نفس الأرصدة ونفس عدد الحركات)،
    * مفيش حساب جديد اتعمل،
    * تغيّر الخزن بالظبط زي المتوقع.
  لو أي حاجة منهم اختلفت: بيعمل rollback ومفيش حاجة بتتكتب.
- بيعدّي أي مستند: ليه قيد بالفعل، أو متولد من قيد يومية، أو من غير خزنة، أو في فترة مقفولة،
  أو فيه قيد يدوي مشابه (نفس المبلغ ونفس أحد الحسابين خلال 45 يوم) وبيطبعه للمراجعة.
- تشغيله مرتين آمن.
- ما بيطبعش رابط القاعدة ولا أي أسرار.
"""
import argparse
import os
import sys
from collections import defaultdict
from datetime import date as _date
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app  # noqa: E402
from models import (  # noqa: E402
    ChartOfAccount, ClientReceipt, CustodySettlement, EmployeeSalaryPayment, JournalEntry, LaborEntry,
    SalesTrip, Subcontractor, SubcontractorPayment, Supplier, SupplierPayment, db,
)
from services import accounting as acc  # noqa: E402

D = Decimal

KINDS = {
    "supplier": ("سداد الموردين", SupplierPayment, ("PAY-AUTO:",), acc.sync_supplier_payment_journal, True),
    "subcontractor": ("دفعات المقاولين", SubcontractorPayment, ("SPAY-AUTO:",), acc.sync_subcontractor_payment_journal, True),
    "receipt": ("إيصالات العملاء", ClientReceipt, ("REC-AUTO:",), acc.sync_client_receipt_journal, True),
    "salary": ("مرتبات الموظفين (صرف)", EmployeeSalaryPayment, ("ESAL-AUTO:",), acc.sync_employee_salary_payment_journal, True),
    "custody": ("العهد", CustodySettlement, ("CUST-AUTO:",), acc.sync_custody_settlement_journal, True),
    "sales": ("نقلات المبيعات", SalesTrip, ("SALE-AUTO:", "SALE-ADV-AUTO:"), acc.sync_sales_trip_journal, False),
    "labor": ("العمالة", LaborEntry, ("LAB-AUTO:",), acc.sync_labor_journal, False),
}


def fmt(value):
    return f"{D(value):,.2f}"


def party_state():
    """لقطة لكشوف الموردين والمقاولين: لازم تفضل زي ما هي بعد التنفيذ."""
    state = {}
    for supplier in Supplier.query.all():
        account = acc.find_account_by_person("الموردين", supplier.name)
        parts = acc.supplier_balance_parts(supplier, account)
        state[("مورد", supplier.id, supplier.name)] = (
            round(parts["purchases"], 2), round(parts["payments"], 2), round(parts["due"], 2), len(parts["lines"]),
        )
    for sub in Subcontractor.query.all():
        statement = acc.build_subcontractor_statement(sub, create_account=False)
        state[("مقاول", sub.id, sub.name)] = (
            round(statement["total_works"], 2), round(statement["total_paid"], 2),
            round(statement["net_due"], 2), len(statement["movements"]),
        )
    return state


def treasury_state():
    accounts = [a for a in ChartOfAccount.query.all() if acc.is_treasury_account(a)]
    balances = acc.build_account_balances(accounts)
    return {a.id: (a, D(str(balances.get(a.id, 0)))) for a in accounts}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--kinds", default="supplier,subcontractor", help=",".join(KINDS))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--i-have-a-backup", action="store_true")
    args = parser.parse_args()
    if args.apply and not args.i_have_a_backup:
        print("لازم --i-have-a-backup مع --apply. ما اتكتبش حاجة.")
        return 2
    apply = args.apply and args.i_have_a_backup
    chosen = [k.strip() for k in args.kinds.split(",") if k.strip()]
    for key in chosen:
        if key not in KINDS:
            print("نوع غير معروف:", key)
            return 2

    captured = []
    ctx = {}

    def capture(marker, payloads, *a, **k):
        for payload in payloads or []:
            captured.append((ctx["label"], ctx["doc"], dict(payload)))

    original_commit = db.session.commit
    original_funcs = (acc.replace_auto_journals, acc.replace_document_journals)

    with app.app_context():
        print("القاعدة:", db.engine.dialect.name, "| الوضع:", "تنفيذ فعلي" if apply else "معاينة بس (مفيش كتابة)")
        journals_before = JournalEntry.query.count()
        accounts_before = ChartOfAccount.query.count()

        manual = defaultdict(list)
        for entry in JournalEntry.query.filter(JournalEntry.status == "مرحل").all():
            if acc.is_auto_journal_entry(entry) or entry.is_reversal:
                continue
            manual[round(float(entry.amount or 0), 2)].append(entry)

        candidates = []  # (key, label, sync, doc_id)
        skipped = defaultdict(list)
        stats = {}
        try:
            # مرحلة 1: التقاط القيود المتوقعة (من غير كتابة)
            acc.replace_auto_journals = capture
            acc.replace_document_journals = capture
            db.session.commit = db.session.flush
            for key in chosen:
                label, model, markers, sync, needs_treasury = KINDS[key]
                stats[key] = {"total": 0, "has": 0, "sourced": 0, "todo": 0}
                for doc_id in [r[0] for r in db.session.query(model.id).order_by(model.id)]:
                    stats[key]["total"] += 1
                    doc = model.query.get(doc_id)
                    if acc.is_journal_sourced_document(getattr(doc, "notes", None)):
                        stats[key]["sourced"] += 1
                        continue
                    if any(acc._journals_for_marker(f"{m}{doc_id}") for m in markers):
                        stats[key]["has"] += 1
                        continue
                    if needs_treasury and not getattr(doc, "treasury_account_id", None):
                        skipped["من غير خزنة"].append(f"{label} #{doc_id}")
                        continue
                    ctx["label"], ctx["doc"] = label, doc_id
                    before = len(captured)
                    try:
                        sync(doc)
                    except Exception as exc:  # noqa: BLE001
                        skipped["فشل الحساب"].append(f"{label} #{doc_id} ({type(exc).__name__})")
                        del captured[before:]
                        continue
                    finally:
                        db.session.rollback()
                    mine = captured[before:]
                    if not mine:
                        continue
                    if any(acc.find_closed_period(p.get("date")) for _, _, p in mine):
                        skipped["فترة مقفولة"].append(f"{label} #{doc_id}")
                        del captured[before:]
                        continue
                    suspect = None
                    for _, _, p in mine:
                        amount = round(float(p.get("amount") or 0), 2)
                        for entry in manual.get(amount, []):
                            if {entry.debit_account_id, entry.credit_account_id} & {p["debit_account_id"], p["credit_account_id"]}:
                                try:
                                    gap = abs((_date.fromisoformat(entry.date) - _date.fromisoformat(p["date"])).days)
                                except Exception:  # noqa: BLE001
                                    gap = 0
                                if gap <= 45:
                                    suspect = f"{entry.display_number} ({entry.date})"
                                    break
                        if suspect:
                            break
                    if suspect:
                        skipped["قيد يدوي مشابه (راجعه)"].append(f"{label} #{doc_id} ~ {suspect}")
                        del captured[before:]
                        continue
                    stats[key]["todo"] += 1
                    candidates.append((key, label, sync, doc_id))
        finally:
            acc.replace_auto_journals, acc.replace_document_journals = original_funcs
            db.session.commit = original_commit
            db.session.rollback()

        expected = defaultdict(lambda: D("0"))
        for _, _, p in captured:
            amount = D(str(p["amount"]))
            expected[p["debit_account_id"]] += amount
            expected[p["credit_account_id"]] -= amount
        accounts = {a.id: a for a in ChartOfAccount.query.all()}
        treasuries_now = treasury_state()

        print("\n=== الخطة ===")
        for key in chosen:
            label = KINDS[key][0]
            s = stats[key]
            amount = sum((D(str(p["amount"])) for lbl, _, p in captured if lbl == label), D("0"))
            print(f"{label}: الكل {s['total']} | من قيد يومية {s['sourced']} | عنده قيد {s['has']} | هيتسجل {s['todo']} بإجمالي {fmt(amount)}")
        print("\nتغيير الخزن المتوقع:")
        for tid, (account, balance) in sorted(treasuries_now.items(), key=lambda kv: kv[1][0].code or ""):
            delta = expected.get(tid, D("0"))
            if delta:
                print(f"- {account.code} | {account.name}: {fmt(balance)} → {fmt(balance + delta)} ({fmt(delta)})")
        if not any(expected.get(t) for t in treasuries_now):
            print("- لا يوجد")
        if skipped:
            print("\nاتعدّت (راجعها):")
            for title, items in skipped.items():
                print(f"* {title}:")
                for item in items[:100]:
                    print("   -", item)

        if not apply:
            print("\nمعاينة بس. للتنفيذ: --apply --i-have-a-backup")
            print("قيود:", JournalEntry.query.count(), "(لازم تساوي", journals_before, ")")
            return 0
        if not candidates:
            print("\nمفيش حاجة تتسجل.")
            return 0

        # مرحلة 2: التنفيذ في معاينة واحدة + تحقق قبل الحفظ النهائي
        print("\nجاري التنفيذ والتحقق (لسه ما اتحفظش)...")
        before_party = party_state()
        before_treasury = {tid: bal for tid, (_, bal) in treasuries_now.items()}
        db.session.commit = db.session.flush
        ok = True
        try:
            created = 0
            for key, label, sync, doc_id in candidates:
                model = KINDS[key][1]
                n = JournalEntry.query.count()
                sync(model.query.get(doc_id))
                db.session.flush()
                if JournalEntry.query.count() > n:
                    created += 1
            after_party = party_state()
            changed = [(k, before_party[k], after_party.get(k)) for k in before_party if before_party[k] != after_party.get(k)]
            if changed:
                ok = False
                print("!! كشف حساب اتغير، هيتلغى التنفيذ:")
                for k, b, a in changed[:30]:
                    print("   ", k[0], k[2], b, "→", a)
            if ChartOfAccount.query.count() != accounts_before:
                ok = False
                print("!! اتعمل حساب جديد، هيتلغى التنفيذ.")
            after_balances = acc.build_account_balances([v[0] for v in treasuries_now.values()])
            for tid, (account, _) in treasuries_now.items():
                got = D(str(after_balances.get(tid, 0))) - before_treasury[tid]
                want = expected.get(tid, D("0"))
                if abs(got - want) > D("0.01"):
                    ok = False
                    print(f"!! {account.code}: التغيير الفعلي {fmt(got)} غير المتوقع {fmt(want)}")
            if ok:
                db.session.commit = original_commit
                db.session.commit()
                print(f"اتحفظ. قيود اتسجلت: {created}")
            else:
                db.session.rollback()
                print("اتلغى: مفيش حاجة اتكتبت.")
        except Exception as exc:  # noqa: BLE001
            ok = False
            db.session.rollback()
            print("اتلغى بسبب خطأ:", type(exc).__name__, str(exc)[:200])
        finally:
            db.session.commit = original_commit

        print("\nقيود قبل:", journals_before, "| بعد:", JournalEntry.query.count())
        for tid, (account, balance) in sorted(treasury_state().items(), key=lambda kv: kv[1][0].code or ""):
            if tid in expected and expected[tid]:
                print(f"- {account.code} | {account.name}: الرصيد الآن {fmt(balance)}")
        return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
