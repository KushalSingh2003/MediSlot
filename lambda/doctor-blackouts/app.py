
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


# =========================================================
# 1. LOGGING AND CONFIGURATION
# =========================================================

logger = logging.getLogger()
logger.setLevel(logging.INFO)

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
# 2. RESPONSE HELPERS
# =========================================================

def json_serializer(value):
    if isinstance(value, date):
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
            "Access-Control-Allow-Methods": "GET,POST,DELETE,OPTIONS"
        },
        "body": json.dumps(body, default=json_serializer)
    }


# =========================================================
# 3. DATABASE CONFIGURATION
# =========================================================

def get_ssm_parameter(parameter_name):
    logger.info(
        "Retrieving database configuration parameter: %s",
        parameter_name
    )

    result = ssm.get_parameter(
        Name=parameter_name,
        WithDecryption=True
    )

    return result["Parameter"]["Value"]


def get_database_config():
    logger.info("Loading database configuration from AWS.")

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

    logger.info("Database configuration loaded successfully.")

    return {
        "host": endpoint,
        "port": port,
        "user": username,
        "password": password,
        "database": database
    }


def get_db_connection(autocommit=True):
    config = get_database_config()

    connection = pymysql.connect(
        host=config["host"],
        port=config["port"],
        user=config["user"],
        password=config["password"],
        database=config["database"],
        cursorclass=DictCursor,
        connect_timeout=10,
        read_timeout=20,
        write_timeout=20,
        autocommit=autocommit
    )

    logger.info("MySQL database connection established.")

    return connection


# =========================================================
# 4. VALIDATION
# =========================================================

def validate_blackout_date(value):
    if not isinstance(value, str):
        return None

    value = value.strip()

    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None

    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def get_doctor_id(event, request_id):
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

        return None, response(401, {
            "message": "Unauthorized."
        })

    if not role or str(role).lower() != "doctor":
        logger.warning(
            "Non-doctor attempted to access blackout API. "
            "role=%s request_id=%s",
            role,
            request_id
        )

        return None, response(403, {
            "message": "Only doctors can access this resource."
        })

    logger.info(
        "Doctor authorization successful. doctor_id=%s request_id=%s",
        user_id,
        request_id
    )

    return user_id, None


# =========================================================
# 5. GET BLACKOUT DATES
# =========================================================

