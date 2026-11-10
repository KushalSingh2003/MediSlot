"""Public doctor search, public profile and date-range slot availability."""
import json
import logging
import os
from datetime import date

import boto3
import pymysql

logger = logging.getLogger()
logger.setLevel(logging.INFO)
ssm = boto3.client("ssm")
secrets = boto3.client("secretsmanager")
SSM_PREFIX = os.environ["SSM_PREFIX"].rstrip("/")


def db_connection():
    names = [f"{SSM_PREFIX}/db/{key}" for key in ("endpoint", "port", "name", "secret-arn")]
    result = ssm.get_parameters(Names=names, WithDecryption=True)
    params = {p["Name"].rsplit("/", 1)[-1]: p["Value"] for p in result["Parameters"]}
    if result.get("InvalidParameters") or len(params) != 4:
        raise RuntimeError("Database configuration is incomplete")
    secret = json.loads(secrets.get_secret_value(SecretId=params["secret-arn"])["SecretString"])
    return pymysql.connect(host=params["endpoint"], port=int(params["port"]),
        user=secret.get("username", params.get("username")), password=secret["password"],
        database=params["name"], connect_timeout=5, read_timeout=10, write_timeout=10,
        cursorclass=pymysql.cursors.DictCursor, autocommit=True)


def response(code, body):
    return {"statusCode": code, "headers": {"Content-Type": "application/json"},
            "body": json.dumps(body, default=str)}


def query_params(event):
    return event.get("queryStringParameters") or {}


