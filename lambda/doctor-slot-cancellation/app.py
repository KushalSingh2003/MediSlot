
import os
import json
import logging
import boto3
import pymysql

from datetime import date, datetime, time
from decimal import Decimal
from pymysql.cursors import DictCursor
from botocore.exceptions import ClientError


# =========================================================
# LOGGING
# =========================================================

logger = logging.getLogger()
logger.setLevel(logging.INFO)


# =========================================================
# CONFIGURATION
# =========================================================

AWS_REGION = os.environ.get("AWS_REGION", "ap-south-1")
SSM_PREFIX = os.environ.get("SSM_PREFIX", "/medislot/dev")

DB_ENDPOINT_PARAM = f"{SSM_PREFIX}/db/endpoint"
DB_PORT_PARAM = f"{SSM_PREFIX}/db/port"
DB_USERNAME_PARAM = f"{SSM_PREFIX}/db/username"
DB_NAME_PARAM = f"{SSM_PREFIX}/db/name"
DB_SECRET_ARN_PARAM = f"{SSM_PREFIX}/db/secret-arn"

ssm = boto3.client("ssm", region_name=AWS_REGION)
secrets_manager = boto3.client(
    "secretsmanager",
    region_name=AWS_REGION
)


# =========================================================
# RESPONSE HELPER
# =========================================================

def json_serializer(value):

    if isinstance(value, (date, datetime, time)):
        return value.isoformat()

    if isinstance(value, Decimal):
        return float(value)

    return str(value)


def response(status_code, body):

    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
            "Access-Control-Allow-Methods": "DELETE,OPTIONS"
        },
        "body": json.dumps(body, default=json_serializer)
    }


# =========================================================
# AWS CONFIGURATION
# =========================================================

def get_ssm_parameter(parameter_name):

    result = ssm.get_parameter(
        Name=parameter_name,
        WithDecryption=True
    )

    return result["Parameter"]["Value"]


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
        password = secret_value

    return {
        "host": endpoint,
        "port": port,
        "user": username,
        "password": password,
        "database": database
    }


# =========================================================
# DATABASE CONNECTION
# =========================================================

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
        autocommit=False
    )


# =========================================================
# REFUND LEDGER
# =========================================================

def record_full_refund(cursor, appointment):
    """Record a 100% doctor-cancellation refund; this does not transfer money."""
    refund_amount = Decimal(str(appointment["amount"])).quantize(
        Decimal("0.01")
    )
    if refund_amount < 0:
        raise ValueError("Appointment amount cannot be negative.")

    reason = "Your appointment was cancelled by the doctor"
    cursor.execute(
        """
        INSERT INTO refund_ledger (
            appointment_id,
            patient_id,
            refund_percent,
            refund_amount,
            reason
        )
        VALUES (%s, %s, %s, %s, %s)
        """,
        (
            appointment["appointment_id"],
            appointment["patient_id"],
            100,
            refund_amount,
            reason,
        ),
    )

    return {
        "appointmentId": appointment["appointment_id"],
        "patientId": appointment["patient_id"],
        "refundPercent": 100,
        "refundAmount": str(refund_amount),
        "refundStatus": "RECORDED",
        "reason": reason,
    }


# =========================================================
# CANCEL SLOT
# =========================================================

def cancel_doctor_slot(doctor_id, slot_id):

    connection = None

    try:

        connection = get_db_connection()

        with connection.cursor() as cursor:

            # ---------------------------------------------
            # STEP 1: START TRANSACTION
            # ---------------------------------------------

            connection.begin()

            # ---------------------------------------------
            # STEP 2: LOCK SLOT
            # ---------------------------------------------

            cursor.execute(
                """
                SELECT
                    slot_id,
                    doctor_id,
                    slot_date,
                    start_time,
                    end_time,
                    status,
                    capacity,
                    booked_count
                FROM appointment_slot
                WHERE slot_id = %s
                  AND doctor_id = %s
                FOR UPDATE
                """,
                (slot_id, doctor_id)
            )

            slot = cursor.fetchone()

            if not slot:

                connection.rollback()

                return {
                    "success": False,
                    "statusCode": 404,
                    "message": "Slot not found for this doctor."
                }

            # ---------------------------------------------
            # STEP 3: CHECK SLOT STATUS
            # ---------------------------------------------

            if slot["status"] == "CLOSED":

                connection.rollback()

                return {
                    "success": False,
                    "statusCode": 409,
                    "message": "This slot is already closed."
                }

            # ---------------------------------------------
            # STEP 4: LOCK BOOKED APPOINTMENTS
            # ---------------------------------------------

            cursor.execute(
                """
                SELECT
                    appointment_id,
                    patient_id,
                    slot_id,
                    status,
                    amount
                FROM appointments
                WHERE slot_id = %s
                  AND status = 'BOOKED'
                FOR UPDATE
                """,
                (slot_id,)
            )

            booked_appointments = cursor.fetchall()

            # ---------------------------------------------
            # STEP 5: CLOSE THE SLOT
            # ---------------------------------------------

            cursor.execute(
                """
                UPDATE appointment_slot
                SET status = 'CLOSED'
                WHERE slot_id = %s
                  AND doctor_id = %s
                """,
                (slot_id, doctor_id)
            )

            # ---------------------------------------------
            # STEP 6: CANCEL BOOKED APPOINTMENTS
            # ---------------------------------------------

            cancelled_count = 0

            if booked_appointments:

                cursor.execute(
                    """
                    UPDATE appointments
                    SET status = 'CANCELLED'
                    WHERE slot_id = %s
                      AND status = 'BOOKED'
                    """,
                    (slot_id,)
                )

                cancelled_count = cursor.rowcount

            refund_results = []

            if cancelled_count != len(booked_appointments):
                raise RuntimeError(
                    "Not all booked appointments were cancelled."
                )

            for appointment in booked_appointments:
                refund_results.append(
                    record_full_refund(cursor, appointment)
                )

            # ---------------------------------------------
            # STEP 7: COMMIT TRANSACTION
            # ---------------------------------------------

            connection.commit()

            logger.info(
                "Slot cancellation committed. doctor_id=%s "
                "slot_id=%s cancelled_appointments=%s",
                doctor_id,
                slot_id,
                cancelled_count
            )

        refund_recorded = len(refund_results)

        # ---------------------------------------------
        # STEP 8: BUILD RESPONSE
        # ---------------------------------------------

        return {
            "success": True,
            "statusCode": 200,
            "message": (
                "Slot cancelled and full refund entries recorded. "
                "No payment transfer was made."
            ),
            "slot": {
                "slotId": slot["slot_id"],
                "date": slot["slot_date"],
                "startTime": slot["start_time"],
                "endTime": slot["end_time"],
                "status": "CLOSED"
            },
            "appointments": {
                "cancelled": cancelled_count
            },
            "refunds": {
                "total": len(booked_appointments),
                "recorded": refund_recorded,
                "moneyTransferred": False,
                "details": refund_results
            }
        }

    except Exception:

        if connection:
            connection.rollback()

        raise

    finally:

        if connection:
            connection.close()


