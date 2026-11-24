
from functools import wraps

from flask import (
    Blueprint,
    session,
    redirect,
    url_for,
    flash,
    render_template,
    request,
    current_app
)

from services.api_client import APIClient


consultation_types_bp = Blueprint(
    "consultation_types",
    __name__
)


# =========================================================
# LOGIN REQUIRED DECORATOR
# =========================================================

def login_required(view):

    @wraps(view)
    def wrapped(*args, **kwargs):

        if not session.get("access_token"):
            flash("Please log in to continue.", "warning")
            return redirect(url_for("auth.login"))

        return view(*args, **kwargs)

    return wrapped


# =========================================================
# HELPER: EXTRACT API ERROR MESSAGE
# =========================================================

def get_error_message(response, default_message):

    try:
        return response.json().get("message", default_message)

    except (ValueError, AttributeError):
        return default_message


# =========================================================
# GET: DISPLAY CONSULTATION TYPES
# =========================================================

@consultation_types_bp.route("/consultation-types", methods=["GET"])
@login_required
def consultation_types():

    master_types = []
    doctor_types = []

    try:

        # Fetch all available master consultation types
        master_response = APIClient.request(
            "GET",
            "/doctors/consultation-types"
        )

        if master_response.status_code == 200:

            master_types = master_response.json().get(
                "consultationTypes", []
            )

        else:

            current_app.logger.error(
                "Master consultation types API failed. "
                "Status: %s, Response: %s",
                master_response.status_code,
                master_response.text
            )

            flash(
                get_error_message(
                    master_response,
                    "Unable to load available consultation types."
                ),
                "danger"
            )

        # Fetch consultation types selected by the logged-in doctor
        doctor_response = APIClient.request(
            "GET",
            "/doctors/me/consultation-types"
        )

        if doctor_response.status_code == 200:

            doctor_types = doctor_response.json().get(
                "consultationTypes", []
            )

        else:

            current_app.logger.error(
                "Doctor consultation types API failed. "
                "Status: %s, Response: %s",
                doctor_response.status_code,
                doctor_response.text
            )

            flash(
                get_error_message(
                    doctor_response,
                    "Unable to load your consultation types."
                ),
                "danger"
            )

    except Exception:

        current_app.logger.exception(
            "Error retrieving consultation types."
        )

        flash(
            "Unable to connect to the server.",
            "danger"
        )

    return render_template(
        "consultation_types.html",
        master_types=master_types,
        doctor_types=doctor_types
    )


# =========================================================
# POST: ADD CONSULTATION TYPE
# =========================================================

@consultation_types_bp.route(
    "/consultation-types/add",
    methods=["POST"]
)
@login_required
def add_consultation_type():

    try:

        payload = {
            "consultationTypeId": request.form.get(
                "consultationTypeId"
            ),
            "fee": request.form.get("fee"),
            "capacity": request.form.get("capacity"),
            "active": True
        }

        response = APIClient.request(
            "POST",
            "/doctors/me/consultation-types",
            json=payload
        )

        if response.status_code in (200, 201):

            flash(
                "Consultation type added successfully.",
                "success"
            )

        else:

            current_app.logger.error(
                "Add consultation type failed. "
                "Status: %s, Response: %s",
                response.status_code,
                response.text
            )

            flash(
                get_error_message(
                    response,
                    "Unable to add consultation type."
                ),
                "danger"
            )

    except Exception:

        current_app.logger.exception(
            "Error adding consultation type."
        )

        flash(
            "Unable to connect to the server.",
            "danger"
        )

    return redirect(
        url_for("consultation_types.consultation_types")
    )


# =========================================================
# PUT: UPDATE CONSULTATION TYPE
# =========================================================

@consultation_types_bp.route(
    "/consultation-types/update",
    methods=["POST"]
)
@login_required
def update_consultation_type():

    try:

        payload = {
            "consultationTypeId": request.form.get(
                "consultationTypeId"
            ),
            "fee": request.form.get("fee"),
            "capacity": request.form.get("capacity"),
            "active": request.form.get("active") == "true"
        }

        response = APIClient.request(
            "PUT",
            "/doctors/me/consultation-types",
            json=payload
        )

        if response.status_code == 200:

            flash(
                "Consultation type updated successfully.",
                "success"
            )

        else:

            current_app.logger.error(
                "Update consultation type failed. "
                "Status: %s, Response: %s",
                response.status_code,
                response.text
            )

            flash(
                get_error_message(
                    response,
                    "Unable to update consultation type."
                ),
                "danger"
            )

    except Exception:

        current_app.logger.exception(
            "Error updating consultation type."
        )

        flash(
            "Unable to connect to the server.",
            "danger"
        )

    return redirect(
        url_for("consultation_types.consultation_types")
    )


# =========================================================
# DELETE: REMOVE CONSULTATION TYPE
# =========================================================

@consultation_types_bp.route(
    "/consultation-types/delete/<int:type_id>",
    methods=["POST"]
)
@login_required
def remove_consultation_type(type_id):

    try:

        response = APIClient.request(
            "DELETE",
            f"/doctors/me/consultation-types/{type_id}"
        )

        if response.status_code in (200, 204):

            flash(
                "Consultation type removed successfully.",
                "success"
            )

        else:

            current_app.logger.error(
                "Remove consultation type failed. "
                "Status: %s, Response: %s",
                response.status_code,
                response.text
            )

            flash(
                get_error_message(
                    response,
                    "Unable to remove consultation type."
                ),
                "danger"
            )

    except Exception:

        current_app.logger.exception(
            "Error removing consultation type."
        )

        flash(
            "Unable to connect to the server.",
            "danger"
        )

    return redirect(
        url_for("consultation_types.consultation_types")
    )
