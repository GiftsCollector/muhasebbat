from datetime import date

from flask import flash, redirect, render_template, request, url_for

from models import Project, SalesTrip, db
from services.accounting import (
    PeriodClosedError, as_float, as_int, assert_period_open, list_client_names,
    resolve_client_name, sync_journal_related_accounts, sync_sales_trip_journal,
)

ARABIC_WEEKDAYS = ["الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]
SALES_COMPANY_NAME = "شركة السيد الشيخ للمقاولات العمومية"
DEFAULT_TRIP_TYPES = ["سيارة", "قلاب", "خلاطة", "لودر"]


def arabic_weekday(value):
    raw = (value or "")[:10]
    try:
        year, month, day = [int(part) for part in raw.split("-")]
        return ARABIC_WEEKDAYS[date(year, month, day).weekday()]
    except (TypeError, ValueError):
        return ""


def _apply_sales_fields(trip, form):
    trip.date = (form.get("date") or "").strip() or date.today().isoformat()
    trip.voucher_number = (form.get("voucher_number") or "").strip() or None
    trip.trip_type = (form.get("trip_type") or "").strip() or None
    trip.origin = (form.get("origin") or "").strip() or None
    trip.destination = (form.get("destination") or "").strip() or None
    trip.distance_km = as_float(form.get("distance_km"))
    trip.driver_name = (form.get("driver_name") or "").strip()
    trip.tractor_number = (form.get("tractor_number") or "").strip() or None
    trip.cubage = as_float(form.get("cubage"))
    trip.discount = as_float(form.get("discount"))
    trip.unit_price = as_float(form.get("unit_price"))
    trip.advances = as_float(form.get("advances"))
    trip.period_label = (form.get("period_label") or "").strip() or None
    trip.client_name = resolve_client_name(form) or trip.client_name or "عملاء النقل"
    trip.project_id = as_int(form.get("project_id")) or None
    if form.get("notes") is not None:
        trip.notes = form.get("notes")
    trip.recalculate()
    return trip


def _sales_query():
    query = SalesTrip.query
    from_date = (request.args.get("from_date") or "").strip()
    to_date = (request.args.get("to_date") or "").strip()
    driver_name = (request.args.get("driver_name") or "").strip()
    period_label = (request.args.get("period_label") or "").strip()
    voucher = (request.args.get("voucher") or "").strip()
    if from_date:
        query = query.filter(SalesTrip.date >= from_date)
    if to_date:
        query = query.filter(SalesTrip.date <= to_date)
    if driver_name:
        query = query.filter(SalesTrip.driver_name == driver_name)
    if period_label:
        query = query.filter(SalesTrip.period_label == period_label)
    if voucher:
        query = query.filter(SalesTrip.voucher_number.contains(voucher))
    return query.order_by(SalesTrip.date.desc(), SalesTrip.id.desc()), {
        "from_date": from_date,
        "to_date": to_date,
        "driver_name": driver_name,
        "period_label": period_label,
        "voucher": voucher,
    }


def _sheet_totals(trips):
    totals = {
        "cubage": 0.0,
        "discount": 0.0,
        "net_quantity": 0.0,
        "total_amount": 0.0,
        "advances": 0.0,
        "remaining": 0.0,
        "distance_km": 0.0,
    }
    for trip in trips:
        totals["cubage"] += as_float(trip.cubage)
        totals["discount"] += as_float(trip.discount)
        totals["net_quantity"] += as_float(trip.net_quantity)
        totals["total_amount"] += as_float(trip.total_amount)
        totals["advances"] += as_float(trip.advances)
        totals["remaining"] += as_float(trip.remaining)
        totals["distance_km"] += as_float(trip.distance_km)
    return {key: round(value, 2) for key, value in totals.items()}


def _choice_values(attr):
    values = {
        (value[0] or "").strip()
        for value in SalesTrip.query.with_entities(getattr(SalesTrip, attr)).all()
        if (value[0] or "").strip()
    }
    return sorted(values)


def register(app):
    @app.route("/sales", methods=["GET", "POST"])
    def sales_trips():
        sync_journal_related_accounts()
        if request.method == "POST":
            driver_name = (request.form.get("driver_name") or "").strip()
            if not driver_name:
                flash("يرجى إدخال اسم السائق", "danger")
                return redirect(url_for("sales_trips", **request.args))
            trip = SalesTrip(driver_name=driver_name)
            _apply_sales_fields(trip, request.form)
            try:
                assert_period_open(trip.date)
            except PeriodClosedError as exc:
                flash(str(exc), "danger")
                return redirect(url_for("sales_trips", **request.args))
            db.session.add(trip)
            db.session.flush()
            try:
                sync_sales_trip_journal(trip)
            except PeriodClosedError as exc:
                db.session.rollback()
                flash(str(exc), "danger")
                return redirect(url_for("sales_trips", **request.args))
            flash(f"تم حفظ النقلة {trip.document_number} وظهرت في حساب السائق {trip.driver_name}", "success")
            return redirect(url_for("sales_trips", **request.args))

        query, filters = _sales_query()
        trips = query.all()
        driver_names = _choice_values("driver_name")
        period_labels = _choice_values("period_label")
        type_choices = sorted(set(DEFAULT_TRIP_TYPES).union(_choice_values("trip_type")))
        return render_template(
            "sales.html",
            trips=trips,
            totals=_sheet_totals(trips),
            filters=filters,
            today_date=date.today().isoformat(),
            weekday_name=arabic_weekday(date.today().isoformat()),
            projects=Project.query.order_by(Project.code).all(),
            client_names=list_client_names(),
            driver_names=driver_names,
            period_labels=period_labels,
            type_choices=type_choices,
            origin_choices=_choice_values("origin"),
            destination_choices=_choice_values("destination"),
            tractor_choices=_choice_values("tractor_number"),
            company_name=SALES_COMPANY_NAME,
            weekday_of=arabic_weekday,
        )

    @app.route("/sales/<int:trip_id>/update", methods=["POST"])
    def update_sales_trip(trip_id):
        trip = SalesTrip.query.get_or_404(trip_id)
        driver_name = (request.form.get("driver_name") or "").strip()
        if not driver_name:
            flash("يرجى إدخال اسم السائق", "danger")
            return redirect(url_for("sales_trips"))
        old_date = trip.date
        _apply_sales_fields(trip, request.form)
        try:
            assert_period_open(old_date)
            assert_period_open(trip.date)
            db.session.flush()
            sync_sales_trip_journal(trip)
        except PeriodClosedError as exc:
            db.session.rollback()
            flash(str(exc), "danger")
            return redirect(url_for("sales_trips"))
        flash(f"تم تعديل النقلة {trip.document_number}", "success")
        return redirect(url_for("sales_trips"))

    @app.route("/sales/print")
    def print_sales_trips():
        query, filters = _sales_query()
        trips = query.all()
        return render_template(
            "print_sales.html",
            trips=trips,
            totals=_sheet_totals(trips),
            filters=filters,
            company_name=SALES_COMPANY_NAME,
            weekday_of=arabic_weekday,
        )
