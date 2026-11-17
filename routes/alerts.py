from flask import flash, g, redirect, render_template, request, url_for

from services.alerts import dismiss_alert, visible_alerts_for


def register(app):
    @app.route("/alerts")
    def alerts_inbox():
        return render_template(
            "alerts.html",
            alerts=visible_alerts_for(g.current_user),
        )

    @app.route("/alerts/dismiss", methods=["POST"])
    def dismiss_user_alert():
        key = (request.form.get("alert_key") or "").strip()
        next_url = request.form.get("next") or request.referrer or url_for("alerts_inbox")
        if not next_url.startswith("/"):
            next_url = url_for("alerts_inbox")
        if dismiss_alert(g.current_user, key):
            flash("تم إخفاء التنبيه. لو الحالة اتغيّرت هيرجع يظهر.", "success")
        else:
            flash("التنبيه مش موجود أو اتنحّى قبل كده.", "info")
        return redirect(next_url)
