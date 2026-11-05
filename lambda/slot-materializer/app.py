import os
import json
import logging
import boto3
import pymysql

from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo
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

TIMEZONE = os.environ.get("TIMEZONE", "Asia/Kolkata")
GENERATION_DAYS = int(os.environ.get("GENERATION_DAYS", "30"))

MORNING_END = time.fromisoformat(
    os.environ.get("MORNING_END", "12:00")
)

AFTERNOON_START = time.fromisoformat(
    os.environ.get("AFTERNOON_START", "12:00")
)

ssm = boto3.client("ssm")
secretsmanager = boto3.client("secretsmanager")


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
        "autocommit": False
    }


def get_connection():

    return pymysql.connect(**get_db_config())


# ==========================================================
# TIME HELPERS
# ==========================================================

def normalize_time(value):
    """
    Converts MySQL TIME values into Python time objects.
    """

    if isinstance(value, time):
        return value

    if isinstance(value, timedelta):

        total_seconds = int(value.total_seconds())

        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        seconds = total_seconds % 60

        return time(hours % 24, minutes, seconds)

    return time.fromisoformat(str(value))


# ==========================================================
# FETCH ACTIVE UNIVERSAL TEMPLATE
# ==========================================================

def get_active_template(cursor):

    cursor.execute(
        """
        SELECT
            template_id,
            working_start_time,
            working_end_time
        FROM slot_template
        WHERE is_active = 1
        ORDER BY template_id DESC
        LIMIT 1
        """
    )

    template = cursor.fetchone()

    if not template:
        raise RuntimeError(
            "No active universal slot template found."
        )

    template["working_start_time"] = normalize_time(
        template["working_start_time"]
    )

    template["working_end_time"] = normalize_time(
        template["working_end_time"]
    )

    if (
        template["working_start_time"]
        >= template["working_end_time"]
    ):
        raise RuntimeError(
            "Invalid template working hours."
        )

    return template


# ==========================================================
# FETCH ACTIVE CONSULTATION RULES
# ==========================================================

def get_consultation_rules(cursor):

    cursor.execute(
        """
        SELECT
            r.rule_id,
            r.day_of_week,
            r.consultation_type_id,
            r.allocation_weight,
            r.time_period,
            ct.duration_min
        FROM template_consultation_rule r
        INNER JOIN consultation_type ct
            ON ct.id = r.consultation_type_id
        WHERE r.is_active = 1
        ORDER BY r.rule_id
        """
    )

    rules = cursor.fetchall()

    for rule in rules:

        rule["allocation_weight"] = max(
            1,
            int(rule["allocation_weight"] or 1)
        )

        rule["duration_min"] = int(
            rule["duration_min"]
        )

        if rule["duration_min"] <= 0:
            raise RuntimeError(
                "Invalid consultation duration."
            )

        rule["time_period"] = (
            rule["time_period"] or "ANY"
        ).upper()

        if rule["time_period"] not in (
            "MORNING",
            "AFTERNOON",
            "ANY"
        ):
            raise RuntimeError(
                f"Invalid time period: {rule['time_period']}"
            )

    return rules


# ==========================================================
# FETCH DOCTORS
# ==========================================================

def get_doctors(cursor):

    cursor.execute(
        """
        SELECT doctor_id
        FROM doctor
        """
    )

    return cursor.fetchall()


# ==========================================================
# FETCH DOCTOR CONSULTATION TYPES
# ==========================================================

def get_doctor_consultation_types(cursor, doctor_id):

    cursor.execute(
        """
        SELECT
            consultation_type_id,
            capacity
        FROM doctor_consultation_type
        WHERE doctor_id = %s
          AND active = 1
        """,
        (doctor_id,)
    )

    rows = cursor.fetchall()

    return {
        row["consultation_type_id"]: int(row["capacity"])
        for row in rows
    }


# ==========================================================
# FETCH DOCTOR BLACKOUT DATES
# ==========================================================

