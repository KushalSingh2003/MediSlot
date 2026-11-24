import os
import json
import logging
import re
from decimal import Decimal, InvalidOperation

import boto3
import pymysql

from botocore.exceptions import ClientError
from pymysql.cursors import DictCursor
from pymysql.err import IntegrityError, OperationalError


# --------------------------------------------------
# Logging
# --------------------------------------------------

logger = logging.getLogger()
logger.setLevel(logging.INFO)


# --------------------------------------------------
# AWS clients
# --------------------------------------------------

ssm = boto3.client("ssm")
secrets_manager = boto3.client("secretsmanager")

ENVIRONMENT = os.environ["ENV"]
SSM_PREFIX = os.environ["SSM_PREFIX"]
# Cached configuration and database credentials.
_db_config = None


# --------------------------------------------------
# HTTP response
# --------------------------------------------------

def response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": (
                "Content-Type,Authorization"
            ),
            "Access-Control-Allow-Methods": (
                "GET,POST,PUT,DELETE,OPTIONS"
            ),
        },
        "body": json.dumps(body, default=str),
    }


# --------------------------------------------------
# Database configuration
# --------------------------------------------------

def get_parameter(name):
    result = ssm.get_parameter(Name=name)
    return result["Parameter"]["Value"]


def get_db_config():
    global _db_config

    if _db_config is not None:
        return _db_config

    prefix = SSM_PREFIX.rstrip("/")

    endpoint = get_parameter(f"{prefix}/db/endpoint")
    port = int(get_parameter(f"{prefix}/db/port"))
    username = get_parameter(f"{prefix}/db/username")
    database = get_parameter(f"{prefix}/db/name")
    secret_arn = get_parameter(f"{prefix}/db/secret-arn")

    secret_response = secrets_manager.get_secret_value(
        SecretId=secret_arn
    )

    secret_string = secret_response.get("SecretString")

    if not secret_string:
        raise ValueError("Database secret has no SecretString.")

    try:
        secret_value = json.loads(secret_string)
        password = (
            secret_value.get("password")
            if isinstance(secret_value, dict)
            else secret_string
        )
    except json.JSONDecodeError:
        password = secret_string

    if not password:
        raise ValueError("Database password is missing.")

    _db_config = {
        "host": endpoint,
        "port": port,
        "user": username,
        "password": password,
        "database": database,
        "charset": "utf8mb4",
        "cursorclass": DictCursor,
        "connect_timeout": 5,
        "read_timeout": 10,
        "write_timeout": 10,
        "autocommit": False,
    }

    return _db_config


def get_connection():
    return pymysql.connect(**get_db_config())


# --------------------------------------------------
# Validation helpers
# --------------------------------------------------

def parse_positive_id(value, field_name="consultationTypeId"):
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a positive integer.")

    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValueError(
            f"{field_name} must be a positive integer."
        )

    # Reject values such as 1.5 and "1.5".
    if str(value).strip() != str(parsed) or parsed <= 0:
        raise ValueError(
            f"{field_name} must be a positive integer."
        )

    return parsed


def parse_fee(value):
    if isinstance(value, bool) or value is None:
        raise ValueError("fee must be a non-negative number.")

    try:
        fee = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("fee must be a valid number.")

    if not fee.is_finite() or fee < 0:
        raise ValueError("fee must be a non-negative number.")

    if fee.as_tuple().exponent < -2:
        raise ValueError("fee must have at most 2 decimal places.")

    if fee > Decimal("99999999.99"):
        raise ValueError("fee exceeds the supported maximum.")

    return fee


def parse_capacity(value):
    if isinstance(value, bool):
        raise ValueError("capacity must be a positive integer.")

    if isinstance(value, int):
        capacity = value
    elif isinstance(value, str) and re.fullmatch(
        r"[0-9]+", value.strip()
    ):
        capacity = int(value.strip())
    else:
        raise ValueError("capacity must be a positive integer.")

    if capacity < 1 or capacity > 2147483647:
        raise ValueError(
            "capacity must be between 1 and 2147483647."
        )

    return capacity


