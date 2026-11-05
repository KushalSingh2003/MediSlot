import os
import json
import logging
import boto3
import pymysql

from datetime import datetime
from pymysql.cursors import DictCursor
from pymysql.err import OperationalError
from botocore.exceptions import ClientError


# ==========================================================
# CONFIGURATION
# ==========================================================

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ENV = os.environ["ENV"]
SSM_PREFIX = os.environ["SSM_PREFIX"]

ssm = boto3.client("ssm")
secretsmanager = boto3.client("secretsmanager")


# ==========================================================
# RESPONSE HELPER
# ==========================================================

def response(status_code, body):

    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
            "Access-Control-Allow-Methods": "GET,OPTIONS"
        },
        "body": json.dumps(body, default=str)
    }


# ==========================================================
# DATABASE CONFIGURATION
# ==========================================================

def get_parameter(name):

    result = ssm.get_parameter(
        Name=f"{SSM_PREFIX}/db/{name}",
        WithDecryption=True
    )

    return result["Parameter"]["Value"]


def get_db_config():

    endpoint = get_parameter("endpoint")
    port = int(get_parameter("port"))
    username = get_parameter("username")
    database = get_parameter("name")
    secret_arn = get_parameter("secret-arn")

    secret_response = secretsmanager.get_secret_value(
        SecretId=secret_arn
    )

    secret_value = secret_response["SecretString"]

    try:
        password = json.loads(secret_value)["password"]
    except (json.JSONDecodeError, KeyError):
        password = secret_value

    return {
        "host": endpoint,
        "port": port,
        "user": username,
        "password": password,
        "database": database,
        "charset": "utf8mb4",
        "cursorclass": DictCursor,
        "connect_timeout": 10,
        "read_timeout": 30,
        "write_timeout": 30,
        "autocommit": True
    }


def get_db_connection():

    return pymysql.connect(**get_db_config())


# ==========================================================
# DATE VALIDATION
# ==========================================================

def validate_date(date_string):

    if not date_string:
        raise ValueError("date query parameter is required.")

    try:

        parsed_date = datetime.strptime(
            date_string,
            "%Y-%m-%d"
        ).date()

        if parsed_date.isoformat() != date_string:
            raise ValueError()

        return parsed_date

    except (ValueError, TypeError):

        raise ValueError(
            "Invalid date. Expected format: YYYY-MM-DD."
        )


# ==========================================================
# FETCH DOCTOR DAILY SCHEDULE
# ==========================================================