def get_blackout_dates(
    cursor,
    doctor_id,
    start_date,
    end_date
):

    cursor.execute(
        """
        SELECT blackout_date
        FROM blackout
        WHERE doctor_id = %s
          AND blackout_date BETWEEN %s AND %s
        """,
        (
            doctor_id,
            start_date,
            end_date
        )
    )

    return {
        row["blackout_date"]
        for row in cursor.fetchall()
    }


# ==========================================================
# GENERATE SLOTS FOR A PARTICULAR DAY
# ==========================================================

def generate_day_slots(
    slot_date,
    working_start,
    working_end,
    rules,
    doctor_consultation_types
):

    weekday = slot_date.weekday()

    # Select rules applicable to this weekday
    daily_rules = []

    for rule in rules:

        if int(rule["day_of_week"]) != weekday:
            continue

        consultation_type_id = rule["consultation_type_id"]

        # Doctor must actively offer this consultation type.
        if consultation_type_id not in doctor_consultation_types:
            continue

        daily_rules.append({
            "consultation_type_id": consultation_type_id,
            "duration_min": rule["duration_min"],
            "allocation_weight": rule["allocation_weight"],
            "time_period": rule["time_period"],
            "capacity": doctor_consultation_types[
                consultation_type_id
            ]
        })

    if not daily_rules:
        return []

    # Weighted rotation.
    #
    # Example:
    # Type A weight 2
    # Type B weight 1
    #
    # Rotation: A, A, B

    rotation = []

    for rule in daily_rules:

        rotation.extend(
            [rule] * rule["allocation_weight"]
        )

    generated_slots = []

    # Split working hours into morning and afternoon.
    windows = [
        (
            "MORNING",
            max(working_start, time(0, 0)),
            min(working_end, MORNING_END)
        ),
        (
            "AFTERNOON",
            max(working_start, AFTERNOON_START),
            min(working_end, time(23, 59, 59))
        )
    ]

    rotation_index = 0

    for period, window_start, window_end in windows:

        if window_start >= window_end:
            continue

        current_time = datetime.combine(
            slot_date,
            window_start
        )

        window_end_datetime = datetime.combine(
            slot_date,
            window_end
        )

        while current_time < window_end_datetime:

            selected_rule = None

            # Find the next eligible consultation type.
            for _ in range(len(rotation)):

                rule = rotation[
                    rotation_index % len(rotation)
                ]

                rotation_index += 1

                if (
                    rule["time_period"] != "ANY"
                    and rule["time_period"] != period
                ):
                    continue

                duration = timedelta(
                    minutes=rule["duration_min"]
                )

                if (
                    current_time + duration
                    > window_end_datetime
                ):
                    continue

                selected_rule = rule
                break

            if selected_rule is None:
                break

            end_datetime = current_time + timedelta(
                minutes=selected_rule["duration_min"]
            )

            generated_slots.append({
                "doctor_id": None,
                "consultation_type_id":
                    selected_rule["consultation_type_id"],
                "slot_date": slot_date,
                "start_time": current_time.time(),
                "end_time": end_datetime.time(),
                "status": "AVAILABLE",
                "capacity": selected_rule["capacity"],
                "booked_count": 0
            })

            current_time = end_datetime

    return generated_slots


# ==========================================================
# INSERT GENERATED SLOTS
# ==========================================================

def insert_slots(
    cursor,
    doctor_id,
    generated_slots
):

    inserted_count = 0

    for slot in generated_slots:

        cursor.execute(
            """
            INSERT IGNORE INTO appointment_slot (
                doctor_id,
                consultation_type_id,
                slot_date,
                start_time,
                end_time,
                status,
                capacity,
                booked_count,
                hold_expires_at
            )
            VALUES (
                %s, %s, %s, %s, %s,
                'AVAILABLE', %s, 0, NULL
            )
            """,
            (
                doctor_id,
                slot["consultation_type_id"],
                slot["slot_date"],
                slot["start_time"],
                slot["end_time"],
                slot["capacity"]
            )
        )

        inserted_count += cursor.rowcount

    return inserted_count


