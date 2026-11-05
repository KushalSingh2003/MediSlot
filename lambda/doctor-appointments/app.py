import os
import json
import logging
import re
from datetime import date
from decimal import Decimal

import boto3
import pymysql
from pymysql.cursors import DictCursor
from botocore.exceptions import ClientError


# ---------------------------------------------------------
# LOGGING
# ---------------------------------------------------------

logger = logging.getLogger()
logger.setLevel(logging.INFO)


# ---------------------------------------------------------
# ENVIRONMENT CONFIGURATION
# ---------------------------------------------------------

AWS_REGION = os.environ.get("AWS_REGION", "ap-south-1")
SSM_PREFIX = os.environ.get("SSM_PREFIX", "/medislot/dev")

# These parameter paths must match your working Doctor Profile Lambda.
DB_ENDPOINT_PARAM = f"{SSM_PREFIX}/db/endpoint"
DB_PORT_PARAM = f"{SSM_PREFIX}/db/port"
DB_USERNAME_PARAM = f"{SSM_PREFIX}/db/username"
DB_NAME_PARAM = f"{SSM_PREFIX}/db/name"
DB_SECRET_ARN_PARAM = f"{SSM_PREFIX}/db/secret-arn"

ALLOWED_STATUSES = {
    "BOOKED",
    "CANCELLED",
    "COMPLETED",
    "NO_SHOW"
}

DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")

ssm = boto3.client("ssm", region_name=AWS_REGION)
secrets_manager = boto3.client("secretsmanager", region_name=AWS_REGION)


# ---------------------------------------------------------
# RESPONSE HELPER
# ---------------------------------------------------------

def response(status_code, body):

    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
            "Access-Control-Allow-Methods": "GET,OPTIONS"
        },
        "body": json.dumps(body, default=json_serializer)
    }


def json_serializer(value):

    if isinstance(value, (date,)):
        return value.isoformat()

    if isinstance(value, Decimal):
        return float(value)

    return str(value)


# ---------------------------------------------------------
# AWS PARAMETER HELPERS
# ---------------------------------------------------------

def get_ssm_parameter(parameter_name):

    try:
        result = ssm.get_parameter(
            Name=parameter_name,
            WithDecryption=True
        )

        return result["Parameter"]["Value"]

    except ClientError:
        logger.exception(
            "Failed to retrieve required SSM parameter: %s",
            parameter_name
        )
        raise


def get_database_config():

    endpoint = get_ssm_parameter(DB_ENDPOINT_PARAM)
    port = int(get_ssm_parameter(DB_PORT_PARAM))
    username = get_ssm_parameter(DB_USERNAME_PARAM)
    database = get_ssm_parameter(DB_NAME_PARAM)
    secret_arn = get_ssm_parameter(DB_SECRET_ARN_PARAM)

    secret_response = secrets_manager.get_secret_value(
        SecretId=secret_arn
    )

    secret_value = secret_response.get("SecretString")

    if not secret_value:
        raise ValueError("Database secret is empty.")

    try:
        secret_json = json.loads(secret_value)
        password = secret_json["password"]
    except (json.JSONDecodeError, KeyError):
        # Supports a plaintext password if that is how your secret is stored.
        password = secret_value

    return {
        "host": endpoint,
        "port": port,
        "user": username,
        "password": password,
        "database": database
    }


# ---------------------------------------------------------
# DATABASE CONNECTION
# ---------------------------------------------------------

def get_db_connection():

    config = get_database_config()

    return pymysql.connect(
        host=config["host"],
        port=config["port"],
        user=config["user"],
        password=config["password"],
        database=config["database"],
        cursorclass=DictCursor,
        connect_timeout=10,
        read_timeout=20,
        write_timeout=20,
        autocommit=True
    )


# ---------------------------------------------------------
# VALIDATION
# ---------------------------------------------------------

def validate_date(value, parameter_name):

    if value is None or value == "":
        return None, None

    if not DATE_PATTERN.fullmatch(value):
        return None, (
            f"Invalid '{parameter_name}' date format. "
            "Expected YYYY-MM-DD."
        )

    try:
        parsed_date = date.fromisoformat(value)
        return parsed_date, None

    except ValueError:
        return None, (
            f"Invalid '{parameter_name}' date. "
            "Please provide a valid calendar date."
        )


def validate_query_parameters(query_params):

    from_value = query_params.get("from")
    to_value = query_params.get("to")
    status_value = query_params.get("status")

    from_date, error = validate_date(from_value, "from")

    if error:
        return None, error

    to_date, error = validate_date(to_value, "to")

    if error:
        return None, error

    if from_date and to_date and from_date > to_date:
        return None, "'from' date cannot be later than 'to' date."

    status = None

    if status_value is not None and status_value.strip():

        status = status_value.strip().upper()

        if status not in ALLOWED_STATUSES:
            return None, (
                "Invalid appointment status. Allowed values: "
                + ", ".join(sorted(ALLOWED_STATUSES))
            )

    return {
        "from_date": from_date,
        "to_date": to_date,
        "status": status
    }, None


# ---------------------------------------------------------
# FETCH DOCTOR APPOINTMENTS
# ---------------------------------------------------------

