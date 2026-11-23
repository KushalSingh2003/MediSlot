import json
import logging
import os
import re
from datetime import time as datetime_time

import boto3
import pymysql
from botocore.exceptions import BotoCoreError, ClientError
from pymysql import err as pymysql_err

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ssm = boto3.client("ssm")
secretsmanager = boto3.client("secretsmanager")

ENV = os.environ.get("ENV", "dev")
SSM_PREFIX = os.environ.get(
    "SSM_PREFIX",
    f"/medislot/{ENV}",
)

ADMIN_ROLE = "admin"
MAX_TEMPLATE_NAME_LENGTH = 255
TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d$")


def response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
        },
        "body": json.dumps(body, default=str),
    }


def get_parameter(name):
    parameter_name = f"{SSM_PREFIX}/db/{name}"

    logger.info("Reading SSM parameter %s", parameter_name)

    result = ssm.get_parameter(
        Name=parameter_name,
        WithDecryption=True,
    )

    value = result["Parameter"]["Value"]

    if not value:
        raise ValueError(f"SSM parameter {parameter_name} is empty")

    return value


def get_db_connection():
    logger.info("Opening RDS database connection")

    secret_arn = get_parameter("secret-arn")

    secret_response = secretsmanager.get_secret_value(
        SecretId=secret_arn,
    )

    secret_string = secret_response.get("SecretString")

    if not secret_string:
        raise ValueError("Database secret is empty")

    secret = json.loads(secret_string)

    if not isinstance(secret.get("password"), str):
        raise ValueError("Database secret does not contain a password")

    return pymysql.connect(
        host=get_parameter("endpoint"),
        port=int(get_parameter("port")),
        user=get_parameter("username"),
        password=secret["password"],
        database=get_parameter("name"),
        connect_timeout=10,
        read_timeout=30,
        write_timeout=30,
        autocommit=False,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
    )


def get_http_method(event):
    if event.get("httpMethod"):
        return event["httpMethod"].upper()

    http_context = (
        event.get("requestContext", {})
        .get("http", {})
    )

    method = http_context.get("method")

    return method.upper() if method else None


def get_request_context(event):
    return event.get("requestContext") or {}


def get_authorizer_context(event):
    request_context = get_request_context(event)
    authorizer = request_context.get("authorizer") or {}

    return {
        "user_id": authorizer.get("userId"),
        "role": authorizer.get("role"),
    }


def require_admin(event):
    context = get_authorizer_context(event)

    if context["role"] != ADMIN_ROLE:
        logger.warning(
            "Forbidden template request: role=%s user_id=%s",
            context["role"],
            context["user_id"],
        )
        raise PermissionError("Administrator access is required")

    return context["user_id"]


def get_request_body(event):
    raw_body = event.get("body")

    if raw_body is None or raw_body.strip() == "":
        raise ValueError("Request body is required")

    try:
        body = json.loads(raw_body)
    except json.JSONDecodeError:
        raise ValueError("Request body must contain valid JSON")

    if not isinstance(body, dict):
        raise ValueError("Request body must be a JSON object")

    return body


def parse_time(value, field_name):
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")

    value = value.strip()

    if not TIME_PATTERN.fullmatch(value):
        raise ValueError(
            f"{field_name} must use the HH:MM:SS format"
        )

    hour, minute, second = (int(part) for part in value.split(":"))

    return datetime_time(hour, minute, second)


def serialize_time(value):
    if isinstance(value, datetime_time):
        return value.strftime("%H:%M:%S")

    if value is None:
        return None

    return str(value)


def validate_name(value):
    if not isinstance(value, str):
        raise ValueError("name must be a string")

    name = value.strip()

    if not name:
        raise ValueError("name must not be empty")

    if len(name) > MAX_TEMPLATE_NAME_LENGTH:
        raise ValueError(
            f"name must not exceed "
            f"{MAX_TEMPLATE_NAME_LENGTH} characters"
        )

    return name


def validate_template_times(start_time, end_time):
    if start_time >= end_time:
        raise ValueError(
            "working_end_time must be later than working_start_time"
        )


def serialize_template(template):
    if template is None:
        return None

    return {
        "template_id": template["template_id"],
        "name": template["name"],
        "is_active": bool(template["is_active"]),
        "working_start_time": serialize_time(
            template["working_start_time"]
        ),
        "working_end_time": serialize_time(
            template["working_end_time"]
        ),
    }


def get_template(cursor, template_id):
    cursor.execute(
        """
        SELECT
            template_id,
            name,
            is_active,
            working_start_time,
            working_end_time
        FROM slot_template
        WHERE template_id = %s
        """,
        (template_id,),
    )

    template = cursor.fetchone()

    if not template:
        raise LookupError("Slot template not found")

    return response(
        200,
        {"template": serialize_template(template)},
    )


