from functools import wraps

from flask import (
    Blueprint,
    render_template,
    session,
    redirect,
    url_for,
    flash,
    request
)

from services.api_client import APIClient

profile_bp = Blueprint("profile", __name__)


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("access_token"):
            flash("Please log in to continue.", "warning")
            return redirect(url_for("auth.login"))

        return view(*args, **kwargs)

    return wrapped
@profile_bp.route("/profile/photo/upload-url", methods=["POST"])
@login_required
def generate_photo_upload_url():
    data = request.get_json(silent=True) or {}
    content_type = data.get("contentType")

    allowed_types = {
        "image/jpeg",
        "image/png",
        "image/webp"
    }

    if content_type not in allowed_types:
        return {"error": "Unsupported image type."}, 400

    response = APIClient.request(
        "POST",
        "/doctors/me/photo",
        json={"contentType": content_type}
    )

    if response.status_code not in (200, 201):
        try:
            error = response.json().get("message", "Could not generate upload URL.")
        except ValueError:
            error = "Could not generate upload URL."

        return {"error": error}, response.status_code

    return response.json(), response.status_code


@profile_bp.route("/profile/photo/save", methods=["POST"])
@login_required
def save_profile_photo():
    data = request.get_json(silent=True) or {}
    photo_key = data.get("photoKey")

    if not photo_key:
        return {"error": "Photo key is required."}, 400

    response = APIClient.request(
        "PUT",
        "/doctors/me",
        json={"photoKey": photo_key}
    )

    if response.status_code != 200:
        try:
            error = response.json().get("message", "Could not update profile picture.")
        except ValueError:
            error = "Could not update profile picture."

        return {"error": error}, response.status_code

    return {"message": "Profile picture updated successfully."}, 200


@profile_bp.route("/profile", methods=["GET", "POST"])
@login_required
def index():

    if request.method == "POST":

        full_name = request.form.get("fullName", "").strip()
        phone = request.form.get("phone", "").strip()
        years_experience = request.form.get(
            "yearsExperience", ""
        ).strip()

        if not full_name:
            flash("Full name is required.", "danger")
            return redirect(url_for("profile.index"))

        if not phone:
            flash("Phone number is required.", "danger")
            return redirect(url_for("profile.index"))

        try:
            years_experience = int(years_experience)
        except (ValueError, TypeError):
            flash("Experience must be a valid integer.", "danger")
            return redirect(url_for("profile.index"))

        if years_experience < 0:
            flash("Experience cannot be negative.", "danger")
            return redirect(url_for("profile.index"))

        payload = {
            "fullName": full_name,
            "phone": phone,
            "yearsExperience": years_experience
        }

        response = APIClient.request(
            "PUT",
            "/doctors/me",
            json=payload
        )

        if response.status_code == 200:
            flash("Profile updated successfully.", "success")
        else:
            try:
                error_message = response.json().get(
                    "message",
                    "Unable to update profile."
                )
            except ValueError:
                error_message = "Unable to update profile."

            flash(error_message, "danger")

        return redirect(url_for("profile.index"))
    # GET doctor profile
    response = APIClient.request("GET", "/doctors/me")

    if response.status_code != 200:
        flash("Unable to load your profile.", "danger")
        profile = {}

    else:
        profile = response.json()

        # Fetch profile picture URL if photoKey exists
        # Fetch profile picture URL if photoKey exists
    if profile.get("photoKey"):
        photo_response = APIClient.request(
            "GET",
            "/doctors/me/photo"
        )

        print("\n========== PHOTO API DEBUG ==========")
        print("PHOTO API STATUS:", photo_response.status_code)

        try:
            photo_data = photo_response.json()
            print("PHOTO API MESSAGE:", photo_data.get("message"))
            print("PHOTO URL GENERATED:", bool(photo_data.get("photoUrl")))

            if photo_response.status_code == 200:
                profile["photoUrl"] = photo_data.get("photoUrl")
            else:
                print("PHOTO API ERROR:", photo_data)
                profile["photoUrl"] = None

        except ValueError:
            print("PHOTO API RESPONSE:", photo_response.text)
            profile["photoUrl"] = None

        print("=====================================\n")

    return render_template(
        "profile.html",
        profile=profile
    )

  