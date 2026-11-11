
"""Patient self-service profile CRUD.
Patient identity is obtained exclusively from API Gateway authorizer context.
"""

import json
import logging
import os
import re
from datetime import date

import boto3
import pymysql


# --------------------------------------------------
# INITIALIZATION
# --------------------------------------------------

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ssm = boto3.client("ssm")
secrets = boto3.client("secretsmanager")

SSM_PREFIX = os.environ["SSM_PREFIX"].rstrip("/")

PHONE_PATTERN = re.compile(r"[+0-9() .-]{7,30}")

PROFILE_FIELDS = {
    "name": "name",
    "phone": "phone",
    "city": "city",
    "dateOfBirth": "date_of_birth",
    "gender": "gender",
    "emergencyContact": "emergency_contact"
}

PROFILE_COLUMNS = """
    patient_id, name, email, phone, city, date_of_birth,
    gender, emergency_contact, created_at, updated_at
"""


# --------------------------------------------------
# RESPONSE HELPER
# --------------------------------------------------

def response(status_code, message, data=None):

    body = {"message": message}

    if data is not None:
        body["profile"] = data

    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json"
        },
        "body": json.dumps(body, default=str)
    }


# --------------------------------------------------
# DATABASE CONNECTION
# --------------------------------------------------

def db_connection():

    logger.info("Fetching database configuration from SSM")

    parameter_names = [
        f"{SSM_PREFIX}/db/{key}"
        for key in ("endpoint", "port", "name", "secret-arn")
    ]

    result = ssm.get_parameters(
        Names=parameter_names,
        WithDecryption=True
    )

    params = {
        p["Name"].rsplit("/", 1)[-1]: p["Value"]
        for p in result["Parameters"]
    }

    if result.get("InvalidParameters") or len(params) != 4:
        raise RuntimeError("Incomplete database configuration in SSM")

    logger.info("Database configuration retrieved successfully")

    secret_response = secrets.get_secret_value(
        SecretId=params["secret-arn"]
    )

    credentials = json.loads(secret_response["SecretString"])

    logger.info("Connecting to RDS MySQL")

    connection = pymysql.connect(
        host=params["endpoint"],
        port=int(params["port"]),
        user=credentials["username"],
        password=credentials["password"],
        database=params["name"],
        connect_timeout=5,
        read_timeout=10,
        write_timeout=10,
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False
    )

    logger.info("RDS MySQL connection established")

    return connection


# --------------------------------------------------
# REQUEST BODY PARSER
# --------------------------------------------------

def parse_body(event):

    raw_body = event.get("body")

    if not raw_body:
        raise ValueError("Request body is required")

    if event.get("isBase64Encoded"):
        raise ValueError("Base64 request bodies are not supported")

    body = json.loads(raw_body) if isinstance(raw_body, str) else raw_body

    if not isinstance(body, dict):
        raise ValueError("Request body must be a JSON object")

    return body


# --------------------------------------------------
# INPUT VALIDATION
# --------------------------------------------------

def validate_profile(body):

    unknown_fields = set(body) - set(PROFILE_FIELDS)

    if unknown_fields:
        raise ValueError(
            f"Unsupported fields: {', '.join(sorted(unknown_fields))}"
        )

    if not body:
        raise ValueError("At least one field is required")

    updates = []
    values = []

    for api_field, db_field in PROFILE_FIELDS.items():

        if api_field not in body:
            continue

        value = body[api_field]

        if value is not None and not isinstance(value, str):
            raise ValueError(f"{api_field} must be a string or null")

        if isinstance(value, str):
            value = value.strip() or None

        # Name validation
        if api_field == "name":
            if not value or len(value) > 255:
                raise ValueError(
                    "name is required and must be at most 255 characters"
                )

        # Phone validation
        if api_field in ("phone", "emergencyContact") and value:
            if not PHONE_PATTERN.fullmatch(value):
                raise ValueError(f"Invalid {api_field}")

        # Date validation
        if api_field == "dateOfBirth" and value:
            try:
                date.fromisoformat(value)
            except ValueError:
                raise ValueError(
                    "dateOfBirth must use YYYY-MM-DD format"
                )

        # Gender validation
        if api_field == "gender" and value and len(value) > 20:
            raise ValueError("gender must be at most 20 characters")

        # City validation
        if api_field == "city" and value and len(value) > 100:
            raise ValueError("city must be at most 100 characters")

        updates.append(f"{db_field}=%s")
        values.append(value)

    logger.info(
        "Profile validation successful; fields received: %s",
        list(body.keys())
    )

    return updates, values


# --------------------------------------------------
# GET PATIENT PROFILE
# --------------------------------------------------

def get_patient_profile(connection, patient_id):

    logger.info("Fetching patient profile")

    with connection.cursor() as cursor:

        cursor.execute(
            f"""
            SELECT {PROFILE_COLUMNS}
            FROM patients
            WHERE patient_id = %s
            LIMIT 1
            """,
            (patient_id,)
        )

        profile = cursor.fetchone()

    if not profile:
        logger.warning("Patient profile not found")
        return response(404, "Patient profile not found")

    logger.info("Patient profile retrieved successfully")

    return response(
        200,
        "Patient profile retrieved successfully",
        profile
    )


