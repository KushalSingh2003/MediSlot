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
s3 = boto3.client("s3")
PHOTO_BUCKET = os.environ["PHOTOS_BUCKET"]
PHOTO_URL_EXPIRY = 3600



def db_connection():
    names = [f"{SSM_PREFIX}/db/{key}" for key in ("endpoint", "port", "name", "secret-arn")]
    logger.debug("Fetching DB config from SSM: %s", names)
    result = ssm.get_parameters(Names=names, WithDecryption=True)
    params = {p["Name"].rsplit("/", 1)[-1]: p["Value"] for p in result["Parameters"]}
    if result.get("InvalidParameters") or len(params) != 4:
        logger.error("Database configuration incomplete. Invalid parameters: %s, found keys: %s",
                     result.get("InvalidParameters"), list(params.keys()))
        raise RuntimeError("Database configuration is incomplete")
    logger.debug("Fetching DB credentials from Secrets Manager")
    secret = json.loads(secrets.get_secret_value(SecretId=params["secret-arn"])["SecretString"])
    logger.info("Connecting to database host=%s port=%s name=%s",
                params["endpoint"], params["port"], params["name"])
    return pymysql.connect(host=params["endpoint"], port=int(params["port"]),
        user=secret.get("username", params.get("username")), password=secret["password"],
        database=params["name"], connect_timeout=5, read_timeout=10, write_timeout=10,
        cursorclass=pymysql.cursors.DictCursor, autocommit=True)


def response(code, body):
    logger.info("Returning response statusCode=%s", code)
    return {"statusCode": code, "headers": {"Content-Type": "application/json"},
            "body": json.dumps(body, default=str)}


def query_params(event):
    return event.get("queryStringParameters") or {}
def attach_photo_url(doctor):
    photo_key = doctor.pop("photo_key", None)

    if photo_key:
        logger.debug("Generating presigned photo URL doctor_id=%s key=%s",
                     doctor.get("doctor_id"), photo_key)
        doctor["photoUrl"] = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": PHOTO_BUCKET, "Key": photo_key},
            ExpiresIn=PHOTO_URL_EXPIRY,
        )
    else:
        logger.debug("No photo for doctor_id=%s", doctor.get("doctor_id"))
        doctor["photoUrl"] = None

    return doctor


def search_doctors(cur, q):
    try:
        page = max(1, int(q.get("page", 1)))
        page_size = min(50, max(1, int(q.get("pageSize", 10))))
    except (TypeError, ValueError):
        logger.warning("Invalid pagination params page=%r pageSize=%r",
                       q.get("page"), q.get("pageSize"))
        raise ValueError("page and pageSize must be positive integers")
        #filters stores SQL conditions.
        #values stores the actual values that will be passed into those conditions.
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
    logger.info("Searching doctors page=%s pageSize=%s filters=%s values=%s",
                page, page_size, filters, values)
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
    logger.info("Doctor search matched total=%s", total)
    select_sql = """SELECT d.doctor_id, d.name, d.years_experience, d.photo_key,
        GROUP_CONCAT(DISTINCT s.name ORDER BY s.name SEPARATOR ', ') AS specialisations,
        GROUP_CONCAT(DISTINCT h.name ORDER BY h.name SEPARATOR ', ') AS hospitals,
        GROUP_CONCAT(DISTINCT a.city ORDER BY a.city SEPARATOR ', ') AS cities
        """ + joins + " " + where + " " + """GROUP BY d.doctor_id, d.name, d.years_experience, d.photo_key
        ORDER BY d.name ASC LIMIT %s OFFSET %s"""
    cur.execute(select_sql, values + [page_size, (page-1)*page_size])
    doctors = cur.fetchall()
    logger.info("Fetched %s doctors for page=%s", len(doctors), page)

    for doctor in doctors:
        attach_photo_url(doctor)

    return {
        "doctors": doctors,
        "pagination": {
            "page": page,
            "pageSize": page_size,
            "total": total,
            "totalPages": (total + page_size - 1) // page_size,
        },
    }
    