def get_blackouts(doctor_id, request_id):
    connection = None

    logger.info(
        "Fetching blackout dates. doctor_id=%s request_id=%s",
        doctor_id,
        request_id
    )

    try:
        connection = get_db_connection()

        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    id AS blackoutId,
                    blackout_date AS blackoutDate
                FROM blackout
                WHERE doctor_id = %s
                ORDER BY blackout_date ASC
                """,
                (doctor_id,)
            )

            rows = cursor.fetchall()

        blackouts = [
            {
                "blackoutId": row["blackoutId"],
                "blackoutDate": row["blackoutDate"]
            }
            for row in rows
        ]

        logger.info(
            "Blackout dates fetched successfully. "
            "doctor_id=%s count=%d request_id=%s",
            doctor_id,
            len(blackouts),
            request_id
        )

        return response(200, {
            "message": "Doctor blackout dates fetched successfully.",
            "totalBlackouts": len(blackouts),
            "blackouts": blackouts
        })

    finally:
        if connection:
            connection.close()
            logger.info(
                "Database connection closed after fetching blackouts. "
                "request_id=%s",
                request_id
            )


# =========================================================
# 6. CREATE BLACKOUT AND CANCEL APPOINTMENTS
# =========================================================

def create_blackout(doctor_id, blackout_date, request_id):
    connection = None
    transaction_committed = False

    logger.info(
        "Starting blackout creation. doctor_id=%s date=%s request_id=%s",
        doctor_id,
        blackout_date,
        request_id
    )

    try:
        connection = get_db_connection(autocommit=False)

        logger.info(
            "Starting blackout database transaction. request_id=%s",
            request_id
        )

        with connection.cursor() as cursor:

            # -------------------------------------------------
            # STEP 1: INSERT BLACKOUT
            # -------------------------------------------------

            logger.info(
                "Creating blackout record. doctor_id=%s date=%s",
                doctor_id,
                blackout_date
            )

            cursor.execute(
                """
                INSERT INTO blackout (
                    doctor_id,
                    blackout_date
                )
                VALUES (%s, %s)
                """,
                (doctor_id, blackout_date)
            )

            blackout_id = cursor.lastrowid

            logger.info(
                "Blackout record inserted. blackout_id=%s request_id=%s",
                blackout_id,
                request_id
            )

            # -------------------------------------------------
            # STEP 2: LOCK ALL SLOTS FOR THAT DATE
            # -------------------------------------------------

            logger.info(
                "Locking doctor's slots. doctor_id=%s date=%s",
                doctor_id,
                blackout_date
            )

            cursor.execute(
                """
                SELECT
                    slot_id,
                    status
                FROM appointment_slot
                WHERE doctor_id = %s
                  AND slot_date = %s
                FOR UPDATE
                """,
                (doctor_id, blackout_date)
            )

            slots = cursor.fetchall()

            slot_ids = [
                row["slot_id"]
                for row in slots
            ]

            logger.info(
                "Slots retrieved and locked. doctor_id=%s "
                "date=%s total_slots=%d request_id=%s",
                doctor_id,
                blackout_date,
                len(slot_ids),
                request_id
            )

            cancelled_count = 0
            cancelled_appointment_ids = []
            closed_slot_count = 0

            # -------------------------------------------------
            # STEP 3: LOCK AND IDENTIFY BOOKED APPOINTMENTS
            # -------------------------------------------------

            if slot_ids:

                placeholders = ",".join(
                    ["%s"] * len(slot_ids)
                )

                logger.info(
                    "Finding booked appointments for blackout. "
                    "doctor_id=%s slot_count=%d request_id=%s",
                    doctor_id,
                    len(slot_ids),
                    request_id
                )

                cursor.execute(
                    f"""
                    SELECT
                        appointment_id,
                        slot_id
                    FROM appointments
                    WHERE slot_id IN ({placeholders})
                      AND status = 'BOOKED'
                    FOR UPDATE
                    """,
                    tuple(slot_ids)
                )

                booked_appointments = cursor.fetchall()

                cancelled_appointment_ids = [
                    row["appointment_id"]
                    for row in booked_appointments
                ]

                logger.info(
                    "Booked appointments identified. "
                    "doctor_id=%s booked_count=%d request_id=%s",
                    doctor_id,
                    len(booked_appointments),
                    request_id
                )

                # -------------------------------------------------
                # STEP 4: CANCEL BOOKED APPOINTMENTS
                # -------------------------------------------------

                if cancelled_appointment_ids:

                    logger.info(
                        "Cancelling booked appointments. "
                        "doctor_id=%s count=%d request_id=%s",
                        doctor_id,
                        len(cancelled_appointment_ids),
                        request_id
                    )

                    cursor.execute(
                        f"""
                        UPDATE appointments
                        SET status = 'CANCELLED'
                        WHERE appointment_id IN (
                            {",".join(["%s"] * len(cancelled_appointment_ids))}
                        )
                          AND status = 'BOOKED'
                        """,
                        tuple(cancelled_appointment_ids)
                    )

                    cancelled_count = cursor.rowcount

                    logger.info(
                        "Appointment cancellation completed. "
                        "doctor_id=%s cancelled_count=%d request_id=%s",
                        doctor_id,
                        cancelled_count,
                        request_id
                    )

                else:
                    logger.info(
                        "No booked appointments found for this date. "
                        "doctor_id=%s date=%s request_id=%s",
                        doctor_id,
                        blackout_date,
                        request_id
                    )

                # -------------------------------------------------
                # STEP 5: CLOSE ALL SLOTS
                # -------------------------------------------------

                logger.info(
                    "Closing slots for blackout date. "
                    "doctor_id=%s date=%s request_id=%s",
                    doctor_id,
                    blackout_date,
                    request_id
                )

                cursor.execute(
                    f"""
                    UPDATE appointment_slot
                    SET status = 'CLOSED'
                    WHERE slot_id IN ({placeholders})
                      AND status <> 'CLOSED'
                    """,
                    tuple(slot_ids)
                )

                closed_slot_count = cursor.rowcount

                logger.info(
                    "Slot closure completed. "
                    "doctor_id=%s closed_count=%d request_id=%s",
                    doctor_id,
                    closed_slot_count,
                    request_id
                )

            else:
                logger.info(
                    "No generated slots found for blackout date. "
                    "doctor_id=%s date=%s request_id=%s",
                    doctor_id,
                    blackout_date,
                    request_id
                )

        # -----------------------------------------------------
        # STEP 6: COMMIT TRANSACTION
        # -----------------------------------------------------

        logger.info(
            "Committing blackout transaction. "
            "doctor_id=%s blackout_id=%s request_id=%s",
            doctor_id,
            blackout_id,
            request_id
        )

        connection.commit()
        transaction_committed = True

        logger.info(
            "Blackout transaction committed successfully. "
            "doctor_id=%s blackout_id=%s date=%s "
            "cancelled_appointments=%d closed_slots=%d request_id=%s",
            doctor_id,
            blackout_id,
            blackout_date,
            cancelled_count,
            closed_slot_count,
            request_id
        )

        return response(201, {
            "success": True,
            "message": "Blackout created successfully. Associated appointments have been cancelled and slots have been closed.",
            "blackout": {
                "blackoutId": blackout_id,
                "blackoutDate": blackout_date
            },
            "slots": {
                "totalFound": len(slot_ids),
                "closed": closed_slot_count
            },
            "appointments": {
                "cancelled": cancelled_count,
                "appointmentIds": cancelled_appointment_ids
            },
            "refunds": {
                "status": "NOT_CONFIGURED",
                "message": "Refund processing has not been integrated."
            }
        })

    except Exception:
        if connection and not transaction_committed:
            logger.exception(
                "Blackout transaction failed. Rolling back. "
                "doctor_id=%s date=%s request_id=%s",
                doctor_id,
                blackout_date,
                request_id
            )

            connection.rollback()

            logger.warning(
                "Blackout transaction rolled back. "
                "doctor_id=%s date=%s request_id=%s",
                doctor_id,
                blackout_date,
                request_id
            )

        raise

    finally:
        if connection:
            connection.close()

            logger.info(
                "Database connection closed after blackout creation. "
                "request_id=%s",
                request_id
            )


# =========================================================
# 7. DELETE BLACKOUT
# =========================================================

def delete_blackout(doctor_id, blackout_id, request_id):
    connection = None

    logger.info(
        "Deleting blackout. doctor_id=%s blackout_id=%s request_id=%s",
        doctor_id,
        blackout_id,
        request_id
    )

    try:
        connection = get_db_connection()

        with connection.cursor() as cursor:
            cursor.execute(
                """
                DELETE FROM blackout
                WHERE id = %s
                  AND doctor_id = %s
                """,
                (blackout_id, doctor_id)
            )

            deleted_count = cursor.rowcount

        if deleted_count == 0:
            logger.warning(
                "Blackout not found for deletion. "
                "doctor_id=%s blackout_id=%s request_id=%s",
                doctor_id,
                blackout_id,
                request_id
            )

            return response(404, {
                "message": "Blackout date not found."
            })

        logger.info(
            "Blackout deleted successfully. "
            "doctor_id=%s blackout_id=%s request_id=%s",
            doctor_id,
            blackout_id,
            request_id
        )

        return response(200, {
            "success": True,
            "message": "Blackout date deleted successfully.",
            "blackoutId": blackout_id,
            "note": "Previously closed slots and cancelled appointments are not automatically restored."
        })

    finally:
        if connection:
            connection.close()

            logger.info(
                "Database connection closed after blackout deletion. "
                "request_id=%s",
                request_id
            )


# =========================================================
# 8. LAMBDA HANDLER
# =========================================================

def lambda_handler(event, context):
    request_id = getattr(
        context,
        "aws_request_id",
        "unknown"
    )

    logger.info(
        "Doctor blackout request received. request_id=%s",
        request_id
    )

    try:

        # -------------------------------------------------
        # HTTP METHOD VALIDATION
        # -------------------------------------------------

        http_method = event.get("httpMethod")

        logger.info(
            "Request method received. method=%s request_id=%s",
            http_method,
            request_id
        )

        if http_method == "OPTIONS":
            logger.info(
                "CORS preflight request received. request_id=%s",
                request_id
            )

            return response(200, {
                "message": "OK"
            })

        if http_method not in {"GET", "POST", "DELETE"}:
            logger.warning(
                "Unsupported HTTP method. method=%s request_id=%s",
                http_method,
                request_id
            )

            return response(405, {
                "message": "Method not allowed."
            })

        # -------------------------------------------------
        # ROUTE VALIDATION
        # -------------------------------------------------

        resource = event.get("resource")

        allowed_resources = {
            "/doctors/me/blackouts",
            "/doctors/me/blackouts/{blackoutId}"
        }

        if resource and resource not in allowed_resources:
            logger.warning(
                "Invalid resource requested. resource=%s request_id=%s",
                resource,
                request_id
            )

            return response(404, {
                "message": "Route not found."
            })

        # -------------------------------------------------
        # AUTHORIZATION
        # -------------------------------------------------

        doctor_id, auth_error = get_doctor_id(
            event,
            request_id
        )

        if auth_error:
            return auth_error

        # -------------------------------------------------
        # GET BLACKOUTS
        # -------------------------------------------------

        if http_method == "GET":

            if resource and resource != "/doctors/me/blackouts":
                return response(404, {
                    "message": "Route not found."
                })

            return get_blackouts(
                doctor_id,
                request_id
            )

        # -------------------------------------------------
        # CREATE BLACKOUT
        # -------------------------------------------------

        if http_method == "POST":

            if resource and resource != "/doctors/me/blackouts":
                return response(404, {
                    "message": "Route not found."
                })

            logger.info(
                "Processing create blackout request. "
                "doctor_id=%s request_id=%s",
                doctor_id,
                request_id
            )

            try:
                body = json.loads(event.get("body") or "{}")
            except (json.JSONDecodeError, TypeError):
                logger.warning(
                    "Invalid JSON body received. request_id=%s",
                    request_id
                )

                return response(400, {
                    "message": "Invalid JSON request body."
                })

            blackout_date = validate_blackout_date(
                body.get("blackoutDate")
            )

            if not blackout_date:
                logger.warning(
                    "Invalid blackout date received. "
                    "doctor_id=%s request_id=%s",
                    doctor_id,
                    request_id
                )

                return response(400, {
                    "message": "A valid blackoutDate is required in YYYY-MM-DD format."
                })

            logger.info(
                "Blackout date validated. doctor_id=%s date=%s request_id=%s",
                doctor_id,
                blackout_date,
                request_id
            )

            return create_blackout(
                doctor_id,
                blackout_date,
                request_id
            )

        # -------------------------------------------------
        # DELETE BLACKOUT
        # -------------------------------------------------

        if http_method == "DELETE":

            if resource and resource != "/doctors/me/blackouts/{blackoutId}":
                return response(404, {
                    "message": "Route not found."
                })

            path_parameters = event.get("pathParameters") or {}

            blackout_id_value = path_parameters.get("blackoutId")

            try:
                blackout_id = int(blackout_id_value)

                if blackout_id <= 0:
                    raise ValueError()

            except (TypeError, ValueError):
                logger.warning(
                    "Invalid blackout ID. value=%s request_id=%s",
                    blackout_id_value,
                    request_id
                )

                return response(400, {
                    "message": "A valid blackoutId is required."
                })

            return delete_blackout(
                doctor_id,
                blackout_id,
                request_id
            )

    except pymysql.err.IntegrityError as exc:

        if exc.args and exc.args[0] == 1062:
            logger.warning(
                "Duplicate blackout date rejected. request_id=%s",
                request_id
            )

            return response(409, {
                "message": "A blackout already exists for this date."
            })

        logger.exception(
            "Database integrity error. request_id=%s",
            request_id
        )

        return response(500, {
            "message": "Database integrity error."
        })

    except pymysql.err.OperationalError:
        logger.exception(
            "Database operational error. request_id=%s",
            request_id
        )

        return response(503, {
            "message": "Database temporarily unavailable."
        })

    except pymysql.MySQLError:
        logger.exception(
            "MySQL error while processing blackout. request_id=%s",
            request_id
        )

        return response(500, {
            "message": "Failed to process blackout request."
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
            "Unexpected blackout error. request_id=%s",
            request_id
        )

        return response(500, {
            "message": "An unexpected error occurred."
        })