def get_doctor_schedule(doctor_id, schedule_date):

    connection = None

    try:

        connection = get_db_connection()

        with connection.cursor() as cursor:

            cursor.execute(
                """
                SELECT
                    aps.slot_id,
                    aps.doctor_id,
                    aps.consultation_type_id,
                    ct.name AS consultation_type,
                    ct.duration_min,
                    aps.slot_date,
                    aps.start_time,
                    aps.end_time,
                    aps.status AS slot_status,
                    aps.capacity,
                    aps.booked_count,

                    a.appointment_id,
                    a.patient_id,
                    a.status AS appointment_status,

                    p.name AS patient_name,
                    p.email AS patient_email,
                    p.phone AS patient_phone

                FROM appointment_slot aps

                INNER JOIN consultation_type ct
                    ON ct.id = aps.consultation_type_id

                LEFT JOIN appointments a
                    ON a.slot_id = aps.slot_id
                    AND a.status = 'BOOKED'

                LEFT JOIN patients p
                    ON p.patient_id = a.patient_id

                WHERE aps.doctor_id = %s
                  AND aps.slot_date = %s

                ORDER BY
                    aps.start_time,
                    aps.slot_id,
                    a.appointment_id
                """,
                (doctor_id, schedule_date)
            )

            rows = cursor.fetchall()

        # Group appointment records by slot.
        schedule = {}

        for row in rows:

            slot_id = row["slot_id"]

            if slot_id not in schedule:

                schedule[slot_id] = {
                    "slotId": slot_id,
                    "consultationTypeId":
                        row["consultation_type_id"],
                    "consultationType":
                        row["consultation_type"],
                    "durationMinutes":
                        row["duration_min"],
                    "startTime":
                        str(row["start_time"]),
                    "endTime":
                        str(row["end_time"]),
                    "status":
                        row["slot_status"],
                    "capacity":
                        row["capacity"],
                    "bookedCount":
                        row["booked_count"],
                    "patients": []
                }

            # Add patient only when a booked appointment exists.
            if row["appointment_id"] is not None:

                schedule[slot_id]["patients"].append({
                    "appointmentId":
                        row["appointment_id"],
                    "patientId":
                        row["patient_id"],
                    "name":
                        row["patient_name"],
                    "email":
                        row["patient_email"],
                    "phone":
                        row["patient_phone"],
                    "appointmentStatus":
                        row["appointment_status"]
                })

        schedule_list = list(schedule.values())

        # Calculate daily summary.
        total_slots = len(schedule_list)

        available_slots = sum(
            1 for slot in schedule_list
            if slot["status"] == "AVAILABLE"
        )

        booked_slots = sum(
            1 for slot in schedule_list
            if slot["status"] == "BOOKED"
        )

        closed_slots = sum(
            1 for slot in schedule_list
            if slot["status"] == "CLOSED"
        )

        return {
            "date": str(schedule_date),
            "doctorId": doctor_id,
            "summary": {
                "totalSlots": total_slots,
                "availableSlots": available_slots,
                "bookedSlots": booked_slots,
                "closedSlots": closed_slots
            },
            "schedule": schedule_list
        }

    finally:

        if connection:
            connection.close()


# ==========================================================
# LAMBDA HANDLER
# ==========================================================

def lambda_handler(event, context):

    request_id = context.aws_request_id

    logger.info(
        "Doctor schedule request started. request_id=%s",
        request_id
    )

    try:

        # ------------------------------------------
        # Handle CORS
        # ------------------------------------------

        method = event.get("httpMethod", "")

        if method == "OPTIONS":

            return response(
                200,
                {"message": "OK"}
            )

        if method != "GET":

            return response(
                405,
                {"message": "Method not allowed."}
            )

        # ------------------------------------------
        # Validate route
        # ------------------------------------------

        resource = event.get("resource", "")

        if resource != "/doctors/me/schedule":

            return response(
                404,
                {"message": "Route not found."}
            )

        # ------------------------------------------
        # Validate authorizer
        # ------------------------------------------

        authorizer = (
            event.get("requestContext", {})
            .get("authorizer", {})
        )

        doctor_id = authorizer.get("userId")
        role = authorizer.get("role")

        if not doctor_id:

            return response(
                401,
                {"message": "Unauthorized."}
            )

        if not role or str(role).lower() != "doctor":

            return response(
                403,
                {"message": "Only doctors can access this schedule."}
            )

        # ------------------------------------------
        # Validate query parameter
        # ------------------------------------------

        query_params = event.get("queryStringParameters") or {}

        schedule_date = validate_date(
            query_params.get("date")
        )

        logger.info(
            "Fetching schedule for doctor_id=%s date=%s",
            doctor_id,
            schedule_date
        )

        # ------------------------------------------
        # Fetch schedule
        # ------------------------------------------

        result = get_doctor_schedule(
            doctor_id,
            schedule_date
        )

        logger.info(
            "Schedule fetched successfully. doctor_id=%s slots=%s",
            doctor_id,
            result["summary"]["totalSlots"]
        )

        return response(
            200,
            result
        )

    except ValueError as error:

        return response(
            400,
            {"message": str(error)}
        )

    except OperationalError:

        logger.exception(
            "Database connection or operation failed."
        )

        return response(
            503,
            {"message": "Database temporarily unavailable."}
        )

    except ClientError:

        logger.exception(
            "AWS service error."
        )

        return response(
            500,
            {"message": "Unable to retrieve database configuration."}
        )

    except Exception:

        logger.exception(
            "Unexpected error fetching doctor schedule."
        )

        return response(
            500,
            {"message": "Internal server error."}
        )