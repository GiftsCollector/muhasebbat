"""ينسخ بيانات المحاسبة من قاعدة الإنتاج لملف SQLite محلي (قراءة بس من المصدر).

- ما بيحمّلش البرنامج (app) فمفيش أي كتابة تجهيزية على قاعدة الإنتاج.
- بيقرا كل جدول بسطر استعلام واحد، فهو أسرع بكتير من المراجعة على قاعدة بعيدة.
- ما بينسخش: المستخدمين والمرفقات وسجل النشاط والإعدادات (فيها أسرار أو مش لازمة للمراجعة).

الاستخدام:  DATABASE_URL=<المصدر> python scripts/snapshot_to_sqlite.py <ملف_الوجهة.db>
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine  # noqa: E402

SKIP = {"user", "activity_log", "document_attachment", "app_setting", "alert_dismissal",
        "submission_receipt", "stored_party_balance"}


def main():
    if len(sys.argv) != 2:
        print("الاستخدام: python scripts/snapshot_to_sqlite.py <ملف_الوجهة.db>")
        return 2
    source_url = (os.getenv("DATABASE_URL") or "").strip()
    if not source_url:
        print("DATABASE_URL مش متحدد.")
        return 2
    if source_url.startswith("postgres://"):
        source_url = source_url.replace("postgres://", "postgresql://", 1)
    target_path = os.path.abspath(sys.argv[1])
    if os.path.exists(target_path):
        os.remove(target_path)

    os.environ["DATABASE_URL"] = "sqlite://"  # models بس، من غير ما نلمس الإنتاج
    os.environ.setdefault("SECRET_KEY", "snapshot")
    from models import db  # noqa: E402

    source = create_engine(source_url)
    target = create_engine("sqlite:///" + target_path.replace("\\", "/"))
    db.metadata.create_all(target)
    print("المصدر:", source.dialect.name, "| الوجهة: ملف محلي")

    total = 0
    with source.connect() as src, target.begin() as dst:
        for table in db.metadata.sorted_tables:
            if table.name in SKIP:
                continue
            try:
                result = src.execute(table.select())
                batch = []
                count = 0
                for row in result:
                    batch.append(dict(row._mapping))
                    if len(batch) >= 1000:
                        dst.execute(table.insert(), batch)
                        count += len(batch)
                        batch = []
                if batch:
                    dst.execute(table.insert(), batch)
                    count += len(batch)
                total += count
                print(f"  {table.name}: {count}")
            except Exception as exc:  # noqa: BLE001
                print(f"  تحذير: {table.name} ما اتنسخش ({type(exc).__name__}: {str(exc)[:120]})")
    print("اتنسخ", total, "صف →", target_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