def parse_active(value):
    if not isinstance(value, bool):
        raise ValueError("active must be true or false.")

    return value


def parse_body(event):
    body = event.get("body")

    if body is None or body == "":
        raise ValueError("Request body is required.")

    if event.get("isBase64Encoded"):
        import base64

        try:
            body = base64.b64decode(
                body, validate=True
            ).decode("utf-8")
        except Exception:
            raise ValueError("Request body is not valid Base64.")

    try:
        parsed = json.loads(body) if isinstance(body, str) else body
    except json.JSONDecodeError:
        raise ValueError("Request body must contain valid JSON.")

    if not isinstance(parsed, dict):
        raise ValueError("Request body must be a JSON object.")

    return parsed


# --------------------------------------------------
# Database operations
# --------------------------------------------------

def consultation_type_exists(cursor, consultation_type_id):
    cursor.execute(
        """
        SELECT id, name, duration_min
        FROM consultation_type
        WHERE id = %s
        """,
        (consultation_type_id,),
    )

    return cursor.fetchone()
def list_all_consultation_types(cursor):
    cursor.execute(
        """
        SELECT
            id AS consultationTypeId,
            name,
            duration_min AS durationMin
        FROM consultation_type
        ORDER BY id
        """
    )

    return cursor.fetchall()
def get_all_consultation_types(connection):
    with connection.cursor() as cursor:
        items = list_all_consultation_types(cursor)

    logger.info(
        "All consultation types retrieved: count=%s",
        len(items)
    )

    return response(200, {
        "message": "Consultation types retrieved successfully.",
        "count": len(items),
        "consultationTypes": items
    })


def list_consultation_types(cursor, doctor_id):
    cursor.execute(
        """
        SELECT
            ct.id AS consultationTypeId,
            ct.name AS name,
            ct.duration_min AS durationMin,
            dct.fee AS fee,
            dct.capacity AS capacity,
            dct.active AS active
        FROM doctor_consultation_type AS dct
        INNER JOIN consultation_type AS ct
            ON ct.id = dct.consultation_type_id
        WHERE dct.doctor_id = %s
        ORDER BY ct.id
        """,
        (doctor_id,),
    )

    rows = cursor.fetchall()

    for row in rows:
        row["active"] = bool(row["active"])

    return rows


# --------------------------------------------------
# POST: Add consultation type
# --------------------------------------------------

