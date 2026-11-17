"""مراجعة (قراءة بس) لكل المستندات: مين ليه قيد ومين لأ، وإيه اللي هيتغير في الخزن.

ما بيكتبش أي حاجة في قاعدة البيانات:
- بيحسب القيود المتوقعة بتشغيل دوال التسجيل نفسها في وضع التقاط (من غير كتابة).
- أي commit بيتحوّل لـ flush، وبيعمل rollback بعد كل مستند وفي الآخر.
- في الآخر بيتأكد إن عدد القيود ما اتغيرش.

الاستخدام:  python scripts/audit_document_journals.py
المخرجات أرقام ومراجع بس، من غير رابط قاعدة ولا أي أسرار.
"""
import os
import re
import sys
from collections import defaultdict
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app  # noqa: E402
from models import (  # noqa: E402
    ChartOfAccount, ClientReceipt, CustodySettlement, DriverCompensationEntry, Equipment,
    EquipmentMaintenance, EmployeeSalaryPayment, InventoryTransaction, JournalEntry, LaborEntry,
    PayrollSlip, ProgressPayment, PurchaseOrder, SalesTrip, SubcontractorPayment,
    SubcontractorRetentionRelease, SupplierPayment, db,
)
from services import accounting as acc  # noqa: E402

D = Decimal

# (الاسم، الموديل، علامات القيد، دالة التسجيل أو None لو مفيش منطق قيد، محتاج خزنة؟)
KINDS = [
    ("إيصالات العملاء", ClientReceipt, ("REC-AUTO:",), acc.sync_client_receipt_journal, True),
    ("سداد الموردين", SupplierPayment, ("PAY-AUTO:",), acc.sync_supplier_payment_journal, True),
    ("دفعات المقاولين", SubcontractorPayment, ("SPAY-AUTO:",), acc.sync_subcontractor_payment_journal, True),
    ("مرتبات الموظفين (صرف)", EmployeeSalaryPayment, ("ESAL-AUTO:",), acc.sync_employee_salary_payment_journal, True),
    ("العهد", CustodySettlement, ("CUST-AUTO:",), acc.sync_custody_settlement_journal, True),
    ("نقلات المبيعات", SalesTrip, ("SALE-AUTO:", "SALE-ADV-AUTO:"), acc.sync_sales_trip_journal, False),
    ("العمالة", LaborEntry, ("LAB-AUTO:",), acc.sync_labor_journal, False),
    ("صيانة المعدات", EquipmentMaintenance, ("EQP-MNT-AUTO:",), acc.sync_equipment_maintenance_journal, False),
    ("المعدات (شراء/تشغيل)", Equipment, ("EQP-AUTO:", "EQP-OP-AUTO:"), acc.sync_equipment_journals, False),
    ("كشوف المرتبات", PayrollSlip, ("PAYROLL-AUTO:",), acc.sync_payroll_slip_journal, False),
    ("حركة المخزون", InventoryTransaction, ("INV-AUTO:",), acc.sync_inventory_transaction_journal, False),
    ("مستخلصات", ProgressPayment, ("PP-AUTO:",), acc.sync_progress_payment_journals, False),
    ("إفراج الضمان", SubcontractorRetentionRelease, ("RETREL-AUTO:",), acc.sync_retention_release_journal, False),
    ("شيت السائق", DriverCompensationEntry, ("DRV-WORK-AUTO:", "DRV-DED-AUTO:", "DRV-PAY-AUTO:"), None, False),
    ("أوامر الشراء", PurchaseOrder, ("PO-JRN-AUTO:",), None, False),
]


def fmt(value):
    return f"{D(value):,.2f}"


def has_marker_journal(markers, doc_id):
    return any(acc._journals_for_marker(f"{m}{doc_id}") for m in markers)


