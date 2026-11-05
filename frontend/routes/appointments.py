
from functools import wraps

import requests

from flask import (
    Blueprint,
    session,
    redirect,
    url_for,
    flash,
    render_template,
    request
)

from services.api_client import APIClient


appointments_bp = Blueprint("appointments", __name__)


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("access_token"):
            flash("Please log in to continue.", "warning")
            return redirect(url_for("auth.login"))

        return view(*args, **kwargs)

    return wrapped


@appointments_bp.route("/appointments", methods=["GET"])
@login_required
def index():

    # Read filters from the frontend.
    from_date = request.args.get("from", "").strip()
    to_date = request.args.get("to", "").strip()
    status = request.args.get("status", "").strip()

    params = {}

    if from_date:
        params["from"] = from_date

    if to_date:
        params["to"] = to_date

    if status:
        params["status"] = status

    appointments = []
    total_appointments = 0

    filters = {
        "from": from_date or None,
        "to": to_date or None,
        "status": status or None
    }

    try:
        response = APIClient.request(
            "GET",
            "/doctors/me/appointments",
            params=params
        )

        if response.status_code == 200:

            data = response.json()

            appointments = data.get("appointments", [])
            total_appointments = data.get(
                "totalAppointments",
                len(appointments)
            )

            filters = data.get("filters", filters)

        else:
            flash(
                "Unable to load appointments. Please try again.",
                "danger"
            )

    except requests.RequestException:
        flash(
            "Unable to connect to the MediSlot API.",
            "danger"
        )

    return render_template(
        "appointments.html",
        appointments=appointments,
        total_appointments=total_appointments,
        filters=filters
    )
