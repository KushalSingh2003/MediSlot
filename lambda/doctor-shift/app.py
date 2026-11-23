import json
import logging
import os
from datetime import date, timedelta

import boto3
import pymysql
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ssm = boto3.client("ssm")
secretsmanager = boto3.client("secretsmanager")

ENV = os.environ.get("ENV", "dev")
SSM_PREFIX = os.environ.get("SSM_PREFIX", f"/medislot/{ENV}")

WORKING_DAYS = (1, 2, 3, 4, 5)
DAY_NAMES = {
    1: "Monday",
    2: "Tuesday",
    3: "Wednesday",
    4: "Thursday",
    5: "Friday",
}


class InvalidRequestError(Exception):
    pass


class InvalidExistingSchedule(Exception):
    pass


class TemplateSelectionConflict(Exception):
    pass


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
    result = ssm.get_parameter(
        Name=parameter_name,
        WithDecryption=True,
    )
    return result["Parameter"]["Value"]


def get_db_connection():
    secret_arn = get_parameter("secret-arn")
    secret_response = secretsmanager.get_secret_value(
        SecretId=secret_arn
    )
    secret_string = secret_response.get("SecretString")

    if not secret_string:
        raise RuntimeError("Database secret is empty")

    secret = json.loads(secret_string)
    password = secret.get("password")

    if not isinstance(password, str) or not password:
        raise RuntimeError("Database secret does not contain a password")

    return pymysql.connect(
        host=get_parameter("endpoint"),
        port=int(get_parameter("port")),
        user=get_parameter("username"),
        password=password,
        database=get_parameter("name"),
        connect_timeout=10,
        read_timeout=30,
        write_timeout=30,
        autocommit=False,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
    )


def get_http_method(event):
    method = event.get("httpMethod")

    if not method:
        request_context = event.get("requestContext") or {}
        http_context = request_context.get("http") or {}
        method = http_context.get("method")

    return method.upper() if isinstance(method, str) else None


def get_doctor_id(event):
    request_context = event.get("requestContext") or {}
    authorizer = request_context.get("authorizer") or {}

    doctor_id = authorizer.get("userId")
    role = authorizer.get("role")

    if not doctor_id or str(role).lower() != "doctor":
        raise PermissionError("Doctor access is required")

    return str(doctor_id)


def parse_request_body(event):
    raw_body = event.get("body")

    if not isinstance(raw_body, str) or not raw_body.strip():
        raise InvalidRequestError("Request body must be a JSON object")

    try:
        body = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise InvalidRequestError(
            "Request body must contain valid JSON"
        ) from exc

    if not isinstance(body, dict):
        raise InvalidRequestError("Request body must be a JSON object")

    unexpected_fields = set(body) - {"template_id"}
    if unexpected_fields:
        raise InvalidRequestError(
            "Unsupported field(s): "
            + ", ".join(sorted(unexpected_fields))
        )

    raw_template_id = body.get("template_id")

    if isinstance(raw_template_id, bool):
        raise InvalidRequestError("template_id must be a positive integer")

    if isinstance(raw_template_id, int):
        template_id = raw_template_id
    elif (
        isinstance(raw_template_id, str)
        and raw_template_id.isdigit()
    ):
        template_id = int(raw_template_id)
    else:
        raise InvalidRequestError("template_id must be a positive integer")

    if template_id <= 0:
        raise InvalidRequestError("template_id must be a positive integer")

    return template_id


def parse_iso_date(value, parameter_name):
    if not isinstance(value, str):
        raise InvalidRequestError(
            f"{parameter_name} must use YYYY-MM-DD format"
        )

    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise InvalidRequestError(
            f"{parameter_name} must use YYYY-MM-DD format"
        ) from exc

    if parsed.isoformat() != value:
        raise InvalidRequestError(
            f"{parameter_name} must use YYYY-MM-DD format"
        )

    return parsed


def parse_get_query(event):
    query = event.get("queryStringParameters") or {}

    has_date = "date" in query
    has_range_param = "from" in query or "to" in query

    if has_date and has_range_param:
        raise InvalidRequestError("Use either date or from/to, not both")

    if has_date:
        return "date", parse_iso_date(query["date"], "date"), None

    if "from" not in query or "to" not in query:
        raise InvalidRequestError("Provide date or both from and to")

    start_date = parse_iso_date(query["from"], "from")
    end_date = parse_iso_date(query["to"], "to")

    if end_date < start_date:
        raise InvalidRequestError("to must be on or after from")

    # Inclusive range: a difference of 89 days is 90 calendar days.
    if (end_date - start_date).days > 89:
        raise InvalidRequestError("Date range cannot exceed 90 days")

    return "range", start_date, end_date