def main():
    captured = []  # (kind, doc_id, payload)
    ctx = {"kind": None, "doc": None}

    def capture(marker, payloads, *args, **kwargs):
        for payload in payloads or []:
            captured.append((ctx["kind"], ctx["doc"], dict(payload)))

    original = (acc.replace_auto_journals, acc.replace_document_journals, db.session.commit)
    with app.app_context():
        journals_before = JournalEntry.query.count()
        print("القاعدة:", db.engine.dialect.name, "| قيود حالية:", journals_before, "| (مراجعة بس، مفيش كتابة)\n")

        summary = []
        skipped_no_treasury = defaultdict(list)
        skipped_closed = defaultdict(list)
        no_logic = {}
        try:
            acc.replace_auto_journals = capture
            acc.replace_document_journals = capture
            db.session.commit = db.session.flush
            for label, model, markers, sync, needs_treasury in KINDS:
                rows = {"total": 0, "has": 0, "sourced": 0, "new_docs": 0, "no_treasury": 0, "closed": 0}
                for doc_id in [row[0] for row in db.session.query(model.id).order_by(model.id).all()]:
                    rows["total"] += 1
                    doc = model.query.get(doc_id)
                    if acc.is_journal_sourced_document(getattr(doc, "notes", None)):
                        rows["sourced"] += 1
                        continue
                    if has_marker_journal(markers, doc_id):
                        rows["has"] += 1
                        continue
                    if sync is None:
                        continue
                    if needs_treasury and not getattr(doc, "treasury_account_id", None):
                        rows["no_treasury"] += 1
                        skipped_no_treasury[label].append(doc_id)
                        continue
                    ctx["kind"], ctx["doc"] = label, doc_id
                    before = len(captured)
                    try:
                        sync(doc)
                    except Exception as exc:  # noqa: BLE001
                        print(f"  تحذير: {label} #{doc_id} ما اتحسبش ({type(exc).__name__})")
                    finally:
                        db.session.rollback()
                    new_payloads = captured[before:]
                    if not new_payloads:
                        continue
                    if any(acc.find_closed_period(p.get("date")) for _, _, p in new_payloads):
                        rows["closed"] += 1
                        skipped_closed[label].append(doc_id)
                        del captured[before:]
                        continue
                    rows["new_docs"] += 1
                if sync is None:
                    no_logic[label] = rows
                summary.append((label, rows, sync is None))
        finally:
            acc.replace_auto_journals, acc.replace_document_journals = original[0], original[1]
            db.session.commit = original[2]
            db.session.rollback()

        # قيود يدوية قابلة للتكرار: نفس الحسابين ونفس المبلغ
        manual = defaultdict(list)
        for entry in JournalEntry.query.filter(JournalEntry.status == "مرحل").all():
            if acc.is_auto_journal_entry(entry) or entry.reversal_of_id or entry.is_reversal:
                continue
            manual[(entry.debit_account_id, entry.credit_account_id, round(float(entry.amount or 0), 2))].append(entry)

        dup_rows = []
        deltas = defaultdict(lambda: defaultdict(lambda: D("0")))  # account -> kind -> delta
        deltas_nodup = defaultdict(lambda: D("0"))
        counts_by_kind = defaultdict(lambda: {"journals": 0, "amount": D("0"), "dups": 0})
        for kind, doc_id, p in captured:
            amount = D(str(p.get("amount") or 0))
            if amount <= 0 or not p.get("debit_account_id") or not p.get("credit_account_id"):
                continue
            key = (p["debit_account_id"], p["credit_account_id"], round(float(amount), 2))
            matches = manual.get(key, [])
            counts_by_kind[kind]["journals"] += 1
            counts_by_kind[kind]["amount"] += amount
            deltas[p["debit_account_id"]][kind] += amount
            deltas[p["credit_account_id"]][kind] -= amount
            if matches:
                counts_by_kind[kind]["dups"] += 1
                dup_rows.append((kind, doc_id, p.get("date"), float(amount), [(m.display_number, m.date) for m in matches[:3]]))
            else:
                deltas_nodup[p["debit_account_id"]] += amount
                deltas_nodup[p["credit_account_id"]] -= amount

        print("=== (1) المستندات ===")
        for label, rows, nologic in summary:
            line = (f"{label}: الكل {rows['total']} | عنده قيد {rows['has']} | من قيد يومية {rows['sourced']}"
                    f" | هيتسجل له قيد {rows['new_docs']} | من غير خزنة {rows['no_treasury']} | فترة مقفولة {rows['closed']}")
            if nologic:
                line += " | (مفيش منطق قيد حاليًا، محتاج تصميم)"
            c = counts_by_kind.get(label)
            if c:
                line += f" | قيود جديدة {c['journals']} بإجمالي {fmt(c['amount'])} | منها محتمل تكرار {c['dups']}"
            print(line)

        accounts = {a.id: a for a in ChartOfAccount.query.all()}
        treasury_ids = [a.id for a in accounts.values() if acc.is_treasury_account(a)]
        balances = acc.build_account_balances([accounts[i] for i in treasury_ids])
        print("\n=== (2) الخزن والبنوك: الرصيد الحالي والمتوقع ===")
        print("(الموجب = مدين/رصيد، السالب = دائن. 'لو استبعدنا المحتمل تكرارها' = لو القيود المتشابهة مع يدوية اتعدّت)")
        for tid in sorted(treasury_ids, key=lambda i: accounts[i].code or ""):
            delta = sum(deltas[tid].values(), D("0")) if tid in deltas else D("0")
            current = D(str(balances.get(tid, 0)))
            nodup = deltas_nodup.get(tid, D("0"))
            print(f"- {accounts[tid].code} | {accounts[tid].name}: حالي {fmt(current)} | تغيير متوقع {fmt(delta)}"
                  f" | بعد التسجيل {fmt(current + delta)} | لو استبعدنا المحتمل تكرارها {fmt(current + nodup)}")
            for kind, value in sorted(deltas[tid].items(), key=lambda kv: -abs(kv[1])):
                if value:
                    print(f"      {kind}: {fmt(value)}")

        print("\n=== (3) قيود يدوية ممكن تكون نفس المستند (راجعها) ===")
        if not dup_rows:
            print("لا يوجد")
        for kind, doc_id, date, amount, matches in dup_rows[:300]:
            shown = "، ".join(f"{num} ({d})" for num, d in matches)
            print(f"- {kind} #{doc_id} بتاريخ {date} مبلغ {fmt(amount)} ~ قيد يدوي: {shown}")

        print("\n=== (4) مستندات اتعدّت ===")
        for title, bucket in (("من غير خزنة", skipped_no_treasury), ("فترة مقفولة", skipped_closed)):
            for label, ids in bucket.items():
                print(f"- {title} | {label}: {len(ids)} مستند، أول 20: {ids[:20]}")
        if not skipped_no_treasury and not skipped_closed:
            print("لا يوجد")

        journals_after = JournalEntry.query.count()
        print("\nقيود بعد المراجعة:", journals_after, "(لازم تساوي", journals_before, ")",
              "OK" if journals_after == journals_before else "!! اتغير، بلّغني فورًا")


if __name__ == "__main__":
    main()