# ==========================================================
# MATERIALIZE SLOTS
# ==========================================================

def materialize_slots():

    connection = None

    total_inserted = 0
    total_skipped_blackout_days = 0
    total_processed_days = 0

    local_today = datetime.now(
        ZoneInfo(TIMEZONE)
    ).date()

    end_date = local_today + timedelta(
        days=GENERATION_DAYS - 1
    )

    logger.info(
        "Starting slot materialization. Start=%s End=%s",
        local_today,
        end_date
    )

    try:

        connection = get_connection()

        with connection.cursor() as cursor:

            # Fetch universal template and rules.
            template = get_active_template(cursor)

            rules = get_consultation_rules(cursor)

            doctors = get_doctors(cursor)

            working_start = template["working_start_time"]
            working_end = template["working_end_time"]

            logger.info(
                "Active template=%s Doctors=%s",
                template["template_id"],
                len(doctors)
            )

            # Process each doctor independently.
            for doctor in doctors:

                doctor_id = doctor["doctor_id"]

                doctor_consultation_types = (
                    get_doctor_consultation_types(
                        cursor,
                        doctor_id
                    )
                )

                if not doctor_consultation_types:
                    continue

                blackout_dates = get_blackout_dates(
                    cursor,
                    doctor_id,
                    local_today,
                    end_date
                )

                current_date = local_today

                while current_date <= end_date:

                    # Blackout dates must not receive slots.
                    if current_date in blackout_dates:

                        total_skipped_blackout_days += 1

                        current_date += timedelta(days=1)

                        continue

                    # Generate slots for this date.
                    generated_slots = generate_day_slots(
                        slot_date=current_date,
                        working_start=working_start,
                        working_end=working_end,
                        rules=rules,
                        doctor_consultation_types=(
                            doctor_consultation_types
                        )
                    )

                    try:

                        # One transaction per doctor/date.
                        connection.begin()

                        inserted = insert_slots(
                            cursor,
                            doctor_id,
                            generated_slots
                        )

                        connection.commit()

                        total_inserted += inserted
                        total_processed_days += 1

                        logger.info(
                            "Doctor=%s Date=%s Generated=%s Inserted=%s",
                            doctor_id,
                            current_date,
                            len(generated_slots),
                            inserted
                        )

                    except Exception:

                        connection.rollback()

                        logger.exception(
                            "Failed materialization for doctor=%s date=%s",
                            doctor_id,
                            current_date
                        )

                        raise

                    current_date += timedelta(days=1)

        result = {
            "message": "Slot materialization completed.",
            "startDate": str(local_today),
            "endDate": str(end_date),
            "doctorsProcessed": len(doctors),
            "daysProcessed": total_processed_days,
            "blackoutDaysSkipped": total_skipped_blackout_days,
            "slotsInserted": total_inserted
        }

        logger.info(
            "Slot materialization completed: %s",
            json.dumps(result)
        )

        return result

    finally:

        if connection:
            connection.close()


# ==========================================================
# LAMBDA HANDLER
# ==========================================================

def lambda_handler(event, context):

    logger.info(
        "Slot materializer invoked. RequestId=%s",
        context.aws_request_id
    )

    try:

        result = materialize_slots()

        return {
            "statusCode": 200,
            "body": json.dumps(result)
        }

    except OperationalError:

        logger.exception("MySQL operational error.")

        return {
            "statusCode": 503,
            "body": json.dumps({
                "message": "Database temporarily unavailable."
            })
        }

    except ClientError:

        logger.exception("AWS service error.")

        return {
            "statusCode": 500,
            "body": json.dumps({
                "message": "Unable to retrieve database configuration."
            })
        }

    except Exception:

        logger.exception(
            "Slot materialization failed."
        )

        return {
            "statusCode": 500,
            "body": json.dumps({
                "message": "Slot materialization failed."
            })
        }