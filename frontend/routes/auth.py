import requests

from flask import (
    Blueprint,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    session,
    current_app
)

from services.api_client import APIClient


auth_bp = Blueprint("auth", __name__)


@auth_bp.route("/register", methods=["GET", "POST"])
def register():

    if request.method == "GET":
        return render_template("register.html")

    payload = {
        "name": request.form.get("name", "").strip(),
        "email": request.form.get("email", "").strip().lower(),
        "password": request.form.get("password", ""),
        "phone": request.form.get("phone", "").strip(),
        "yearsExperience": int(request.form.get("yearsExperience")),
        "licenceNumber": request.form.get("licenceNumber", "").strip(),
        "licenceAuthority": request.form.get("licenceAuthority", "").strip(),
        "specialisation": request.form.get("specialisation", "").strip(),
        "qualifications": [],
        "address": {
            "addressLine1": request.form.get("addressLine1", "").strip(),
            "addressLine2": request.form.get("addressLine2", "").strip(),
            "city": request.form.get("city", "").strip(),
            "postalCode": request.form.get("postalCode", "").strip()
        },
        "languagesSpoken": [
            language.strip()
            for language in request.form.get("languagesSpoken", "").split(",")
            if language.strip()
        ],
        "hospital": {
            "name": request.form.get("hospitalName", "").strip(),
            "department": request.form.get("department", "").strip()
        }
    }

    # Support multiple qualifications.
    degrees = request.form.getlist("degree[]")
    institutions = request.form.getlist("institution[]")
    years = request.form.getlist("year[]")

    for degree, institution, year in zip(degrees, institutions, years):

        if degree.strip() and institution.strip() and year.strip():
            try:
                payload["qualifications"].append({
                    "degree": degree.strip(),
                    "institution": institution.strip(),
                    "year": int(year)
                })
            except ValueError:
                flash("Qualification year must be a valid number.", "danger")
                return render_template("register.html")

    required_fields = [
        payload["name"],
        payload["email"],
        payload["password"],
        payload["phone"],
        payload["licenceNumber"],
        payload["licenceAuthority"],
        payload["specialisation"]
    ]

    if not all(required_fields):
        flash("Please complete all required fields.", "danger")
        return render_template("register.html")

    try:
        response = requests.post(
            f"{APIClient.base_url()}/auth/doctors/register",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=30
        )

        data = response.json()

        if response.status_code in (200, 201):
            doctor_id = data.get("data", {}).get("doctorId")

            current_app.logger.info(
                "Doctor registration successful. doctor_id=%s",
                doctor_id
            )

            flash(
                "Registration successful. Please log in.",
                "success"
            )

            return redirect(url_for("auth.login"))

        current_app.logger.warning(
            "Doctor registration rejected. status=%s",
            response.status_code
        )

        flash(
            data.get("message", "Registration failed."),
            "danger"
        )

    except requests.RequestException:
        current_app.logger.exception("Doctor registration API failed.")
        flash("Unable to reach the server. Please try again.", "danger")

    except ValueError:
        flash("Invalid response received from the server.", "danger")

    return render_template("register.html")


@auth_bp.route("/login", methods=["GET", "POST"])
def login():

    if request.method == "GET":
        if session.get("access_token"):
            return redirect(url_for("dashboard.index"))

        return render_template("login.html")

    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")

    if not email or not password:
        flash("Email and password are required.", "danger")
        return render_template("login.html")

    try:
        response = requests.post(
            f"{APIClient.base_url()}/auth/doctors/login",
            json={
                "email": email,
                "password": password
            },
            headers={"Content-Type": "application/json"},
            timeout=20
        )

        data = response.json()

        if response.status_code == 200:

            access_token = data.get("access_token")
            refresh_token = data.get("refresh_token")

            if not access_token or not refresh_token:
                current_app.logger.error(
                    "Login response missing authentication tokens."
                )
                flash("Invalid authentication response.", "danger")
                return render_template("login.html")

            # Clear any previous session to prevent session fixation.
            session.clear()

            session["access_token"] = access_token
            session["refresh_token"] = refresh_token
            session["token_type"] = data.get("token_type", "Bearer")
            session["expires_in"] = data.get("expires_in", 900)
            session.permanent = True

            current_app.logger.info("Doctor login successful.")

            return redirect(url_for("dashboard.index"))

        current_app.logger.warning(
            "Doctor login rejected. status=%s",
            response.status_code
        )

        flash(
            data.get("message", "Invalid email or password."),
            "danger"
        )

    except requests.RequestException:
        current_app.logger.exception("Doctor login API failed.")
        flash("Unable to reach the server. Please try again.", "danger")

    except ValueError:
        flash("Invalid response received from the server.", "danger")

    return render_template("login.html")
@auth_bp.route("/test-refresh")
def test_refresh():

    from services.api_client import APIClient

    session["access_token"] = "invalid-token"

    response = APIClient.request(
        "GET",
        "/doctors/me"
    )

    if response.status_code == 200:
        return "Token refresh successful!"

    return f"Request failed: {response.status_code} - {response.text}"


@auth_bp.route("/logout", methods=["POST"])
def logout():

    refresh_token = session.get("refresh_token")

    try:
        if refresh_token:
            response = requests.post(
                f"{APIClient.base_url()}/auth/logout",
                json={"refresh_token": refresh_token},
                headers={"Content-Type": "application/json"},
                timeout=20
            )

            if response.status_code not in (200, 204):
                current_app.logger.warning(
                    "Backend logout returned status=%s",
                    response.status_code
                )

    except requests.RequestException:
        current_app.logger.exception("Backend logout request failed.")

    finally:
        session.clear()

    flash("You have been logged out.", "success")

    return redirect(url_for("auth.login"))