def create_consultation_type(connection, doctor_id, body):
    required = {"consultationTypeId", "fee", "capacity"}
    missing = required - body.keys()

    if missing:
        return response(400, {
            "message": "Missing required fields.",
            "missingFields": sorted(missing),
        })

    allowed = required | {"active"}
    unexpected = body.keys() - allowed

    if unexpected:
        return response(400, {
            "message": "Unknown fields in request.",
            "fields": sorted(unexpected),
        })

    consultation_type_id = parse_positive_id(
        body["consultationTypeId"]
    )
    fee = parse_fee(body["fee"])
    capacity = parse_capacity(body["capacity"])
    active = parse_active(body.get("active", True))

    with connection.cursor() as cursor:
        consultation_type = consultation_type_exists(
            cursor, consultation_type_id
        )

        if not consultation_type:
            return response(404, {
                "message": "Consultation type does not exist."
            })

        cursor.execute(
            """
            SELECT consultation_type_id
            FROM doctor_consultation_type
            WHERE doctor_id = %s
              AND consultation_type_id = %s
            """,
            (doctor_id, consultation_type_id),
        )

        if cursor.fetchone():
            return response(409, {
                "message": (
                    "This consultation type is already assigned "
                    "to your profile."
                )
            })

        cursor.execute(
            """
            INSERT INTO doctor_consultation_type
                (doctor_id, consultation_type_id,
                 fee, capacity, active)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (
                doctor_id,
                consultation_type_id,
                fee,
                capacity,
                active,
            ),
        )

    connection.commit()

    logger.info(
        "Consultation type assigned: doctor_id=%s type_id=%s",
        doctor_id,
        consultation_type_id,
    )

    return response(201, {
        "message": "Consultation type added successfully.",
        "consultationType": {
            "consultationTypeId": consultation_type_id,
            "name": consultation_type["name"],
            "durationMin": consultation_type["duration_min"],
            "fee": str(fee),
            "capacity": capacity,
            "active": active,
        },
    })


# --------------------------------------------------
# GET: List doctor's consultation types
# --------------------------------------------------

def get_consultation_types(connection, doctor_id):
    with connection.cursor() as cursor:
        items = list_consultation_types(cursor, doctor_id)

    logger.info(
        "Consultation types retrieved: doctor_id=%s count=%s",
        doctor_id,
        len(items),
    )

    return response(200, {
        "message": "Consultation types retrieved successfully.",
        "count": len(items),
        "consultationTypes": items,
    })


# --------------------------------------------------
# PUT: Update consultation type
# --------------------------------------------------

def update_consultation_type(connection, doctor_id, body):
    required = {
        "consultationTypeId",
        "fee",
        "capacity",
        "active",
    }

    missing = required - body.keys()

    if missing:
        return response(400, {
            "message": "Missing required fields.",
            "missingFields": sorted(missing),
        })

    unexpected = body.keys() - required

    if unexpected:
        return response(400, {
            "message": "Unknown fields in request.",
            "fields": sorted(unexpected),
        })

    consultation_type_id = parse_positive_id(
        body["consultationTypeId"]
    )
    fee = parse_fee(body["fee"])
    capacity = parse_capacity(body["capacity"])
    active = parse_active(body["active"])

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT consultation_type_id
            FROM doctor_consultation_type
            WHERE doctor_id = %s
              AND consultation_type_id = %s
            """,
            (doctor_id, consultation_type_id),
        )

        if not cursor.fetchone():
            return response(404, {
                "message": (
                    "This consultation type is not assigned "
                    "to your profile."
                )
            })

        cursor.execute(
            """
            UPDATE doctor_consultation_type
            SET fee = %s,
                capacity = %s,
                active = %s
            WHERE doctor_id = %s
              AND consultation_type_id = %s
            """,
            (
                fee,
                capacity,
                active,
                doctor_id,
                consultation_type_id,
            ),
        )

    connection.commit()

    logger.info(
        "Consultation type updated: doctor_id=%s type_id=%s",
        doctor_id,
        consultation_type_id,
    )

    return response(200, {
        "message": "Consultation type updated successfully.",
        "consultationType": {
            "consultationTypeId": consultation_type_id,
            "fee": str(fee),
            "capacity": capacity,
            "active": active,
        },
    })


# --------------------------------------------------
# DELETE: Remove consultation type from doctor
# --------------------------------------------------

