from models import JournalEntry, db

NO_DELETE_MESSAGE = (
    "النظام لا يحذف المستندات المالية. بعد التسجيل تبقى الحركة ثابتة ولا تُمسح من الحساب أو الطرف."
)
POSTED_LOCK_MESSAGE = (
    "القيد المرحّل لا يُحذف ولا يُلغى ترحيله ولا تُغيَّر قيمته أو حساباه. "
    "استخدم قيدًا عكسيًا إذا لزم التصحيح، دون المساس بالحركة الأصلية."
)
POSTED_EDIT_LOCK_MESSAGE = (
    "القيد المرحّل لا يتغير مبلغه ولا حساباه ولا تاريخه ولا يرجع مسودة. "
    "المسموح: البيان والمرجع ورقم القيد ومركز التكلفة. "
    "للتصحيح اعكس القيد ثم سجّل القيد الصحيح."
)
AUTO_JOURNAL_LOCK_MESSAGE = (
    "هذا القيد تلقائي من مستند. تعديله أو حذفه يكون من شاشة المستند نفسه، وقيده يتحدث معه."
)
JOURNAL_SOURCED_LOCK_MESSAGE = (
    "هذه الحركة متسجلة من قيد يومية مرحّل. لتصحيحها اعكس القيد من اليومية ثم سجّل الصحيح."
)
PAYMENT_LOCK_MESSAGE = (
    "الدفعة بعد تسجيلها تثبت على الراجل: لا تعديل للقيمة ولا حذف."
)
ACCOUNT_DELETE_MESSAGE = (
    "لا يمكن حذف الحسابات التي عليها قيود أو مستندات. احذف الحركات المرتبطة أولاً ثم أعد المحاولة."
)
PARTY_DELETE_MESSAGE = (
    "لا يمكن الحذف لوجود مستندات أو قيود مرتبطة. احذف الحركات أولاً ثم أعد المحاولة."
)
OPENING_BALANCE_LOCK_MESSAGE = (
    "الرصيد الافتتاحي لا يُعدَّل بعد وجود حركات على الحساب. غيّر الاسم أو الفئة فقط."
)


def is_posted_journal(entry):
    return (getattr(entry, "status", "") or "") == "مرحل"


def _legacy_reversal_source_id(entry):
    """القيود العكسية القديمة اتربطت بالمرجع REV-000012 قبل عمود reversal_of_id."""
    if not entry or not entry.is_reversal:
        return None
    tail = (entry.reference or "")[4:]
    return int(tail) if tail.isdigit() else None


def journal_reversal_source(entry):
    """القيد الأصلي اللي القيد ده عكسه."""
    if not entry:
        return None
    source_id = entry.reversal_of_id or _legacy_reversal_source_id(entry)
    return JournalEntry.query.get(source_id) if source_id else None


def find_journal_reversal(entry):
    """القيد العكسي لهذا القيد، سواء اتربط بالعمود أو بالمرجع القديم."""
    if not entry or not entry.id:
        return None
    linked = JournalEntry.query.filter_by(reversal_of_id=entry.id).order_by(JournalEntry.id.asc()).first()
    if linked:
        return linked
    legacy = JournalEntry.query.filter(
        JournalEntry.reference == f"REV-{entry.id:06d}",
        JournalEntry.id != entry.id,
    ).order_by(JournalEntry.id.asc()).all()
    return next((item for item in legacy if item.is_reversal), None)


def reversed_journal_ids():
    """أرقام القيود اللي اتعمل لها قيد عكسي (لعرض الحالة في اليومية)."""
    rows = JournalEntry.query.filter(
        db.or_(JournalEntry.reversal_of_id.isnot(None), JournalEntry.reference.like("REV-%"))
    ).all()
    ids = set()
    for row in rows:
        source_id = row.reversal_of_id or _legacy_reversal_source_id(row)
        if source_id:
            ids.add(source_id)
    return ids


def account_has_posted_moves(account_id):
    if not account_id:
        return False
    return JournalEntry.query.filter(
        JournalEntry.status == "مرحل",
        (JournalEntry.debit_account_id == account_id) | (JournalEntry.credit_account_id == account_id),
    ).count() > 0


def account_has_any_journals(account_id):
    if not account_id:
        return False
    return JournalEntry.query.filter(
        (JournalEntry.debit_account_id == account_id) | (JournalEntry.credit_account_id == account_id),
    ).count() > 0


def delete_unused_account(account):
    if not account:
        return True
    if account_has_any_journals(account.id):
        return False
    db.session.delete(account)
    return True


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
