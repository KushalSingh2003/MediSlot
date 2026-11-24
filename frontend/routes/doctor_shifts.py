from datetime import date, timedelta
from functools import wraps

from flask import (
    Blueprint,
    flash,
    redirect,
    render_template,
    session,
    url_for,
    request,
)

from services.api_client import APIClient


doctor_shifts_bp = Blueprint(
    "doctor_shifts",
    __name__,
    url_prefix="/doctor/shifts",
)


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("access_token"):
            flash("Please log in to continue.", "warning")
            return redirect(url_for("auth.login"))
        return view(*args, **kwargs)

    return wrapped


def _response_message(api_response, fallback):
    try:
        payload = api_response.json()
    except ValueError:
        return fallback

    if isinstance(payload, dict):
        return payload.get("message", fallback)

    return fallback


@doctor_shifts_bp.route("/", methods=["GET"])
@login_required
def index():
    start_date = date.today()
    end_date = start_date + timedelta(days=29)

    templates_response = APIClient.request("GET", "/slot-templates")
    templates = []

    if templates_response.status_code == 200:
        try:
            payload = templates_response.json()
        except ValueError:
            payload = None

        if isinstance(payload, list):
            templates = payload
        elif isinstance(payload, dict) and isinstance(
            payload.get("templates"), list
        ):
            templates = payload["templates"]
        else:
            flash("The templates API returned an unexpected response.", "danger")
    else:
        flash(
            _response_message(
                templates_response,
                "Unable to load shift templates.",
            ),
            "danger",
        )

    shifts_path = (
        "/doctors/me/shifts"
        f"?from={start_date.isoformat()}&to={end_date.isoformat()}"
    )
    shifts_response = APIClient.request("GET", shifts_path)
    shifts = []

    if shifts_response.status_code == 200:
        try:
            payload = shifts_response.json()
        except ValueError:
            payload = None

        if isinstance(payload, dict) and isinstance(payload.get("shifts"), list):
            shifts = payload["shifts"]
        else:
            flash("The shifts API returned an unexpected response.", "danger")
    else:
        flash(
            _response_message(
                shifts_response,
                "Unable to load your shift schedule.",
            ),
            "danger",
        )

    return render_template(
        "doctor_shifts.html",
        templates=templates,
        shifts=shifts,
        has_shifts=bool(shifts),
        start_date=start_date.isoformat(),
        end_date=end_date.isoformat(),
    )


@doctor_shifts_bp.route("/select", methods=["POST"])
@login_required
def select_template():
    raw_template_id = request.form.get("template_id", "").strip()

    try:
        template_id = int(raw_template_id)
        if template_id <= 0:
            raise ValueError
    except (TypeError, ValueError):
        flash("Choose a valid shift template.", "danger")
        return redirect(url_for("doctor_shifts.index"))

    api_response = APIClient.request(
        "POST",
        "/doctors/me/shifts",
        json={"template_id": template_id},
    )

    if api_response.status_code in (200, 201):
        flash(
            _response_message(api_response, "Shift template selected."),
            "success",
        )
    else:
        flash(
            _response_message(api_response, "Unable to select that template."),
            "danger",
        )

    return redirect(url_for("doctor_shifts.index"))


@doctor_shifts_bp.route("/delete", methods=["POST"])
@login_required
def delete_template_selection():
    api_response = APIClient.request(
        "DELETE",
        "/doctors/me/shifts",
    )

    if api_response.status_code == 200:
        flash(
            _response_message(
                api_response,
                "Your selected template was removed. You can choose another.",
            ),
            "success",
        )
    else:
        flash(
            _response_message(
                api_response,
                "Unable to remove your selected template.",
            ),
            "danger",
        )

    return redirect(url_for("doctor_shifts.index"))