# =========================================================
# LAMBDA HANDLER
# =========================================================

def lambda_handler(event, context):

    request_id = getattr(
        context,
        "aws_request_id",
        "unknown"
    )

    logger.info(
        "Doctor slot cancellation request received. request_id=%s",
        request_id
    )

    try:

        # ---------------------------------------------
        # STEP 1: HTTP METHOD VALIDATION
        # ---------------------------------------------

        http_method = event.get("httpMethod")

        if http_method == "OPTIONS":
            return response(200, {"message": "OK"})

        if http_method != "DELETE":

            return response(405, {
                "message": "Method not allowed."
            })

        # ---------------------------------------------
        # STEP 2: ROUTE VALIDATION
        # ---------------------------------------------

        resource = event.get("resource")

        if resource and resource != "/doctors/me/slots/{slotId}":

            return response(404, {
                "message": "Route not found."
            })

        # ---------------------------------------------
        # STEP 3: AUTHORIZATION
        # ---------------------------------------------

        authorizer = (
            event.get("requestContext", {})
            .get("authorizer", {})
        )

        doctor_id = authorizer.get("userId")
        role = authorizer.get("role")

        if not doctor_id:

            logger.warning(
                "Missing doctor identity in authorizer context. "
                "request_id=%s",
                request_id
            )

            return response(401, {
                "message": "Unauthorized."
            })

        if not role or str(role).lower() != "doctor":

            logger.warning(
                "Non-doctor attempted slot cancellation. "
                "request_id=%s",
                request_id
            )

            return response(403, {
                "message": "Only doctors can cancel their slots."
            })

        # ---------------------------------------------
        # STEP 4: SLOT ID VALIDATION
        # ---------------------------------------------

        path_parameters = event.get("pathParameters") or {}

        slot_id = path_parameters.get("slotId")

        if not slot_id:

            return response(400, {
                "message": "slotId is required."
            })

        try:

            slot_id = int(slot_id)

            if slot_id <= 0:
                raise ValueError()

        except (ValueError, TypeError):

            return response(400, {
                "message": "slotId must be a positive integer."
            })

        logger.info(
            "Processing slot cancellation. doctor_id=%s "
            "slot_id=%s request_id=%s",
            doctor_id,
            slot_id,
            request_id
        )

        # ---------------------------------------------
        # STEP 5: CANCEL SLOT
        # ---------------------------------------------

        result = cancel_doctor_slot(
            doctor_id,
            slot_id
        )

        status_code = result.pop("statusCode")

        return response(status_code, result)

    except pymysql.err.OperationalError:

        logger.exception(
            "Database operational error during slot cancellation. "
            "request_id=%s",
            request_id
        )

        return response(503, {
            "message": "Database temporarily unavailable."
        })

    except pymysql.MySQLError:

        logger.exception(
            "MySQL error during slot cancellation. request_id=%s",
            request_id
        )

        return response(500, {
            "message": "Failed to cancel the slot."
        })

    except ClientError:

        logger.exception(
            "AWS configuration error during slot cancellation. "
            "request_id=%s",
            request_id
        )

        return response(500, {
            "message": "Unable to initialize database connection."
        })

    except Exception:

        logger.exception(
            "Unexpected error during slot cancellation. "
            "request_id=%s",
            request_id
        )

        return response(500, {
            "message": "An unexpected error occurred."
        })