def public_profile(cur, doctor_id):
    logger.info("Fetching public profile doctor_id=%s", doctor_id)
    cur.execute("""SELECT d.doctor_id, d.name, d.years_experience, d.photo_key,
        dl.authority, dl.verification_status
        FROM doctor d LEFT JOIN doctor_licence dl ON dl.doctor_id=d.doctor_id
        WHERE d.doctor_id=%s LIMIT 1""", (doctor_id,))
    doctor = cur.fetchone()
    if not doctor:
        logger.warning("Doctor not found doctor_id=%s", doctor_id)
        return None

    cur.execute("""SELECT a.city
        FROM doctor_address a WHERE a.doctor_id=%s ORDER BY a.id""", (doctor_id,))
    doctor["addresses"] = cur.fetchall()
    logger.debug("Loaded %s addresses doctor_id=%s", len(doctor["addresses"]), doctor_id)

    cur.execute("""SELECT DISTINCT h.name AS hospital, dep.name AS department
        FROM doctor_hospital dh JOIN hospitals h ON h.hospital_id=dh.hospital_id
        JOIN department dep ON dep.department_id=dh.department_id
        WHERE dh.doctor_id=%s ORDER BY h.name""", (doctor_id,))
    doctor["hospitalAffiliations"] = cur.fetchall()
    logger.debug("Loaded %s hospital affiliations doctor_id=%s",
                 len(doctor["hospitalAffiliations"]), doctor_id)

    cur.execute("""SELECT DISTINCT s.name FROM doctor_specialisation ds
        JOIN specialisations s ON s.specialisation_id=ds.specialisation_id
        WHERE ds.doctor_id=%s ORDER BY s.name""", (doctor_id,))
    doctor["specialisations"] = [r["name"] for r in cur.fetchall()]
    logger.debug("Loaded %s specialisations doctor_id=%s",
                 len(doctor["specialisations"]), doctor_id)

    cur.execute("""SELECT DISTINCT l.name FROM doctor_language dl
        JOIN language l ON l.language_id=dl.language_id
        WHERE dl.doctor_id=%s ORDER BY l.name""", (doctor_id,))
    doctor["languages"] = [r["name"] for r in cur.fetchall()]
    logger.debug("Loaded %s languages doctor_id=%s", len(doctor["languages"]), doctor_id)

    cur.execute("""SELECT degree, institution, year FROM doctor_qualification
        WHERE doctor_id=%s ORDER BY year DESC, degree""", (doctor_id,))
    doctor["qualifications"] = cur.fetchall()
    logger.debug("Loaded %s qualifications doctor_id=%s", len(doctor["qualifications"]), doctor_id)

    cur.execute("""SELECT ct.id AS consultationTypeId, ct.name, ct.duration_min AS durationMin,
        dct.fee, dct.capacity FROM doctor_consultation_type dct
        JOIN consultation_type ct ON ct.id=dct.consultation_type_id
        WHERE dct.doctor_id=%s AND dct.active=1 ORDER BY ct.duration_min""", (doctor_id,))
    doctor["consultationTypes"] = cur.fetchall()
    logger.debug("Loaded %s consultation types doctor_id=%s",
                 len(doctor["consultationTypes"]), doctor_id)
    attach_photo_url(doctor)

    logger.info("Public profile assembled doctor_id=%s", doctor_id)
    return doctor


def slot_availability(cur, doctor_id, q):
    start, end = q.get("from"), q.get("to")
    if not start or not end:
        logger.warning("Missing date range doctor_id=%s from=%r to=%r", doctor_id, start, end)
        raise ValueError("Both from and to dates are required in YYYY-MM-DD format")
    try:
        start_date, end_date = date.fromisoformat(start), date.fromisoformat(end)
    except ValueError:
        logger.warning("Invalid date format doctor_id=%s from=%r to=%r", doctor_id, start, end)
        raise ValueError("from and to must use YYYY-MM-DD")
    if end_date < start_date:
        logger.warning("End date before start date doctor_id=%s from=%s to=%s",
                       doctor_id, start_date, end_date)
        raise ValueError("to must be on or after from")
    if (end_date - start_date).days > 30:
        logger.warning("Date range too large doctor_id=%s from=%s to=%s days=%s",
                       doctor_id, start_date, end_date, (end_date - start_date).days)
        raise ValueError("Date range cannot exceed 31 calendar days")
    logger.info("Fetching slots doctor_id=%s from=%s to=%s consultationTypeId=%s",
                doctor_id, start_date, end_date, q.get("consultationTypeId"))
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
            logger.warning("Invalid consultationTypeId=%r doctor_id=%s",
                           q.get("consultationTypeId"), doctor_id)
            raise ValueError("consultationTypeId must be a positive integer")
        sql += " AND consultation_type_id=%s"
        args.append(type_id)
    sql += " ORDER BY slot_date, start_time"
    cur.execute(sql, args)
    slots = cur.fetchall()
    logger.info("Found %s available slots doctor_id=%s", len(slots), doctor_id)
    return slots


def lambda_handler(event, context):
    connection = None
    try:
        method = (event.get("httpMethod") or "GET").upper()
        path_params = event.get("pathParameters") or {}
        doctor_id = path_params.get("doctorId")
        q = query_params(event)
        logger.info("Request received method=%s path=%s doctor_id=%s query=%s",
                    method, event.get("path"), doctor_id, q)
        connection = db_connection()
        with connection.cursor() as cur:
            if method != "GET":
                logger.warning("Method not allowed method=%s", method)
                return response(405, {"message": "Method not allowed"})
            if not doctor_id:
                logger.info("Routing to doctor search")
                result = search_doctors(cur, q)
                return response(200, result)
            if (event.get("path") or "").rstrip("/").endswith("/slots"):
                logger.info("Routing to slot availability doctor_id=%s", doctor_id)
                slots = slot_availability(cur, doctor_id, q)
                return response(200, {"doctorId": doctor_id, "slots": slots, "total": len(slots)})
            logger.info("Routing to public profile doctor_id=%s", doctor_id)
            profile = public_profile(cur, doctor_id)
            if not profile:
                return response(404, {"message": "Doctor not found"})
            return response(200, {"doctor": profile})
    except ValueError as exc:
        logger.warning("Bad request: %s", exc)
        return response(400, {"message": str(exc)})
    except Exception:
        logger.exception("Doctor discovery request failed")
        return response(500, {"message": "Unable to retrieve doctor information"})
    finally:
        if connection:
            connection.close()
            logger.debug("Database connection closed")