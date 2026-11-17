"""قوائم الشاشات اليومية: عشرون سطرًا في الصفحة، والإجمالي يتحسب على كل النتائج."""

PAGE_SIZE = 20


def listing_page(query, page_arg="page", per_page=PAGE_SIZE):
    from flask import request, url_for

    from services.accounting import as_int

    raw = as_int(request.args.get(page_arg)) or 1
    page = raw if raw > 0 else 1
    ordering = list(getattr(query, "_order_by_clauses", []) or [])
    total = query.order_by(None).count()
    if ordering:
        query = query.order_by(None).order_by(*ordering)
    pages = max((total + per_page - 1) // per_page, 1)
    if page > pages:
        page = pages
    rows = query.offset((page - 1) * per_page).limit(per_page).all() if total else []
    params = request.args.to_dict(flat=True)

    def link(number):
        query_args = dict(params)
        query_args[page_arg] = number
        return url_for(request.endpoint, **query_args)

    pager = {
        "page": page,
        "pages": pages,
        "total": total,
        "start": ((page - 1) * per_page + 1) if total else 0,
        "end": min(page * per_page, total),
        "prev_url": link(page - 1) if page > 1 else "",
        "next_url": link(page + 1) if page < pages else "",
    }
    return rows, pager


def serial_label(column, prefix):
    """رقم العرض التلقائي، مثل JRN-000012، بنفس شكل الشاشة على SQLite وPostgres."""
    from models import db

    if db.engine.dialect.name == "postgresql":
        return db.func.concat(prefix, db.func.lpad(db.cast(column, db.String), 6, "0"))
    return db.func.printf(prefix + "%06d", column)
