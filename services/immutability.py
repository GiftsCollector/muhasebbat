from models import JournalEntry, db

NO_DELETE_MESSAGE = (
    "النظام لا يحذف المستندات المالية. بعد التسجيل تبقى الحركة ثابتة ولا تُمسح من الحساب أو الطرف."
)
POSTED_LOCK_MESSAGE = (
    "القيد المرحّل لا يُحذف ولا يُلغى ترحيله ولا تُغيَّر قيمته أو حساباه. "
    "استخدم قيدًا عكسيًا إذا لزم التصحيح، دون المساس بالحركة الأصلية."
)
PAYMENT_LOCK_MESSAGE = (
    "الدفعة بعد تسجيلها تثبت على الراجل: لا تعديل للقيمة ولا حذف."
)
ACCOUNT_DELETE_MESSAGE = (
    "لا يمكن حذف الحسابات. عدّل اسم الحساب أو فئته إذا لزم، والمعاملات السابقة تبقى كما سُجّلت."
)
OPENING_BALANCE_LOCK_MESSAGE = (
    "الرصيد الافتتاحي لا يُعدَّل بعد وجود حركات على الحساب. غيّر الاسم أو الفئة فقط."
)


def is_posted_journal(entry):
    return (getattr(entry, "status", "") or "") == "مرحل"


def account_has_posted_moves(account_id):
    if not account_id:
        return False
    return JournalEntry.query.filter(
        JournalEntry.status == "مرحل",
        (JournalEntry.debit_account_id == account_id) | (JournalEntry.credit_account_id == account_id),
    ).count() > 0


def account_ids_with_posted_moves():
    debit_ids = {
        row[0]
        for row in db.session.query(JournalEntry.debit_account_id).filter(JournalEntry.status == "مرحل").all()
        if row[0]
    }
    credit_ids = {
        row[0]
        for row in db.session.query(JournalEntry.credit_account_id).filter(JournalEntry.status == "مرحل").all()
        if row[0]
    }
    return debit_ids | credit_ids
