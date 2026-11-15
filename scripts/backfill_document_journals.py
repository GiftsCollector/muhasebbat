"""تسجيل قيود المستندات القديمة (مرة واحدة) عشان تظهر في الخزنة وحساب الطرف.

افتراضيًا بيعمل معاينة بس ومش بيكتب أي حاجة في قاعدة البيانات.
التنفيذ الفعلي محتاج:  --apply --i-have-a-backup   (بعد نسخة احتياطية كاملة).

بيشتغل على قاعدة DATABASE_URL الحالية. ما بيطبعش رابط القاعدة ولا أي بيانات سرية.

القواعد:
- بيعدّي المستند اللي ليه قيد بالفعل (ما يتكررش).
- بيعدّي المستند المتولد من قيد يومية (JRN-SRC)، لأن قيده موجود أصلًا.
- بيعدّي سداد/تحصيل/صرف من غير خزنة محددة (مش هنخمّن الخزنة)، ويطبعها للمراجعة.
- بيعدّي أي مستند تاريخه في فترة مقفولة، ويطبعه للمراجعة.
- تشغيله مرتين آمن: التاني مش بيضيف حاجة.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app  # noqa: E402
from models import (  # noqa: E402
    ClientReceipt, CustodySettlement, EmployeeSalaryPayment, JournalEntry, LaborEntry,
    SalesTrip, SubcontractorPayment, SupplierPayment, db,
)
from services import accounting as acc  # noqa: E402

# (اسم العرض، الموديل، علامة القيد، دالة التسجيل، محتاج خزنة؟)
KINDS = [
    ("إيصالات العملاء", ClientReceipt, "REC-AUTO:", acc.sync_client_receipt_journal, True),
    ("سداد الموردين", SupplierPayment, "PAY-AUTO:", acc.sync_supplier_payment_journal, True),
    ("دفعات المقاولين", SubcontractorPayment, "SPAY-AUTO:", acc.sync_subcontractor_payment_journal, True),
    ("مرتبات الموظفين", EmployeeSalaryPayment, "ESAL-AUTO:", acc.sync_employee_salary_payment_journal, True),
    ("العهد", CustodySettlement, "CUST-AUTO:", acc.sync_custody_settlement_journal, True),
    ("نقلات المبيعات", SalesTrip, "SALE-AUTO:", acc.sync_sales_trip_journal, False),
    ("العمالة", LaborEntry, "LAB-AUTO:", acc.sync_labor_journal, False),
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="نفّذ فعلًا (من غيره معاينة بس)")
    parser.add_argument("--i-have-a-backup", action="store_true", help="تأكيد إن فيه نسخة احتياطية كاملة")
    args = parser.parse_args()
    apply = args.apply and args.i_have_a_backup
    if args.apply and not args.i_have_a_backup:
        print("لازم --i-have-a-backup مع --apply. ما اتكتبش حاجة.")
        return 2

    with app.app_context():
        print("القاعدة:", db.engine.dialect.name, "| الوضع:", "تنفيذ فعلي" if apply else "معاينة بس (مفيش كتابة)")
        journals_before = JournalEntry.query.count()
        grand = {"done": 0, "has": 0, "sourced": 0, "no_treasury": [], "closed": [], "failed": []}
        for label, model, marker, sync, needs_treasury in KINDS:
            counts = {"total": 0, "has": 0, "sourced": 0, "no_treasury": 0, "closed": 0, "todo": 0, "done": 0}
            for doc in model.query.order_by(model.id.asc()).all():
                counts["total"] += 1
                if acc.is_journal_sourced_document(getattr(doc, "notes", None)):
                    counts["sourced"] += 1
                    continue
                existing = acc._journals_for_marker(f"{marker}{doc.id}")
                if existing:
                    counts["has"] += 1
                    continue
                if needs_treasury and not getattr(doc, "treasury_account_id", None):
                    counts["no_treasury"] += 1
                    grand["no_treasury"].append(f"{label} #{doc.id}")
                    continue
                if acc.find_closed_period(getattr(doc, "date", None)):
                    counts["closed"] += 1
                    grand["closed"].append(f"{label} #{doc.id} ({doc.date})")
                    continue
                counts["todo"] += 1
                if not apply:
                    continue
                try:
                    before = JournalEntry.query.count()
                    sync(doc)
                    db.session.commit()
                    if JournalEntry.query.count() > before:
                        counts["done"] += 1
                except Exception as exc:  # مستند واحد فاشل ما يوقفش الباقي
                    db.session.rollback()
                    grand["failed"].append(f"{label} #{doc.id}: {type(exc).__name__}")
            print(
                f"{label}: الكل {counts['total']} | عنده قيد {counts['has']} | من قيد يومية {counts['sourced']} | "
                f"من غير خزنة {counts['no_treasury']} | فترة مقفولة {counts['closed']} | "
                f"محتاج قيد {counts['todo']}" + (f" | اتسجل {counts['done']}" if apply else "")
            )
            grand["done"] += counts["done"]
        print("قيود قبل:", journals_before, "| بعد:", JournalEntry.query.count())
        for key, title in (("no_treasury", "اتعدّت لأنها من غير خزنة (راجعها يدويًا)"),
                           ("closed", "اتعدّت لأنها في فترة مقفولة"),
                           ("failed", "فشلت")):
            if grand[key]:
                print(f"\n{title}:")
                for item in grand[key][:200]:
                    print(" -", item)
        if not apply:
            print("\nده معاينة بس. للتنفيذ بعد النسخة الاحتياطية: --apply --i-have-a-backup")
    return 0


if __name__ == "__main__":
    sys.exit(main())
