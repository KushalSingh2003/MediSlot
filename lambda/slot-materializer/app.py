import json
import logging
import os
from datetime import datetime, date, time, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

import boto3
import pymysql
from botocore.exceptions import ClientError
from pymysql.cursors import DictCursor
from pymysql.err import OperationalError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ENV = os.environ.get("ENV", "dev")
SSM_PREFIX = os.environ["SSM_PREFIX"]

TIMEZONE = os.environ.get("TIMEZONE", "Asia/Kolkata")
GENERATION_DAYS = int(os.environ.get("GENERATION_DAYS", "30"))

ssm = boto3.client("ssm")
secretsmanager = boto3.client("secretsmanager")


def get_parameter(name):
    return ssm.get_parameter(
        Name=f"{SSM_PREFIX}/db/{name}",
        WithDecryption=True,
    )["Parameter"]["Value"]


def get_db_config():
    secret_response = secretsmanager.get_secret_value(
        SecretId=get_parameter("secret-arn")
    )

    try:
        secret = json.loads(secret_response["SecretString"])
        password = secret["password"]
    except (json.JSONDecodeError, KeyError):
        password = secret_response["SecretString"]

    return {
        "host": get_parameter("endpoint"),
        "port": int(get_parameter("port")),
        "user": get_parameter("username"),
        "password": password,
        "database": get_parameter("name"),
        "charset": "utf8mb4",
        "cursorclass": DictCursor,
        "connect_timeout": 10,
        "read_timeout": 30,
        "write_timeout": 30,
        "autocommit": False,
    }


def get_connection():
    return pymysql.connect(**get_db_config())