def lock_doctor(cursor, doctor_id):
    cursor.execute(
        """
        SELECT doctor_id
        FROM doctor
        WHERE doctor_id = %s
        FOR UPDATE
        """,
        (doctor_id,),
    )

    if not cursor.fetchone():
        raise LookupError("Doctor not found")


def load_active_template(cursor, template_id):
    cursor.execute(
        """
        SELECT
            template_id,
            name,
            working_start_time,
            working_end_time
        FROM slot_template
        WHERE template_id = %s
          AND is_active = TRUE
        """,
        (template_id,),
    )

    template = cursor.fetchone()

    if not template:
        raise LookupError("Active shift template not found")

    if template["working_end_time"] <= template["working_start_time"]:
        raise InvalidExistingSchedule(
            "Template has invalid working hours"
        )

    return template


def load_existing_shifts(cursor, doctor_id):
    cursor.execute(
        """
        SELECT
            template_id,
            day_of_week,
            start_time,
            end_time
        FROM doctor_shift
        WHERE doctor_id = %s
        ORDER BY day_of_week
        FOR UPDATE
        """,
        (doctor_id,),
    )
    return cursor.fetchall()


def save_weekly_shifts(cursor, doctor_id, template):
    existing_shifts = load_existing_shifts(cursor, doctor_id)

    if existing_shifts:
        existing_template_ids = {
            row["template_id"] for row in existing_shifts
        }

        if len(existing_template_ids) != 1:
            raise InvalidExistingSchedule(
                "Doctor has inconsistent template selections"
            )

        existing_template_id = next(iter(existing_template_ids))

        if existing_template_id != template["template_id"]:
            raise TemplateSelectionConflict(
                "You have already selected another shift template"
            )

        existing_days = set()

        for row in existing_shifts:
            day_of_week = row["day_of_week"]

            if day_of_week not in WORKING_DAYS:
                raise InvalidExistingSchedule(
                    "Doctor has a shift outside Monday to Friday"
                )

            existing_days.add(day_of_week)

        existing_hours = {
            (row["start_time"], row["end_time"])
            for row in existing_shifts
        }

        if len(existing_hours) != 1:
            raise InvalidExistingSchedule(
                "Doctor's weekday shifts have inconsistent hours"
            )

        # Keep the existing saved hours if repairing a partial schedule.
        start_time, end_time = next(iter(existing_hours))
    else:
        existing_days = set()
        start_time = template["working_start_time"]
        end_time = template["working_end_time"]

    missing_days = [
        day for day in WORKING_DAYS
        if day not in existing_days
    ]

    if missing_days:
        cursor.executemany(
            """
            INSERT INTO doctor_shift (
                doctor_id,
                template_id,
                day_of_week,
                start_time,
                end_time
            )
            VALUES (%s, %s, %s, %s, %s)
            """,
            [
                (
                    doctor_id,
                    template["template_id"],
                    day,
                    start_time,
                    end_time,
                )
                for day in missing_days
            ],
        )

    return {
        "created_count": len(missing_days),
        "already_configured": not missing_days,
        "start_time": start_time,
        "end_time": end_time,
    }


def get_shift_occurrences(cursor, doctor_id, start_date, end_date):
    cursor.execute(
        """
        SELECT day_of_week, start_time, end_time
        FROM doctor_shift
        WHERE doctor_id = %s
        ORDER BY day_of_week
        """,
        (doctor_id,),
    )

    weekly_shifts = {
        row["day_of_week"]: row
        for row in cursor.fetchall()
    }

    shifts = []
    current_date = start_date

    while current_date <= end_date:
        # Python weekday(): Monday=0; doctor_shift: Monday=1.
        weekday_number = current_date.weekday() + 1
        shift = weekly_shifts.get(weekday_number)

        if shift:
            shifts.append({
                "date": current_date.isoformat(),
                "day": DAY_NAMES[weekday_number],
                "start_time": str(shift["start_time"]),
                "end_time": str(shift["end_time"]),
            })

        current_date += timedelta(days=1)

    return shifts


def get_shift_for_date(cursor, doctor_id, shift_date):
    day_of_week = shift_date.weekday() + 1

    if day_of_week not in WORKING_DAYS:
        return None

    cursor.execute(
        """
        SELECT start_time, end_time
        FROM doctor_shift
        WHERE doctor_id = %s
          AND day_of_week = %s
        """,
        (doctor_id, day_of_week),
    )
    row = cursor.fetchone()

    if not row:
        return None

    return {
        "date": shift_date.isoformat(),
        "day": DAY_NAMES[day_of_week],
        "start_time": str(row["start_time"]),
        "end_time": str(row["end_time"]),
    }