def search_doctors(cur, q):
    try:
        page = max(1, int(q.get("page", 1)))
        page_size = min(50, max(1, int(q.get("pageSize", 10))))
    except (TypeError, ValueError):
        raise ValueError("page and pageSize must be positive integers")
    filters, values = [], []
    # Prefix matching retains index usability; use % only after the supplied prefix.
    if q.get("name", "").strip():
        filters.append("d.name LIKE %s")
        values.append(q["name"].strip() + "%")
    if q.get("hospital", "").strip():
        filters.append("h.name LIKE %s")
        values.append(q["hospital"].strip() + "%")
    if q.get("specialisation", "").strip():
        filters.append("s.name LIKE %s")
        values.append(q["specialisation"].strip() + "%")
    if q.get("city", "").strip():
        filters.append("a.city LIKE %s")
        values.append(q["city"].strip() + "%")
    where = ("WHERE " + " AND ".join(filters)) if filters else ""
    joins = """
        FROM doctor d
        LEFT JOIN doctor_address a ON a.doctor_id=d.doctor_id
        LEFT JOIN doctor_hospital dh ON dh.doctor_id=d.doctor_id
        LEFT JOIN hospitals h ON h.hospital_id=dh.hospital_id
        LEFT JOIN doctor_specialisation ds ON ds.doctor_id=d.doctor_id
        LEFT JOIN specialisations s ON s.specialisation_id=ds.specialisation_id
    """
    cur.execute("SELECT COUNT(DISTINCT d.doctor_id) AS total " + joins + " " + where, values)
    total = cur.fetchone()["total"]
    select_sql = """SELECT d.doctor_id, d.name, d.years_experience, d.photo_key,
        GROUP_CONCAT(DISTINCT s.name ORDER BY s.name SEPARATOR ', ') AS specialisations,
        GROUP_CONCAT(DISTINCT h.name ORDER BY h.name SEPARATOR ', ') AS hospitals,
        GROUP_CONCAT(DISTINCT a.city ORDER BY a.city SEPARATOR ', ') AS cities
        """ + joins + " " + where + " " + """GROUP BY d.doctor_id, d.name, d.years_experience, d.photo_key
        ORDER BY d.name ASC LIMIT %s OFFSET %s"""
    cur.execute(select_sql, values + [page_size, (page-1)*page_size])
    return {"doctors": cur.fetchall(), "pagination": {"page": page, "pageSize": page_size,
            "total": total, "totalPages": (total + page_size - 1)//page_size}}


def public_profile(cur, doctor_id):
    cur.execute("""SELECT d.doctor_id, d.name, d.phone, d.years_experience, d.photo_key,
        dl.licence_no, dl.authority, dl.verification_status
        FROM doctor d LEFT JOIN doctor_licence dl ON dl.doctor_id=d.doctor_id
        WHERE d.doctor_id=%s LIMIT 1""", (doctor_id,))
    doctor = cur.fetchone()
    if not doctor:
        return None
    cur.execute("""SELECT a.address_line_1, a.address_line_2, a.city, a.postal_code
        FROM doctor_address a WHERE a.doctor_id=%s ORDER BY a.id""", (doctor_id,))
    doctor["addresses"] = cur.fetchall()
    cur.execute("""SELECT DISTINCT h.name AS hospital, dep.name AS department
        FROM doctor_hospital dh JOIN hospitals h ON h.hospital_id=dh.hospital_id
        JOIN department dep ON dep.department_id=dh.department_id
        WHERE dh.doctor_id=%s ORDER BY h.name""", (doctor_id,))
    doctor["hospitalAffiliations"] = cur.fetchall()
    cur.execute("""SELECT DISTINCT s.name FROM doctor_specialisation ds
        JOIN specialisations s ON s.specialisation_id=ds.specialisation_id
        WHERE ds.doctor_id=%s ORDER BY s.name""", (doctor_id,))
    doctor["specialisations"] = [r["name"] for r in cur.fetchall()]
    cur.execute("""SELECT DISTINCT l.name FROM doctor_language dl
        JOIN language l ON l.language_id=dl.language_id
        WHERE dl.doctor_id=%s ORDER BY l.name""", (doctor_id,))
    doctor["languages"] = [r["name"] for r in cur.fetchall()]
    cur.execute("""SELECT degree, institution, year FROM doctor_qualification
        WHERE doctor_id=%s ORDER BY year DESC, degree""", (doctor_id,))
    doctor["qualifications"] = cur.fetchall()
    cur.execute("""SELECT ct.id AS consultationTypeId, ct.name, ct.duration_min AS durationMin,
        dct.fee, dct.capacity FROM doctor_consultation_type dct
        JOIN consultation_type ct ON ct.id=dct.consultation_type_id
        WHERE dct.doctor_id=%s AND dct.active=1 ORDER BY ct.duration_min""", (doctor_id,))
    doctor["consultationTypes"] = cur.fetchall()
    return doctor


def slot_availability(cur, doctor_id, q):
    start, end = q.get("from"), q.get("to")
    if not start or not end:
        raise ValueError("Both from and to dates are required in YYYY-MM-DD format")
    try:
        start_date, end_date = date.fromisoformat(start), date.fromisoformat(end)
    except ValueError:
        raise ValueError("from and to must use YYYY-MM-DD")
    if end_date < start_date:
        raise ValueError("to must be on or after from")
    if (end_date - start_date).days > 30:
        raise ValueError("Date range cannot exceed 31 calendar days")
    sql = """SELECT slot_id AS slotId, doctor_id AS doctorId,
        consultation_type_id AS consultationTypeId, slot_date AS slotDate,
        start_time AS startTime, end_time AS endTime, capacity, booked_count AS bookedCount,
        (capacity-booked_count) AS freeSeats
        FROM appointment_slot WHERE doctor_id=%s AND slot_date BETWEEN %s AND %s
        AND (slot_date > CURRENT_DATE OR (slot_date = CURRENT_DATE AND start_time > CURRENT_TIME))
        AND status='AVAILABLE' AND booked_count < capacity"""
    args = [doctor_id, start_date, end_date]
    if q.get("consultationTypeId"):
        try:
            type_id = int(q["consultationTypeId"])
            if type_id <= 0: raise ValueError()
        except ValueError:
            raise ValueError("consultationTypeId must be a positive integer")
        sql += " AND consultation_type_id=%s"
        args.append(type_id)
    sql += " ORDER BY slot_date, start_time"
    cur.execute(sql, args)
    return cur.fetchall()


def lambda_handler(event, context):
    connection = None
    try:
        method = (event.get("httpMethod") or "GET").upper()
        path_params = event.get("pathParameters") or {}
        doctor_id = path_params.get("doctorId")
        q = query_params(event)
        connection = db_connection()
        with connection.cursor() as cur:
            if method != "GET":
                return response(405, {"message": "Method not allowed"})
            if not doctor_id:
                result = search_doctors(cur, q)
                return response(200, result)
            if (event.get("path") or "").rstrip("/").endswith("/slots"):
                slots = slot_availability(cur, doctor_id, q)
                return response(200, {"doctorId": doctor_id, "slots": slots, "total": len(slots)})
            profile = public_profile(cur, doctor_id)
            if not profile:
                return response(404, {"message": "Doctor not found"})
            return response(200, {"doctor": profile})
    except ValueError as exc:
        return response(400, {"message": str(exc)})
    except Exception:
        logger.exception("Doctor discovery request failed")
        return response(500, {"message": "Unable to retrieve doctor information"})
    finally:
        if connection:
            connection.close()
