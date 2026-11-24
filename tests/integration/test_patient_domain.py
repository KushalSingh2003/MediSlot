"""Deployed Milestone 4 integration checks. Requires a reachable deployed API."""
import os
import uuid
from datetime import date, timedelta

import requests

BASE = os.environ["MEDISLOT_API_BASE_URL"].rstrip("/")
TIMEOUT = 25


def register_patient(tag):
    suffix = uuid.uuid4().hex[:12]
    payload = {
        "firstName": "Milestone",
        "lastName": tag.title(),
        "email": f"milestone4-{tag}-{suffix}@example.com",
        "password": "Milestone4Pass123",
        "phone": "+1 415 555 0136",
        "city": "Hyderabad",
        "dateOfBirth": "1998-04-12",
        "gender": "Prefer not to say",
    }
    result = requests.post(f"{BASE}/auth/patients/register", json=payload, timeout=TIMEOUT)
    assert result.status_code in (200, 201), result.text
    login = requests.post(f"{BASE}/auth/patients/login",
        json={"email": payload["email"], "password": payload["password"]}, timeout=TIMEOUT)
    assert login.status_code == 200, login.text
    data = login.json()
    return payload, {"Authorization": f"Bearer {data['access_token']}"}


def test_patient_profile_isolation_doctor_discovery_and_slots():
    first, first_headers = register_patient("alpha")
    second, second_headers = register_patient("beta")
    try:
        own = requests.get(f"{BASE}/patients/me", headers=first_headers, timeout=TIMEOUT)
        assert own.status_code == 200, own.text
        assert own.json()["profile"]["email"] == first["email"]

        update = requests.put(f"{BASE}/patients/me", headers=first_headers,
            json={"city": "Hyderabad", "emergencyContact": "+1 415 555 0199"}, timeout=TIMEOUT)
        assert update.status_code == 200, update.text
        assert update.json()["profile"]["emergency_contact"] == "+1 415 555 0199"

        # The endpoint derives the target patient exclusively from the signed token.
        second_profile = requests.get(f"{BASE}/patients/me", headers=second_headers, timeout=TIMEOUT)
        assert second_profile.status_code == 200, second_profile.text
        assert second_profile.json()["profile"]["email"] == second["email"]
        attempted_override = requests.put(f"{BASE}/patients/me", headers=second_headers,
            json={"patientId": first["email"], "city": "Unauthorized"}, timeout=TIMEOUT)
        assert attempted_override.status_code == 400, attempted_override.text

        # Search is public and supports each Milestone 4 filter independently and together.
        search = requests.get(f"{BASE}/doctors", params={"page": 1, "pageSize": 5}, timeout=TIMEOUT)
        assert search.status_code == 200, search.text
        payload = search.json()
        assert "doctors" in payload and "pagination" in payload
        for field in ("name", "hospital", "specialisation", "city"):
            filtered = requests.get(f"{BASE}/doctors", params={field: "a", "page": 1, "pageSize": 5}, timeout=TIMEOUT)
            assert filtered.status_code == 200, filtered.text
        combined = requests.get(f"{BASE}/doctors", params={"name": "a", "hospital": "a",
            "specialisation": "a", "city": "a", "page": 1, "pageSize": 5}, timeout=TIMEOUT)
        assert combined.status_code == 200, combined.text

        if payload["doctors"]:
            doctor_id = payload["doctors"][0]["doctor_id"]
            public_profile = requests.get(f"{BASE}/doctors/{doctor_id}", timeout=TIMEOUT)
            assert public_profile.status_code == 200, public_profile.text
            doctor = public_profile.json()["doctor"]
            assert doctor["doctor_id"] == doctor_id
            assert "consultationTypes" in doctor
            start = date.today()
            end = start + timedelta(days=6)
            availability = requests.get(f"{BASE}/doctors/{doctor_id}/slots",
                params={"from": start.isoformat(), "to": end.isoformat()}, timeout=TIMEOUT)
            assert availability.status_code == 200, availability.text
            assert "slots" in availability.json()

        # A patient with no appointment history can delete their own profile.
        for headers in (first_headers, second_headers):
            deleted = requests.delete(f"{BASE}/patients/me", headers=headers, timeout=TIMEOUT)
            assert deleted.status_code == 200, deleted.text
    finally:
        # Best-effort cleanup if an assertion failed before the explicit delete.
        for headers in (first_headers, second_headers):
            try:
                requests.delete(f"{BASE}/patients/me", headers=headers, timeout=TIMEOUT)
            except requests.RequestException:
                pass
