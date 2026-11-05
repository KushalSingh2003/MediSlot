
from datetime import date, datetime
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


schedule_bp = Blueprint("schedule", __name__)


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("access_token"):
            flash("Please log in to continue.", "warning")
            return redirect(url_for("auth.login"))

        return view(*args, **kwargs)

    return wrapped


@schedule_bp.route("/schedule", methods=["GET"])
@login_required
def index():

    # Default to today's date if no date is selected.
    selected_date = request.args.get("date") or date.today().isoformat()

    try:
        datetime.strptime(selected_date, "%Y-%m-%d")
    except ValueError:
        selected_date = date.today().isoformat()
        flash("Invalid date selected.", "warning")

    summary = {
        "totalSlots": 0,
        "availableSlots": 0,
        "bookedSlots": 0,
        "closedSlots": 0
    }

    slots = []

    try:
        # Always send the date to the backend API.
        response = APIClient.request(
            "GET",
            "/doctors/me/schedule",
            params={
                "date": selected_date
            }
        )

        if response.status_code == 200:
            data = response.json()

            summary = data.get("summary", summary)
            slots = data.get("schedule", [])

        else:
            flash(
                "Unable to load schedule for the selected date.",
                "danger"
            )

    except requests.RequestException:
        flash(
            "Unable to connect to the MediSlot API.",
            "danger"
        )

    return render_template(
        "schedule.html",
        selected_date=selected_date,
        summary=summary,
        slots=slots
    )


@schedule_bp.route("/schedule/slots/<int:slot_id>/cancel", methods=["POST"])
@login_required
def cancel_slot(slot_id):

    selected_date = request.form.get("date") or date.today().isoformat()

    try:
        response = APIClient.request(
            "DELETE",
            f"/doctors/me/slots/{slot_id}"
        )

        data = response.json()

        if response.status_code == 200 and data.get("success"):
            appointments = data.get("appointments", {})
            refunds = data.get("refunds", {})

            message = data.get(
                "message",
                "Slot successfully cancelled."
            )

            flash(message, "success")

            cancelled_count = appointments.get("cancelled", 0)
            if cancelled_count:
                flash(
                    f"{cancelled_count} associated appointment(s) cancelled.",
                    "info"
                )

            refund_count = refunds.get("initiated", 0)
            if refund_count:
                flash(
                    f"{refund_count} refund(s) initiated.",
                    "info"
                )

        else:
            message = data.get(
                "message",
                "Unable to cancel this slot."
            )
            flash(message, "danger")

    except requests.RequestException:
        flash(
            "Unable to connect to the MediSlot API.",
            "danger"
        )

    except ValueError:
        flash(
            "The API returned an invalid response.",
            "danger"
        )

    return redirect(
        url_for("schedule.index", date=selected_date)
    )