# --------------------------------------------------
# UPDATE PATIENT PROFILE
# --------------------------------------------------

def update_patient_profile(connection, patient_id, event):

    logger.info("Patient profile update started")

    body = parse_body(event)

    updates, values = validate_profile(body)

    # Patient ID comes from the authorizer, never the request body.
    values.append(patient_id)

    sql = f"""
        UPDATE patients
        SET {', '.join(updates)}
        WHERE patient_id = %s
    """

    with connection.cursor() as cursor:

        logger.info("Executing patient profile UPDATE query")

        cursor.execute(sql, values)

        # rowcount can be zero when values are unchanged.
        if cursor.rowcount == 0:

            cursor.execute(
                "SELECT patient_id FROM patients WHERE patient_id = %s",
                (patient_id,)
            )

            if not cursor.fetchone():
                logger.warning("Patient profile not found during update")
                connection.rollback()
                return response(404, "Patient profile not found")

        # Retrieve updated profile
        cursor.execute(
            f"""
            SELECT {PROFILE_COLUMNS}
            FROM patients
            WHERE patient_id = %s
            """,
            (patient_id,)
        )

        profile = cursor.fetchone()

    connection.commit()

    logger.info("Patient profile updated successfully")

    return response(
        200,
        "Patient profile updated successfully",
        profile
    )


# --------------------------------------------------
# DELETE PATIENT PROFILE
# --------------------------------------------------

def delete_patient_profile(connection, patient_id):

    logger.info("Patient profile deletion started")

    with connection.cursor() as cursor:

        # Check whether appointment records exist.
        cursor.execute(
            """
            SELECT COUNT(*) AS total
            FROM appointments
            WHERE patient_id = %s
            """,
            (patient_id,)
        )

        appointment_count = cursor.fetchone()["total"]

        if appointment_count > 0:

            logger.warning(
                "Patient deletion rejected: appointment records exist"
            )

            return response(
                409,
                "Profile cannot be deleted while appointment records exist"
            )

        cursor.execute(
            "DELETE FROM patients WHERE patient_id = %s",
            (patient_id,)
        )

        if cursor.rowcount == 0:
            logger.warning("Patient profile not found during deletion")
            connection.rollback()
            return response(404, "Patient profile not found")

    connection.commit()

    logger.info("Patient profile deleted successfully")

    return response(
        200,
        "Patient profile deleted successfully"
    )


# --------------------------------------------------
# LAMBDA HANDLER
# --------------------------------------------------

def lambda_handler(event, context):

    connection = None

    request_id = getattr(context, "aws_request_id", "unknown")
    stage = "authentication"

    logger.info(
        "Patient profile request started | request_id=%s",
        request_id
    )

    try:

        # 1. Extract identity from API Gateway authorizer.
        authorizer = (
            event.get("requestContext", {})
            .get("authorizer", {})
        )

        patient_id = authorizer.get("userId")
        role = authorizer.get("role")

        if not patient_id or not role:

            logger.warning(
                "Authentication context missing | request_id=%s",
                request_id
            )

            return response(401, "Unauthorized: authentication required")

        if str(role).lower() != "patient":

            logger.warning(
                "Access denied: patient role required | request_id=%s",
                request_id
            )

            return response(403, "Forbidden: patient access required")

        # 2. Identify HTTP method.
        method = (event.get("httpMethod") or "").upper()

        logger.info(
            "Authenticated request received | method=%s | request_id=%s",
            method,
            request_id
        )

        # 3. Reject unsupported methods before connecting to DB.
        if method not in ("GET", "PUT", "DELETE"):

            logger.warning("Unsupported HTTP method: %s", method)

            return response(405, "Method not allowed")

        # 4. Connect to database.
        stage = "database_connection"
        connection = db_connection()

        # 5. Route request to appropriate operation.
        if method == "GET":

            stage = "get_profile"

            result = get_patient_profile(
                connection,
                patient_id
            )

        elif method == "PUT":

            stage = "update_profile"

            result = update_patient_profile(
                connection,
                patient_id,
                event
            )

        else:

            stage = "delete_profile"

            result = delete_patient_profile(
                connection,
                patient_id
            )

        logger.info(
            "Request completed | method=%s | status_code=%s | request_id=%s",
            method,
            result["statusCode"],
            request_id
        )

        return result

    except (ValueError, json.JSONDecodeError) as exc:

        if connection:
            connection.rollback()

        logger.warning(
            "Request validation failed | stage=%s | reason=%s | request_id=%s",
            stage,
            str(exc),
            request_id
        )

        return response(400, str(exc))

    except Exception:

        if connection:
            connection.rollback()

        logger.exception(
            "Patient profile operation failed | stage=%s | request_id=%s",
            stage,
            request_id
        )

        error_messages = {
            "database_connection": "Database connection failed",
            "get_profile": "Unable to retrieve patient profile",
            "update_profile": "Unable to update patient profile",
            "delete_profile": "Unable to delete patient profile"
        }

        return response(
            500,
            error_messages.get(stage, "Internal server error")
        )

    finally:

        if connection:
            connection.close()
            logger.info(
                "Database connection closed | request_id=%s",
                request_id
            )