def list_templates(cursor, include_inactive):
    if include_inactive:
        cursor.execute(
            """
            SELECT
                template_id,
                name,
                is_active,
                working_start_time,
                working_end_time
            FROM slot_template
            ORDER BY name, template_id
            """
        )
    else:
        cursor.execute(
            """
            SELECT
                template_id,
                name,
                is_active,
                working_start_time,
                working_end_time
            FROM slot_template
            WHERE is_active = TRUE
            ORDER BY name, template_id
            """
        )

    templates = cursor.fetchall()

    return response(
        200,
        {
            "templates": [
                serialize_template(template)
                for template in templates
            ],
            "count": len(templates),
        },
    )


def ensure_unique_name(cursor, name, excluding_template_id=None):
    if excluding_template_id is None:
        cursor.execute(
            """
            SELECT template_id
            FROM slot_template
            WHERE LOWER(name) = LOWER(%s)
            LIMIT 1
            """,
            (name,),
        )
    else:
        cursor.execute(
            """
            SELECT template_id
            FROM slot_template
            WHERE LOWER(name) = LOWER(%s)
              AND template_id != %s
            LIMIT 1
            """,
            (name, excluding_template_id),
        )

    existing = cursor.fetchone()

    if existing:
        raise ValueError(
            f"A slot template named '{name}' already exists"
        )


def create_template(cursor, body):
    allowed_fields = {
        "name",
        "working_start_time",
        "working_end_time",
        "is_active",
    }

    unknown_fields = set(body) - allowed_fields

    if unknown_fields:
        raise ValueError(
            "Unknown fields: "
            + ", ".join(sorted(unknown_fields))
        )

    required_fields = {
        "name",
        "working_start_time",
        "working_end_time",
    }

    missing_fields = required_fields - set(body)

    if missing_fields:
        raise ValueError(
            "Missing required fields: "
            + ", ".join(sorted(missing_fields))
        )

    name = validate_name(body["name"])

    start_time = parse_time(
        body["working_start_time"],
        "working_start_time",
    )

    end_time = parse_time(
        body["working_end_time"],
        "working_end_time",
    )

    validate_template_times(start_time, end_time)

    is_active = body.get("is_active", True)

    if not isinstance(is_active, bool):
        raise ValueError("is_active must be true or false")

    ensure_unique_name(cursor, name)

    cursor.execute(
        """
        INSERT INTO slot_template (
            name,
            is_active,
            working_start_time,
            working_end_time
        )
        VALUES (%s, %s, %s, %s)
        """,
        (
            name,
            is_active,
            start_time.strftime("%H:%M:%S"),
            end_time.strftime("%H:%M:%S"),
        ),
    )

    template_id = cursor.lastrowid

    cursor.execute(
        """
        SELECT
            template_id,
            name,
            is_active,
            working_start_time,
            working_end_time
        FROM slot_template
        WHERE template_id = %s
        """,
        (template_id,),
    )

    template = cursor.fetchone()

    return response(
        201,
        {
            "message": "Slot template created successfully",
            "template": serialize_template(template),
        },
    )


def update_template(cursor, template_id, body):
    if not body:
        raise ValueError("Request body must contain at least one field")

    allowed_fields = {
        "name",
        "working_start_time",
        "working_end_time",
        "is_active",
    }

    unknown_fields = set(body) - allowed_fields

    if unknown_fields:
        raise ValueError(
            "Unknown fields: "
            + ", ".join(sorted(unknown_fields))
        )

    cursor.execute(
        """
        SELECT
            template_id,
            name,
            is_active,
            working_start_time,
            working_end_time
        FROM slot_template
        WHERE template_id = %s
        FOR UPDATE
        """,
        (template_id,),
    )

    existing = cursor.fetchone()

    if not existing:
        raise LookupError("Slot template not found")

    start_time = parse_time(
        serialize_time(existing["working_start_time"]),
        "working_start_time",
    )

    end_time = parse_time(
        serialize_time(existing["working_end_time"]),
        "working_end_time",
    )

    if "name" in body:
        name = validate_name(body["name"])
        ensure_unique_name(
            cursor,
            name,
            excluding_template_id=template_id,
        )
    else:
        name = existing["name"]

    if "working_start_time" in body:
        start_time = parse_time(
            body["working_start_time"],
            "working_start_time",
        )

    if "working_end_time" in body:
        end_time = parse_time(
            body["working_end_time"],
            "working_end_time",
        )

    validate_template_times(start_time, end_time)

    if "is_active" in body:
        is_active = body["is_active"]

        if not isinstance(is_active, bool):
            raise ValueError("is_active must be true or false")
    else:
        is_active = bool(existing["is_active"])

    cursor.execute(
        """
        UPDATE slot_template
        SET name = %s,
            is_active = %s,
            working_start_time = %s,
            working_end_time = %s
        WHERE template_id = %s
        """,
        (
            name,
            is_active,
            start_time.strftime("%H:%M:%S"),
            end_time.strftime("%H:%M:%S"),
            template_id,
        ),
    )

    cursor.execute(
        """
        SELECT
            template_id,
            name,
            is_active,
            working_start_time,
            working_end_time
        FROM slot_template
        WHERE template_id = %s
        """,
        (template_id,),
    )

    template = cursor.fetchone()

    return response(
        200,
        {
            "message": "Slot template updated successfully",
            "template": serialize_template(template),
        },
    )


