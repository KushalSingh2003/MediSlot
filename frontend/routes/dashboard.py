from functools import wraps
from flask import Blueprint, session, redirect, url_for, flash, render_template
from services.api_client import APIClient
from datetime import date
dashboard_bp = Blueprint("dashboard", __name__)


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("access_token"):
            flash("Please log in to continue.", "warning")
            return redirect(url_for("auth.login"))
        return view(*args, **kwargs)
    return wrapped


@dashboard_bp.route("/dashboard")
@login_required
def index():

    profile_response = APIClient.request(
        "GET", "/doctors/me"
    )

    appointments_response = APIClient.request(
        "GET", "/doctors/me/appointments"
    )

    if profile_response.status_code != 200:
        flash("Unable to load doctor profile.", "danger")
        profile = {}
    else:
        profile = profile_response.json()

    if appointments_response.status_code != 200:
        flash("Unable to load appointments.", "danger")
        appointments = []
    else:
        appointments = appointments_response.json().get(
            "appointments", []
        )

    total_appointments = len(appointments)

    booked = sum(
        1 for appointment in appointments
        if appointment.get("status") == "BOOKED"
    )

    completed = sum(
        1 for appointment in appointments
        if appointment.get("status") == "COMPLETED"
    )

    cancelled = sum(
        1 for appointment in appointments
        if appointment.get("status") == "CANCELLED"
    )

    stats = {
        "total": total_appointments,
        "booked": booked,
        "completed": completed,
        "cancelled": cancelled
    }
    today = date.today().isoformat()

    today_appointments = [
        appointment for appointment in appointments
        if appointment.get("slot", {}).get("date") == today
    ]

    return render_template(
        "dashboard.html",
        profile=profile,
        appointments=appointments,
        today_appointments=today_appointments,
        stats=stats
    )