def normalize_time(value):
    if isinstance(value, time):
        return value

    if isinstance(value, timedelta):
        total_seconds = int(value.total_seconds())
        return time(
            (total_seconds // 3600) % 24,
            (total_seconds % 3600) // 60,
            total_seconds % 60,
        )

    return time.fromisoformat(str(value))


def get_doctors_with_shifts(cursor):
    cursor.execute(
        """
        SELECT DISTINCT d.doctor_id
        FROM doctor d
        INNER JOIN doctor_shift ds
            ON ds.doctor_id = d.doctor_id
        ORDER BY d.doctor_id
        """
    )
    return cursor.fetchall()


def get_doctor_weekly_shift(cursor, doctor_id):
    cursor.execute(
        """
        SELECT
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

    shifts = {}

    for row in cursor.fetchall():
        shifts[int(row["day_of_week"])] = {
            "start_time": normalize_time(row["start_time"]),
            "end_time": normalize_time(row["end_time"]),
        }

    return shifts


def get_doctor_consultation_types(cursor, doctor_id):
    cursor.execute(
        """
        SELECT
            dct.consultation_type_id,
            dct.capacity,
            ct.duration_min,
            ct.name
        FROM doctor_consultation_type dct
        INNER JOIN consultation_type ct
            ON ct.id = dct.consultation_type_id
        WHERE dct.doctor_id = %s
          AND dct.active = TRUE
        ORDER BY dct.consultation_type_id
        """,
        (doctor_id,),
    )

    consultation_types = []

    for row in cursor.fetchall():
        capacity = int(row["capacity"])

        try:
            duration_decimal = Decimal(str(row["duration_min"]))
        except (InvalidOperation, TypeError):
            raise RuntimeError(
                f"Invalid duration for consultation type "
                f"{row['consultation_type_id']}."
            )

        if duration_decimal <= 0:
            raise RuntimeError(
                f"Invalid duration for consultation type "
                f"{row['consultation_type_id']}."
            )

        duration_seconds = int(duration_decimal * Decimal("60"))

        if duration_seconds <= 0:
            raise RuntimeError(
                f"Invalid duration for consultation type "
                f"{row['consultation_type_id']}."
            )

        if capacity <= 0:
            raise RuntimeError(
                f"Invalid capacity for consultation type "
                f"{row['consultation_type_id']}."
            )

        consultation_types.append({
            "consultation_type_id": int(
                row["consultation_type_id"]
            ),
            "duration_seconds": duration_seconds,
            "capacity": capacity,
            "name": row["name"],
        })

    return consultation_types


def get_blackout_dates(cursor, doctor_id, start_date, end_date):
    cursor.execute(
        """
        SELECT blackout_date
        FROM blackout
        WHERE doctor_id = %s
          AND blackout_date BETWEEN %s AND %s
        """,
        (doctor_id, start_date, end_date),
    )

    return {
        row["blackout_date"]
        for row in cursor.fetchall()
    }


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
        raise RuntimeError(f"Doctor not found: {doctor_id}")


def delete_unbooked_slots(cursor, doctor_id, slot_date):
    cursor.execute(
        """
        DELETE s
        FROM appointment_slot s
        WHERE s.doctor_id = %s
          AND s.slot_date = %s
          AND s.booked_count = 0
          AND NOT EXISTS (
              SELECT 1
              FROM appointments a
              WHERE a.slot_id = s.slot_id
          )
        """,
        (doctor_id, slot_date),
    )
    return cursor.rowcount


def generate_day_slots(slot_date, shift, consultation_types):
    generated_slots = []

    if not shift or not consultation_types:
        return generated_slots

    working_start = shift["start_time"]
    working_end = shift["end_time"]

    if working_start >= working_end:
        raise RuntimeError(
            f"Invalid working hours for date {slot_date}: "
            f"{working_start}-{working_end}"
        )

    current_datetime = datetime.combine(slot_date, working_start)
    working_end_datetime = datetime.combine(slot_date, working_end)

    # Sort is already performed by the query. The index advances
    # deterministically, so active types are distributed in rotation.
    consultation_index = 0

    while current_datetime < working_end_datetime:
        remaining_seconds = (
            working_end_datetime - current_datetime
        ).total_seconds()

        # Start from the next type in rotation; if it does not fit in the
        # remaining time, try the following types until one does. This
        # avoids leaving a gap at the end of the shift.
        consultation_type = None

        for offset in range(len(consultation_types)):
            candidate = consultation_types[
                (consultation_index + offset) % len(consultation_types)
            ]

            if candidate["duration_seconds"] <= remaining_seconds:
                consultation_type = candidate
                consultation_index += offset + 1
                break

        # Nothing fits: the time left is shorter than every consultation.
        if consultation_type is None:
            break

        end_datetime = current_datetime + timedelta(
            seconds=consultation_type["duration_seconds"]
        )

        generated_slots.append({
            "doctor_id": None,
            "consultation_type_id":
                consultation_type["consultation_type_id"],
            "slot_date": slot_date,
            "start_time": current_datetime.time(),
            "end_time": end_datetime.time(),
            "status": "AVAILABLE",
            "capacity": consultation_type["capacity"],
            "booked_count": 0,
        })

        current_datetime = end_datetime

    return generated_slots


def insert_generated_slots(cursor, doctor_id, generated_slots):
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
                slot["capacity"],
            ),
        )
        inserted_count += cursor.rowcount

    return inserted_count


def materialize_slots():
    connection = None

    total_inserted = 0
    total_deleted = 0
    total_skipped_blackout_days = 0
    total_processed_days = 0
    doctors_processed = 0

    local_today = datetime.now(
        ZoneInfo(TIMEZONE)
    ).date()
    end_date = local_today + timedelta(
        days=GENERATION_DAYS - 1
    )

    logger.info(
        "Starting slot materialization. Start=%s End=%s Horizon=%s",
        local_today,
        end_date,
        GENERATION_DAYS,
    )

    try:
        connection = get_connection()

        with connection.cursor() as cursor:
            doctors = get_doctors_with_shifts(cursor)

        for doctor in doctors:
            doctor_id = doctor["doctor_id"]
            doctors_processed += 1

            with connection.cursor() as cursor:
                connection.begin()
                lock_doctor(cursor, doctor_id)

                weekly_shifts = get_doctor_weekly_shift(
                    cursor,
                    doctor_id,
                )
                consultation_types = (
                    get_doctor_consultation_types(
                        cursor,
                        doctor_id,
                    )
                )
                blackout_dates = get_blackout_dates(
                    cursor,
                    doctor_id,
                    local_today,
                    end_date,
                )

                current_date = local_today

                try:
                    while current_date <= end_date:
                        deleted = delete_unbooked_slots(
                            cursor,
                            doctor_id,
                            current_date,
                        )
                        total_deleted += deleted

                        if current_date in blackout_dates:
                            total_skipped_blackout_days += 1
                            current_date += timedelta(days=1)
                            continue

                        # isoweekday(): Monday=1, Sunday=7.
                        shift = weekly_shifts.get(
                            current_date.isoweekday()
                        )

                        if not shift:
                            current_date += timedelta(days=1)
                            continue

                        generated_slots = generate_day_slots(
                            slot_date=current_date,
                            shift=shift,
                            consultation_types=consultation_types,
                        )

                        inserted = insert_generated_slots(
                            cursor,
                            doctor_id,
                            generated_slots,
                        )
                        total_inserted += inserted
                        total_processed_days += 1

                        logger.info(
                            "Doctor=%s Date=%s Generated=%s "
                            "Deleted=%s Inserted=%s",
                            doctor_id,
                            current_date,
                            len(generated_slots),
                            deleted,
                            inserted,
                        )

                        current_date += timedelta(days=1)

                    connection.commit()

                except Exception:
                    connection.rollback()
                    raise

        result = {
            "message": "Slot materialization completed.",
            "startDate": str(local_today),
            "endDate": str(end_date),
            "doctorsProcessed": doctors_processed,
            "daysProcessed": total_processed_days,
            "blackoutDaysSkipped": total_skipped_blackout_days,
            "slotsDeleted": total_deleted,
            "slotsInserted": total_inserted,
        }

        logger.info(
            "Slot materialization completed: %s",
            json.dumps(result),
        )
        return result

    finally:
        if connection:
            connection.close()


def lambda_handler(event, context):
    request_id = getattr(
        context,
        "aws_request_id",
        "unknown",
    )
    logger.info(
        "Slot materializer invoked. RequestId=%s",
        request_id,
    )

    try:
        result = materialize_slots()
        return {
            "statusCode": 200,
            "headers": {
                "Content-Type": "application/json",
            },
            "body": json.dumps(result),
        }

    except OperationalError:
        logger.exception("MySQL operational error.")
        return {
            "statusCode": 503,
            "body": json.dumps({
                "message": "Database temporarily unavailable.",
            }),
        }

    except ClientError:
        logger.exception("AWS service error.")
        return {
            "statusCode": 500,
            "body": json.dumps({
                "message": "Unable to retrieve database configuration.",
            }),
        }

    except Exception:
        logger.exception("Slot materialization failed.")
        return {
            "statusCode": 500,
            "body": json.dumps({
                "message": "Slot materialization failed.",
            }),
        }