def delete_unbooked_future_slots(cursor, doctor_id):
    """
    Delete only future AVAILABLE slots with no appointment rows.
    The NOT EXISTS condition also protects slots with cancelled or
    historical appointment records that still reference the slot.
    """
    cursor.execute(
        """
        DELETE s
        FROM appointment_slot AS s
        WHERE s.doctor_id = %s
          AND s.slot_date >= CURRENT_DATE
          AND s.status = 'AVAILABLE'
          AND s.booked_count = 0
          AND NOT EXISTS (
              SELECT 1
              FROM appointments AS a
              WHERE a.slot_id = s.slot_id
          )
        """,
        (doctor_id,),
    )
    return cursor.rowcount


def delete_doctor_weekly_shifts(cursor, doctor_id):
    cursor.execute(
        """
        DELETE FROM doctor_shift
        WHERE doctor_id = %s
        """,
        (doctor_id,),
    )
    return cursor.rowcount


def lambda_handler(event, context):
    request_id = getattr(context, "aws_request_id", "unknown")
    connection = None
    doctor_id = None

    try:
        method = get_http_method(event)

        if method not in ("POST", "GET", "DELETE"):
            return response(405, {"message": "Method not allowed"})

        doctor_id = get_doctor_id(event)

        # Validate input before connecting to the database.
        if method == "POST":
            template_id = parse_request_body(event)
        elif method == "GET":
            query_type, first_date, second_date = parse_get_query(event)

        connection = get_db_connection()

        if method == "POST":
            with connection.cursor() as cursor:
                lock_doctor(cursor, doctor_id)
                template = load_active_template(cursor, template_id)
                result = save_weekly_shifts(
                    cursor,
                    doctor_id,
                    template,
                )

            connection.commit()

            status_code = 200 if result["already_configured"] else 201
            message = (
                "This shift template is already selected"
                if result["already_configured"]
                else "Weekly shift schedule saved"
            )

            return response(
                status_code,
                {
                    "message": message,
                    "template": {
                        "template_id": template["template_id"],
                        "name": template["name"],
                        "working_start_time": str(result["start_time"]),
                        "working_end_time": str(result["end_time"]),
                    },
                    "working_days": [
                        {
                            "day_of_week": day,
                            "day": DAY_NAMES[day],
                        }
                        for day in WORKING_DAYS
                    ],
                    "rows_created": result["created_count"],
                },
            )

        if method == "GET":
            with connection.cursor() as cursor:
                if query_type == "date":
                    shift = get_shift_for_date(
                        cursor,
                        doctor_id,
                        first_date,
                    )
                    return response(200, {"shift": shift})

                shifts = get_shift_occurrences(
                    cursor,
                    doctor_id,
                    first_date,
                    second_date,
                )

            return response(
                200,
                {
                    "from": first_date.isoformat(),
                    "to": second_date.isoformat(),
                    "shifts": shifts,
                },
            )

        # DELETE: remove this doctor's selection, not the shared template.
        with connection.cursor() as cursor:
            lock_doctor(cursor, doctor_id)

            slots_deleted = delete_unbooked_future_slots(
                cursor,
                doctor_id,
            )
            shifts_deleted = delete_doctor_weekly_shifts(
                cursor,
                doctor_id,
            )

        connection.commit()

        return response(
            200,
            {
                "message": (
                    "Your selected shift template was removed. "
                    "You can now select another template."
                ),
                "shift_rows_deleted": shifts_deleted,
                "unbooked_future_slots_deleted": slots_deleted,
                "booked_appointments_preserved": True,
            },
        )

    except PermissionError as exc:
        return response(403, {"message": str(exc)})

    except InvalidRequestError as exc:
        return response(400, {"message": str(exc)})

    except TemplateSelectionConflict as exc:
        if connection:
            connection.rollback()
        return response(409, {"message": str(exc)})

    except InvalidExistingSchedule:
        if connection:
            connection.rollback()
        logger.exception(
            "Invalid doctor schedule: request_id=%s",
            request_id,
        )
        return response(
            409,
            {"message": "Existing shift data is inconsistent"},
        )

    except LookupError as exc:
        if connection:
            connection.rollback()
        return response(404, {"message": str(exc)})

    except pymysql.err.IntegrityError as exc:
        if connection:
            connection.rollback()

        if exc.args and exc.args[0] == 1062:
            return response(
                409,
                {"message": "A weekday shift already exists"},
            )

        logger.exception(
            "Shift database constraint error: request_id=%s",
            request_id,
        )
        return response(
            409,
            {"message": "Shift data conflicts with database constraints"},
        )

    except (pymysql.MySQLError, BotoCoreError, ClientError):
        if connection:
            connection.rollback()
        logger.exception(
            "Shift database or AWS error: request_id=%s",
            request_id,
        )
        return response(
            500,
            {"message": "Unable to process the shift request"},
        )

    except Exception:
        if connection:
            connection.rollback()
        logger.exception(
            "Unexpected shift request error: request_id=%s",
            request_id,
        )
        return response(
            500,
            {"message": "An unexpected error occurred"},
        )

    finally:
        if connection:
            connection.close()