def get_doctor_appointments(doctor_id, filters):

    conditions = [
        "aps.doctor_id = %s"
    ]

    parameters = [doctor_id]

    if filters["from_date"]:
        conditions.append("aps.slot_date >= %s")
        parameters.append(filters["from_date"])

    if filters["to_date"]:
        conditions.append("aps.slot_date <= %s")
        parameters.append(filters["to_date"])

    if filters["status"]:
        conditions.append("a.status = %s")
        parameters.append(filters["status"])

    where_clause = " AND ".join(conditions)

    query = f"""
        SELECT
            a.appointment_id,
            a.patient_id,
            a.status AS appointment_status,

            aps.slot_id,
            aps.slot_date,
            aps.start_time,
            aps.end_time,

            ct.id AS consultation_type_id,
            ct.name AS consultation_type,
            ct.duration_min,

            p.name AS patient_name,
            p.email AS patient_email,
            p.phone AS patient_phone

        FROM appointments a

        INNER JOIN appointment_slot aps
            ON aps.slot_id = a.slot_id

        INNER JOIN consultation_type ct
            ON ct.id = aps.consultation_type_id

        LEFT JOIN patients p
            ON p.patient_id = a.patient_id

        WHERE {where_clause}

        ORDER BY
            aps.slot_date DESC,
            aps.start_time DESC,
            a.appointment_id DESC
    """

    connection = None

    try:

        connection = get_db_connection()

        with connection.cursor() as cursor:

            cursor.execute(query, parameters)

            rows = cursor.fetchall()

        appointments = []

        for row in rows:

            appointments.append({
                "appointmentId": row["appointment_id"],
                "patient": {
                    "patientId": row["patient_id"],
                    "name": row["patient_name"],
                    "email": row["patient_email"],
                    "phone": row["patient_phone"]
                },
                "consultation": {
                    "consultationTypeId": row["consultation_type_id"],
                    "name": row["consultation_type"],
                    "durationMinutes": row["duration_min"]
                },
                "slot": {
                    "slotId": row["slot_id"],
                    "date": row["slot_date"],
                    "startTime": row["start_time"],
                    "endTime": row["end_time"]
                },
                "status": row["appointment_status"]
            })

        logger.info(
            "Doctor appointments fetched successfully. "
            "doctor_id=%s result_count=%d",
            doctor_id,
            len(appointments)
        )

        return appointments

    finally:

        if connection:
            connection.close()


# ---------------------------------------------------------
# LAMBDA HANDLER
# ---------------------------------------------------------

def lambda_handler(event, context):

    request_id = getattr(context, "aws_request_id", "unknown")

    logger.info(
        "Doctor appointments request received. request_id=%s",
        request_id
    )

    try:

        # ---------------------------------------------
        # HTTP METHOD VALIDATION
        # ---------------------------------------------

        http_method = event.get("httpMethod")

        if http_method == "OPTIONS":
            return response(200, {"message": "OK"})

        if http_method != "GET":

            return response(405, {
                "message": "Method not allowed."
            })

        # ---------------------------------------------
        # ROUTE VALIDATION
        # ---------------------------------------------

        resource = event.get("resource")

        if resource and resource != "/doctors/me/appointments":

            return response(404, {
                "message": "Route not found."
            })

        # ---------------------------------------------
        # AUTHORIZATION VALIDATION
        # ---------------------------------------------

        authorizer = (
            event.get("requestContext", {})
            .get("authorizer", {})
        )

        user_id = authorizer.get("userId")
        role = authorizer.get("role")

        if not user_id:

            logger.warning(
                "Missing userId in authorizer context. request_id=%s",
                request_id
            )

            return response(401, {
                "message": "Unauthorized."
            })

        if not role or str(role).lower() != "doctor":

            logger.warning(
                "Unauthorized role accessing doctor appointments. "
                "request_id=%s",
                request_id
            )

            return response(403, {
                "message": "Only doctors can access this resource."
            })

        doctor_id = user_id

        # ---------------------------------------------
        # QUERY PARAMETER VALIDATION
        # ---------------------------------------------

        query_params = event.get("queryStringParameters") or {}

        filters, validation_error = validate_query_parameters(
            query_params
        )

        if validation_error:

            logger.warning(
                "Invalid appointment query parameters. "
                "doctor_id=%s error=%s request_id=%s",
                doctor_id,
                validation_error,
                request_id
            )

            return response(400, {
                "message": validation_error
            })

        logger.info(
            "Fetching doctor appointments. "
            "doctor_id=%s from=%s to=%s status=%s request_id=%s",
            doctor_id,
            filters["from_date"],
            filters["to_date"],
            filters["status"],
            request_id
        )

        # ---------------------------------------------
        # DATABASE QUERY
        # ---------------------------------------------

        appointments = get_doctor_appointments(
            doctor_id,
            filters
        )

        return response(200, {
            "message": "Doctor appointments fetched successfully.",
            "filters": {
                "from": filters["from_date"],
                "to": filters["to_date"],
                "status": filters["status"]
            },
            "totalAppointments": len(appointments),
            "appointments": appointments
        })

    except pymysql.err.OperationalError:

        logger.exception(
            "Database operational error while fetching doctor "
            "appointments. request_id=%s",
            request_id
        )

        return response(503, {
            "message": "Database temporarily unavailable."
        })

    except pymysql.MySQLError:

        logger.exception(
            "MySQL error while fetching doctor appointments. "
            "request_id=%s",
            request_id
        )

        return response(500, {
            "message": "Failed to fetch doctor appointments."
        })

    except (ClientError, ValueError):

        logger.exception(
            "AWS configuration or secret retrieval error. "
            "request_id=%s",
            request_id
        )

        return response(500, {
            "message": "Unable to initialize database connection."
        })

    except Exception:

        logger.exception(
            "Unexpected error fetching doctor appointments. "
            "request_id=%s",
            request_id
        )

        return response(500, {
            "message": "An unexpected error occurred."
        })