def delete_consultation_type(connection, doctor_id, event):
    path_params = event.get("pathParameters") or {}

    raw_id = path_params.get("typeId")

    if raw_id is None:
        return response(400, {
            "message": "typeId path parameter is required."
        })

    consultation_type_id = parse_positive_id(raw_id)

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT consultation_type_id
            FROM doctor_consultation_type
            WHERE doctor_id = %s
              AND consultation_type_id = %s
            """,
            (doctor_id, consultation_type_id),
        )

        if not cursor.fetchone():
            return response(404, {
                "message": (
                    "This consultation type is not assigned "
                    "to your profile."
                )
            })

        cursor.execute(
            """
            DELETE FROM doctor_consultation_type
            WHERE doctor_id = %s
              AND consultation_type_id = %s
            """,
            (doctor_id, consultation_type_id),
        )

    connection.commit()

    logger.info(
        "Consultation type removed: doctor_id=%s type_id=%s",
        doctor_id,
        consultation_type_id,
    )

    return response(200, {
        "message": "Consultation type removed successfully.",
        "consultationTypeId": consultation_type_id,
    })


# --------------------------------------------------
# Lambda handler
# --------------------------------------------------

def lambda_handler(event, context):
    logger.info(
        "FULL API GATEWAY EVENT: %s",
        json.dumps(event, default=str)
    )
    request_id = getattr(context, "aws_request_id", "unknown")
    method = (
        event.get("httpMethod")
        or event.get("requestContext", {})
             .get("http", {})
             .get("method")
        or ""
    ).upper()

    logger.info(
        "Request started: request_id=%s method=%s",
        request_id,
        method,
    )

    if method == "OPTIONS":
        return response(204, {})

    connection = None

    try:
        # REST API Lambda Authorizer context.
        request_context = event.get("requestContext") or {}
        authorizer = request_context.get("authorizer") or {}

        doctor_id = authorizer.get("userId")
        role = authorizer.get("role")

        if not doctor_id:
            logger.warning(
                "Missing authenticated user: request_id=%s",
                request_id,
            )
            return response(401, {
                "message": "Unauthorized."
            })

        if not role or str(role).upper() != "DOCTOR":
            logger.warning(
                "Forbidden role: request_id=%s role=%s",
                request_id,
                role,
            )
            return response(403, {
                "message": "Only doctors can manage consultation types."
            })

        # Accept only the intended route, including API stage prefixes.
        resource = event.get("resource", "").rstrip("/")

        allowed_routes = {
            "/doctors/consultation-types",
            "/doctors/me/consultation-types",
            "/doctors/me/consultation-types/{typeId}"
        }

        if resource not in allowed_routes:
            return response(404, {
                "message": "Route not found."
            })

        if method not in {"GET", "POST", "PUT", "DELETE"}:
            return response(405, {
                "message": "Method not allowed."
            })

        # Open DB connection only after validating the request identity.
        connection = get_connection()

        if method == "GET":

            # Master consultation types
            if resource == "/doctors/consultation-types":
                return get_all_consultation_types(connection)

            # Doctor-specific consultation types
            if resource == "/doctors/me/consultation-types":
                return get_consultation_types(connection, doctor_id)

        if method == "POST":
            body = parse_body(event)
            return create_consultation_type(
                connection, doctor_id, body
            )

        if method == "PUT":
            body = parse_body(event)
            return update_consultation_type(
                connection, doctor_id, body
            )

        if method == "DELETE":
            return delete_consultation_type(
                connection, doctor_id, event
            )

    except ValueError as exc:
        logger.warning(
            "Validation failed: request_id=%s reason=%s",
            request_id,
            str(exc),
        )
        return response(400, {
            "message": str(exc)
        })

    except IntegrityError:
        if connection:
            connection.rollback()

        logger.exception(
            "Database integrity error: request_id=%s",
            request_id,
        )

        return response(409, {
            "message": (
                "The operation conflicts with existing database records."
            )
        })

    except OperationalError:
        if connection:
            connection.rollback()

        logger.exception(
            "Database operational error: request_id=%s",
            request_id,
        )

        return response(503, {
            "message": "Database temporarily unavailable."
        })

    except ClientError:
        if connection:
            connection.rollback()

        logger.exception(
            "AWS configuration or credentials error: request_id=%s",
            request_id,
        )

        return response(500, {
            "message": "Unable to load database configuration."
        })

    except Exception:
        if connection:
            connection.rollback()

        logger.exception(
            "Unexpected error: request_id=%s",
            request_id,
        )

        return response(500, {
            "message": "An internal error occurred."
        })

    finally:
        if connection:
            connection.close()

        logger.info(
            "Request finished: request_id=%s method=%s",
            request_id,
            method,
        )