"""Patient-facing pages. This Flask app remains an API client; it never accesses RDS."""
from functools import wraps
import requests
from flask import Blueprint, current_app, flash, redirect, render_template, request, session, url_for
from services.api_client import APIClient

patient_bp = Blueprint("patient", __name__, url_prefix="/patient")


def patient_login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("access_token") or session.get("role") != "patient":
            flash("Please sign in with a patient account.", "warning")
            return redirect(url_for("patient.login"))
        return view(*args, **kwargs)
    return wrapped


@patient_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("patient_login.html")
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    if not email or not password:
        flash("Email and password are required.", "danger")
        return render_template("patient_login.html")
    try:
        result = requests.post(f"{APIClient.base_url()}/auth/patients/login",
            json={"email": email, "password": password}, timeout=20)
        data = result.json()
        if result.status_code != 200:
            flash(data.get("message", "Invalid email or password."), "danger")
            return render_template("patient_login.html")
        if not data.get("access_token") or not data.get("refresh_token"):
            flash("Invalid authentication response.", "danger")
            return render_template("patient_login.html")
        session.clear()
        session.update(access_token=data["access_token"], refresh_token=data["refresh_token"],
                       token_type=data.get("token_type", "Bearer"), expires_in=data.get("expires_in", 900), role="patient")
        session.permanent = True
        return redirect(url_for("patient.dashboard"))
    except (requests.RequestException, ValueError):
        current_app.logger.exception("Patient login failed")
        flash("Unable to sign in. Please try again.", "danger")
        return render_template("patient_login.html")


@patient_bp.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "GET":
        return render_template("patient_register.html")
    payload = {"firstName": request.form.get("firstName", "").strip(),
        "lastName": request.form.get("lastName", "").strip(),
        "email": request.form.get("email", "").strip().lower(),
        "password": request.form.get("password", ""),
        "phone": request.form.get("phone", "").strip(),
        "city": request.form.get("city", "").strip(),
        "dateOfBirth": request.form.get("dateOfBirth", "").strip(),
        "gender": request.form.get("gender", "").strip()}
    if not all(payload[k] for k in ("firstName", "lastName", "email", "password")):
        flash("First name, last name, email and password are required.", "danger")
        return render_template("patient_register.html")
    try:
        result = requests.post(f"{APIClient.base_url()}/auth/patients/register", json=payload, timeout=25)
        data = result.json()
        if result.status_code in (200, 201):
            flash("Patient account created. Please sign in.", "success")
            return redirect(url_for("patient.login"))
        flash(data.get("message", "Registration failed."), "danger")
    except (requests.RequestException, ValueError):
        current_app.logger.exception("Patient registration failed")
        flash("Unable to register. Please try again.", "danger")
    return render_template("patient_register.html")


@patient_bp.route("/dashboard")
@patient_login_required
def dashboard():
    filters = {key: request.args.get(key, "").strip() for key in ("name", "hospital", "specialisation", "city")}
    params = {k: v for k, v in filters.items() if v}
    params.update(page=request.args.get("page", 1), pageSize=10)
    try:
        profile_response = APIClient.request("GET", "/patients/me")
        search_response = APIClient.request("GET", "/doctors", authenticated=False, params=params)
        profile = profile_response.json().get("profile", {}) if profile_response.status_code == 200 else {}
        results = search_response.json() if search_response.status_code == 200 else {"doctors": [], "pagination": {}}
        if profile_response.status_code != 200:
            flash("Unable to load patient profile.", "warning")
        if search_response.status_code != 200:
            flash(search_response.json().get("message", "Doctor search failed."), "danger")
        return render_template("patient_dashboard.html", profile=profile, doctors=results.get("doctors", []),
            pagination=results.get("pagination", {}), filters=filters)
    except requests.RequestException:
        flash("Unable to connect to MediSlot.", "danger")
        return render_template("patient_dashboard.html", profile={}, doctors=[], pagination={}, filters=filters)


@patient_bp.route("/profile", methods=["GET", "POST"])
@patient_login_required
def profile():
    if request.method == "POST":
        payload = {"name": request.form.get("name", "").strip(), "phone": request.form.get("phone", "").strip(),
            "city": request.form.get("city", "").strip(), "dateOfBirth": request.form.get("dateOfBirth", "").strip() or None,
            "gender": request.form.get("gender", "").strip() or None,
            "emergencyContact": request.form.get("emergencyContact", "").strip() or None}
        result = APIClient.request("PUT", "/patients/me", json=payload)
        if result.status_code == 200:
            flash("Profile updated successfully.", "success")
            return redirect(url_for("patient.profile"))
        try: message = result.json().get("message", "Unable to update profile.")
        except ValueError: message = "Unable to update profile."
        flash(message, "danger")
    result = APIClient.request("GET", "/patients/me")
    profile_data = result.json().get("profile", {}) if result.status_code == 200 else {}
    return render_template("patient_profile.html", profile=profile_data)


@patient_bp.route("/doctors/<doctor_id>")
@patient_login_required
def doctor_detail(doctor_id):
    from_date = request.args.get("from", "")
    to_date = request.args.get("to", "")
    doctor_response = APIClient.request("GET", f"/doctors/{doctor_id}", authenticated=False)
    doctor = doctor_response.json().get("doctor") if doctor_response.status_code == 200 else None
    slots = []
    if doctor and from_date and to_date:
        slot_response = APIClient.request("GET", f"/doctors/{doctor_id}/slots", authenticated=False,
            params={"from": from_date, "to": to_date})
        if slot_response.status_code == 200:
            slots = slot_response.json().get("slots", [])
        else:
            flash(slot_response.json().get("message", "Unable to load availability."), "danger")
    if not doctor:
        flash("Doctor not found.", "danger")
        return redirect(url_for("patient.dashboard"))
    return render_template("patient_doctor.html", doctor=doctor, slots=slots, from_date=from_date, to_date=to_date)


@patient_bp.route("/logout", methods=["POST"])
def logout():
    refresh = session.get("refresh_token")
    if refresh:
        try: requests.post(f"{APIClient.base_url()}/auth/logout", json={"refresh_token": refresh}, timeout=10)
        except requests.RequestException: current_app.logger.warning("Patient logout API could not be reached")
    session.clear()
    return redirect(url_for("patient.login"))
