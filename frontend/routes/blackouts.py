
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


blackouts_bp = Blueprint("blackouts", __name__)


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("access_token"):
            flash("Please log in to continue.", "warning")
            return redirect(url_for("auth.login"))

        return view(*args, **kwargs)

    return wrapped


@blackouts_bp.route("/blackouts", methods=["GET"])
@login_required
def index():
    blackouts = []

    try:
        response = APIClient.request(
            "GET",
            "/doctors/me/blackouts"
        )

        if response.status_code == 200:
            data = response.json()
            blackouts = data.get("blackouts", [])
        else:
            flash(
                "Unable to load blackout dates.",
                "danger"
            )

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

    return render_template(
        "blackouts.html",
        blackouts=blackouts
    )


@blackouts_bp.route("/blackouts/add", methods=["POST"])
@login_required
def add_blackout():
    blackout_date = request.form.get("blackoutDate", "").strip()

    if not blackout_date:
        flash("Please select a blackout date.", "warning")
        return redirect(url_for("blackouts.index"))

    try:
        response = APIClient.request(
            "POST",
            "/doctors/me/blackouts",
            json={"blackoutDate": blackout_date}
        )

        data = response.json()

        if response.status_code == 201 and data.get("success"):
            slots = data.get("slots", {})
            appointments = data.get("appointments", {})

            flash(
                data.get("message", "Blackout date added."),
                "success"
            )

            if slots.get("closed", 0):
                flash(
                    f"{slots['closed']} slot(s) closed.",
                    "info"
                )

            if appointments.get("cancelled", 0):
                flash(
                    f"{appointments['cancelled']} appointment(s) cancelled.",
                    "warning"
                )

        else:
            flash(
                data.get("message", "Unable to add blackout date."),
                "danger"
            )

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

    return redirect(url_for("blackouts.index"))


@blackouts_bp.route(
    "/blackouts/<int:blackout_id>/delete",
    methods=["POST"]
)
@login_required
def delete_blackout(blackout_id):
    try:
        response = APIClient.request(
            "DELETE",
            f"/doctors/me/blackouts/{blackout_id}"
        )

        data = response.json()

        if response.status_code == 200 and data.get("success"):
            flash(
                data.get("message", "Blackout date deleted."),
                "success"
            )
        else:
            flash(
                data.get("message", "Unable to delete blackout date."),
                "danger"
            )

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

    return redirect(url_for("blackouts.index"))