def deactivate_template(cursor, template_id):
    cursor.execute(
        """
        UPDATE slot_template
        SET is_active = FALSE
        WHERE template_id = %s
        """,
        (template_id,),
    )

    if cursor.rowcount == 0:
        raise LookupError("Slot template not found")

    cursor.execute(
        """
        SELECT
            template_id,
            name,
            is_active,
            working_start_time,
            working_end_time
        FROM slot_template
        WHERE template_id = %s
        """,
        (template_id,),
    )

    template = cursor.fetchone()

    return response(
        200,
        {
            "message": "Slot template deactivated successfully",
            "template": serialize_template(template),
        },
    )


def parse_template_id(path_parameters):
    if not path_parameters:
        raise LookupError("templateId is required")

    raw_template_id = path_parameters.get("templateId")

    if not raw_template_id:
        raise LookupError("templateId is required")

    if not str(raw_template_id).isdigit():
        raise ValueError("templateId must be a positive integer")

    template_id = int(raw_template_id)

    if template_id <= 0:
        raise ValueError("templateId must be a positive integer")

    return template_id


def parse_include_inactive(event):
    query_parameters = event.get("queryStringParameters") or {}

    raw_value = query_parameters.get("includeInactive")

    if raw_value is None:
        return False

    if str(raw_value).lower() not in ("true", "false"):
        raise ValueError(
            "includeInactive must be true or false"
        )

    return str(raw_value).lower() == "true"


def lambda_handler(event, context):
    request_id = getattr(context, "aws_request_id", "unknown")

    connection = None

    try:
        logger.info(
            "Template request started: request_id=%s",
            request_id,
        )

        admin_user_id = require_admin(event)

        method = get_http_method(event)
        path_parameters = event.get("pathParameters") or {}

        logger.info(
            "Template request: request_id=%s method=%s "
            "admin_user_id=%s path_parameters=%s",
            request_id,
            method,
            admin_user_id,
            path_parameters,
        )

        if method not in ("GET", "POST", "PATCH", "DELETE"):
            return response(
                405,
                {"message": "Method not allowed"},
            )

        connection = get_db_connection()

        with connection.cursor() as cursor:
            if method == "GET":
                if "templateId" in path_parameters:
                    template_id = parse_template_id(path_parameters)
                    result = get_template(cursor, template_id)
                else:
                    include_inactive = parse_include_inactive(event)
                    result = list_templates(
                        cursor,
                        include_inactive,
                    )

            elif method == "POST":
                body = get_request_body(event)
                result = create_template(cursor, body)

            elif method == "PATCH":
                template_id = parse_template_id(path_parameters)
                body = get_request_body(event)
                result = update_template(
                    cursor,
                    template_id,
                    body,
                )

            elif method == "DELETE":
                template_id = parse_template_id(path_parameters)
                result = deactivate_template(
                    cursor,
                    template_id,
                )

        connection.commit()

        logger.info(
            "Template request completed: request_id=%s "
            "method=%s status_code=%s",
            request_id,
            method,
            result["statusCode"],
        )

        return result

    except PermissionError as exc:
        if connection:
            connection.rollback()

        logger.warning(
            "Forbidden template request: request_id=%s error=%s",
            request_id,
            exc,
        )

        return response(403, {"message": str(exc)})

    except LookupError as exc:
        if connection:
            connection.rollback()

        logger.info(
            "Template resource not found: request_id=%s error=%s",
            request_id,
            exc,
        )

        return response(404, {"message": str(exc)})

    except ValueError as exc:
        if connection:
            connection.rollback()

        logger.warning(
            "Template request validation failed: "
            "request_id=%s error=%s",
            request_id,
            exc,
        )

        return response(400, {"message": str(exc)})

    except pymysql_err.IntegrityError as exc:
        if connection:
            connection.rollback()

        logger.warning(
            "Template database constraint violated: "
            "request_id=%s error=%s",
            request_id,
            exc,
        )

        if getattr(exc, "args", None) and exc.args[0] == 1062:
            return response(
                409,
                {"message": "A slot template with that name already exists"},
            )

        return response(
            409,
            {"message": "The template conflicts with database constraints"},
        )

    except (pymysql.MySQLError, BotoCoreError, ClientError):
        if connection:
            connection.rollback()

        logger.exception(
            "Template infrastructure or database error: "
            "request_id=%s",
            request_id,
        )

        return response(
            500,
            {"message": "Unable to complete the template request"},
        )

    except Exception:
        if connection:
            connection.rollback()

        logger.exception(
            "Unexpected template request error: request_id=%s",
            request_id,
        )

        return response(
            500,
            {"message": "An unexpected error occurred"},
        )

    finally:
        if connection:
            connection.close()