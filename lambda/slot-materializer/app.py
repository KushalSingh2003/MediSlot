# import json
# import logging
# import os
# from datetime import datetime, date, time, timedelta
# from decimal import Decimal, InvalidOperation
# from zoneinfo import ZoneInfo

# import boto3
# import pymysql
# from botocore.exceptions import ClientError
# from pymysql.cursors import DictCursor
# from pymysql.err import OperationalError

# logger = logging.getLogger()
# logger.setLevel(logging.INFO)

# ENV = os.environ.get("ENV", "dev")
# SSM_PREFIX = os.environ["SSM_PREFIX"]

# TIMEZONE = os.environ.get("TIMEZONE", "Asia/Kolkata")
# GENERATION_DAYS = int(os.environ.get("GENERATION_DAYS", "30"))

# ssm = boto3.client("ssm")
# secretsmanager = boto3.client("secretsmanager")


# def get_parameter(name):
#     return ssm.get_parameter(
#         Name=f"{SSM_PREFIX}/db/{name}",
#         WithDecryption=True,
#     )["Parameter"]["Value"]


# def get_db_config():
#     secret_response = secretsmanager.get_secret_value(
#         SecretId=get_parameter("secret-arn")
#     )

#     try:
#         secret = json.loads(secret_response["SecretString"])
#         password = secret["password"]
#     except (json.JSONDecodeError, KeyError):
#         password = secret_response["SecretString"]

#     return {
#         "host": get_parameter("endpoint"),
#         "port": int(get_parameter("port")),
#         "user": get_parameter("username"),
#         "password": password,
#         "database": get_parameter("name"),
#         "charset": "utf8mb4",
#         "cursorclass": DictCursor,
#         "connect_timeout": 10,
#         "read_timeout": 30,
#         "write_timeout": 30,
#         "autocommit": False,
#     }


# def get_connection():
#     return pymysql.connect(**get_db_config())


# def normalize_time(value):
#     if isinstance(value, time):
#         return value

#     if isinstance(value, timedelta):
#         total_seconds = int(value.total_seconds())
#         return time(
#             (total_seconds // 3600) % 24,
#             (total_seconds % 3600) // 60,
#             total_seconds % 60,
#         )

#     return time.fromisoformat(str(value))


# def get_doctors_with_shifts(cursor):
#     cursor.execute(
#         """
#         SELECT DISTINCT d.doctor_id
#         FROM doctor d
#         INNER JOIN doctor_shift ds
#             ON ds.doctor_id = d.doctor_id
#         ORDER BY d.doctor_id
#         """
#     )
#     return cursor.fetchall()


# def get_doctor_weekly_shift(cursor, doctor_id):
#     cursor.execute(
#         """
#         SELECT
#             day_of_week,
#             start_time,
#             end_time
#         FROM doctor_shift
#         WHERE doctor_id = %s
#         ORDER BY day_of_week
#         FOR UPDATE
#         """,
#         (doctor_id,),
#     )

#     shifts = {}

#     for row in cursor.fetchall():
#         shifts[int(row["day_of_week"])] = {
#             "start_time": normalize_time(row["start_time"]),
#             "end_time": normalize_time(row["end_time"]),
#         }

#     return shifts


# def get_doctor_consultation_types(cursor, doctor_id):
#     cursor.execute(
#         """
#         SELECT
#             dct.consultation_type_id,
#             dct.capacity,
#             ct.duration_min,
#             ct.name
#         FROM doctor_consultation_type dct
#         INNER JOIN consultation_type ct
#             ON ct.id = dct.consultation_type_id
#         WHERE dct.doctor_id = %s
#           AND dct.active = TRUE
#         ORDER BY dct.consultation_type_id
#         """,
#         (doctor_id,),
#     )

#     consultation_types = []

#     for row in cursor.fetchall():
#         capacity = int(row["capacity"])

#         try:
#             duration_decimal = Decimal(str(row["duration_min"]))
#         except (InvalidOperation, TypeError):
#             raise RuntimeError(
#                 f"Invalid duration for consultation type "
#                 f"{row['consultation_type_id']}."
#             )

#         if duration_decimal <= 0:
#             raise RuntimeError(
#                 f"Invalid duration for consultation type "
#                 f"{row['consultation_type_id']}."
#             )

#         duration_seconds = int(duration_decimal * Decimal("60"))

#         if duration_seconds <= 0:
#             raise RuntimeError(
#                 f"Invalid duration for consultation type "
#                 f"{row['consultation_type_id']}."
#             )

#         if capacity <= 0:
#             raise RuntimeError(
#                 f"Invalid capacity for consultation type "
#                 f"{row['consultation_type_id']}."
#             )

#         consultation_types.append({
#             "consultation_type_id": int(
#                 row["consultation_type_id"]
#             ),
#             "duration_seconds": duration_seconds,
#             "capacity": capacity,
#             "name": row["name"],
#         })

#     return consultation_types


# def get_blackout_dates(cursor, doctor_id, start_date, end_date):
#     cursor.execute(
#         """
#         SELECT blackout_date
#         FROM blackout
#         WHERE doctor_id = %s
#           AND blackout_date BETWEEN %s AND %s
#         """,
#         (doctor_id, start_date, end_date),
#     )

#     return {
#         row["blackout_date"]
#         for row in cursor.fetchall()
#     }


# def lock_doctor(cursor, doctor_id):
#     cursor.execute(
#         """
#         SELECT doctor_id
#         FROM doctor
#         WHERE doctor_id = %s
#         FOR UPDATE
#         """,
#         (doctor_id,),
#     )

#     if not cursor.fetchone():
#         raise RuntimeError(f"Doctor not found: {doctor_id}")


# def delete_unbooked_slots(cursor, doctor_id, slot_date):
#     cursor.execute(
#         """
#         DELETE s
#         FROM appointment_slot s
#         WHERE s.doctor_id = %s
#           AND s.slot_date = %s
#           AND s.booked_count = 0
#           AND NOT EXISTS (
#               SELECT 1
#               FROM appointments a
#               WHERE a.slot_id = s.slot_id
#           )
#         """,
#         (doctor_id, slot_date),
#     )
#     return cursor.rowcount


# def generate_day_slots(slot_date, shift, consultation_types):
#     generated_slots = []

#     if not shift or not consultation_types:
#         return generated_slots

#     working_start = shift["start_time"]
#     working_end = shift["end_time"]

#     if working_start >= working_end:
#         raise RuntimeError(
#             f"Invalid working hours for date {slot_date}: "
#             f"{working_start}-{working_end}"
#         )

#     current_datetime = datetime.combine(slot_date, working_start)
#     working_end_datetime = datetime.combine(slot_date, working_end)

#     # Sort is already performed by the query. The index advances
#     # deterministically, so active types are distributed in rotation.
#     consultation_index = 0

#     while current_datetime < working_end_datetime:
#         remaining_seconds = (
#             working_end_datetime - current_datetime
#         ).total_seconds()

#         # Start from the next type in rotation; if it does not fit in the
#         # remaining time, try the following types until one does. This
#         # avoids leaving a gap at the end of the shift.
#         consultation_type = None

#         for offset in range(len(consultation_types)):
#             candidate = consultation_types[
#                 (consultation_index + offset) % len(consultation_types)
#             ]

#             if candidate["duration_seconds"] <= remaining_seconds:
#                 consultation_type = candidate
#                 consultation_index += offset + 1
#                 break

#         # Nothing fits: the time left is shorter than every consultation.
#         if consultation_type is None:
#             break

#         end_datetime = current_datetime + timedelta(
#             seconds=consultation_type["duration_seconds"]
#         )

#         generated_slots.append({
#             "doctor_id": None,
#             "consultation_type_id":
#                 consultation_type["consultation_type_id"],
#             "slot_date": slot_date,
#             "start_time": current_datetime.time(),
#             "end_time": end_datetime.time(),
#             "status": "AVAILABLE",
#             "capacity": consultation_type["capacity"],
#             "booked_count": 0,
#         })

#         current_datetime = end_datetime

#     return generated_slots


# def insert_generated_slots(cursor, doctor_id, generated_slots):
#     inserted_count = 0

#     for slot in generated_slots:
#         cursor.execute(
#             """
#             INSERT IGNORE INTO appointment_slot (
#                 doctor_id,
#                 consultation_type_id,
#                 slot_date,
#                 start_time,
#                 end_time,
#                 status,
#                 capacity,
#                 booked_count,
#                 hold_expires_at
#             )
#             VALUES (
#                 %s, %s, %s, %s, %s,
#                 'AVAILABLE', %s, 0, NULL
#             )
#             """,
#             (
#                 doctor_id,
#                 slot["consultation_type_id"],
#                 slot["slot_date"],
#                 slot["start_time"],
#                 slot["end_time"],
#                 slot["capacity"],
#             ),
#         )
#         inserted_count += cursor.rowcount

#     return inserted_count


# def materialize_slots():
#     connection = None

#     total_inserted = 0
#     total_deleted = 0
#     total_skipped_blackout_days = 0
#     total_processed_days = 0
#     doctors_processed = 0

#     local_today = datetime.now(
#         ZoneInfo(TIMEZONE)
#     ).date()
#     end_date = local_today + timedelta(
#         days=GENERATION_DAYS - 1
#     )

#     logger.info(
#         "Starting slot materialization. Start=%s End=%s Horizon=%s",
#         local_today,
#         end_date,
#         GENERATION_DAYS,
#     )

#     try:
#         connection = get_connection()

#         with connection.cursor() as cursor:
#             doctors = get_doctors_with_shifts(cursor)

#         for doctor in doctors:
#             doctor_id = doctor["doctor_id"]
#             doctors_processed += 1

#             with connection.cursor() as cursor:
#                 connection.begin()
#                 lock_doctor(cursor, doctor_id)

#                 weekly_shifts = get_doctor_weekly_shift(
#                     cursor,
#                     doctor_id,
#                 )
#                 consultation_types = (
#                     get_doctor_consultation_types(
#                         cursor,
#                         doctor_id,
#                     )
#                 )
#                 blackout_dates = get_blackout_dates(
#                     cursor,
#                     doctor_id,
#                     local_today,
#                     end_date,
#                 )

#                 current_date = local_today

#                 try:
#                     while current_date <= end_date:
#                         deleted = delete_unbooked_slots(
#                             cursor,
#                             doctor_id,
#                             current_date,
#                         )
#                         total_deleted += deleted

#                         if current_date in blackout_dates:
#                             total_skipped_blackout_days += 1
#                             current_date += timedelta(days=1)
#                             continue

#                         # isoweekday(): Monday=1, Sunday=7.
#                         shift = weekly_shifts.get(
#                             current_date.isoweekday()
#                         )

#                         if not shift:
#                             current_date += timedelta(days=1)
#                             continue

#                         generated_slots = generate_day_slots(
#                             slot_date=current_date,
#                             shift=shift,
#                             consultation_types=consultation_types,
#                         )

#                         inserted = insert_generated_slots(
#                             cursor,
#                             doctor_id,
#                             generated_slots,
#                         )
#                         total_inserted += inserted
#                         total_processed_days += 1

#                         logger.info(
#                             "Doctor=%s Date=%s Generated=%s "
#                             "Deleted=%s Inserted=%s",
#                             doctor_id,
#                             current_date,
#                             len(generated_slots),
#                             deleted,
#                             inserted,
#                         )

#                         current_date += timedelta(days=1)

#                     connection.commit()

#                 except Exception:
#                     connection.rollback()
#                     raise

#         result = {
#             "message": "Slot materialization completed.",
#             "startDate": str(local_today),
#             "endDate": str(end_date),
#             "doctorsProcessed": doctors_processed,
#             "daysProcessed": total_processed_days,
#             "blackoutDaysSkipped": total_skipped_blackout_days,
#             "slotsDeleted": total_deleted,
#             "slotsInserted": total_inserted,
#         }

#         logger.info(
#             "Slot materialization completed: %s",
#             json.dumps(result),
#         )
#         return result

#     finally:
#         if connection:
#             connection.close()


# def lambda_handler(event, context):
#     request_id = getattr(
#         context,
#         "aws_request_id",
#         "unknown",
#     )
#     logger.info(
#         "Slot materializer invoked. RequestId=%s",
#         request_id,
#     )

#     try:
#         result = materialize_slots()
#         return {
#             "statusCode": 200,
#             "headers": {
#                 "Content-Type": "application/json",
#             },
#             "body": json.dumps(result),
#         }

#     except OperationalError:
#         logger.exception("MySQL operational error.")
#         return {
#             "statusCode": 503,
#             "body": json.dumps({
#                 "message": "Database temporarily unavailable.",
#             }),
#         }

#     except ClientError:
#         logger.exception("AWS service error.")
#         return {
#             "statusCode": 500,
#             "body": json.dumps({
#                 "message": "Unable to retrieve database configuration.",
#             }),
#         }

#     except Exception:
#         logger.exception("Slot materialization failed.")
#         return {
#             "statusCode": 500,
#             "body": json.dumps({
#                 "message": "Slot materialization failed.",
#             }),
#         }
import json
import logging
import os
import traceback

from datetime import datetime, date, time, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

import boto3
import pymysql

from botocore.exceptions import BotoCoreError, ClientError
from pymysql.cursors import DictCursor
from pymysql.err import OperationalError


# ---------------------------------------------------------
# LOGGING
# ---------------------------------------------------------

logger = logging.getLogger()
logger.setLevel(logging.INFO)


# ---------------------------------------------------------
# ENVIRONMENT CONFIGURATION
# ---------------------------------------------------------

ENV = os.environ.get("ENV", "dev")
SSM_PREFIX = os.environ.get("SSM_PREFIX", f"/medislot/{ENV}")

TIMEZONE = os.environ.get("TIMEZONE", "Asia/Kolkata")
GENERATION_DAYS = int(os.environ.get("GENERATION_DAYS", "30"))

# Generate slots for today through today + 29 days.
# This gives exactly 30 calendar days.

ssm = boto3.client("ssm")
secretsmanager = boto3.client("secretsmanager")


# ---------------------------------------------------------
# DATABASE CONFIGURATION
# ---------------------------------------------------------

def get_parameter(name):
    parameter_name = f"{SSM_PREFIX}/db/{name}"

    logger.debug(
        "Reading database configuration parameter: %s",
        parameter_name,
    )

    response = ssm.get_parameter(
        Name=parameter_name,
        WithDecryption=True,
    )

    return response["Parameter"]["Value"]


def get_db_config():
    logger.info("Loading database configuration.")

    secret_arn = get_parameter("secret-arn")

    secret_response = secretsmanager.get_secret_value(
        SecretId=secret_arn
    )

    secret_string = secret_response.get("SecretString")

    if not secret_string:
        raise RuntimeError("Database secret is empty.")

    try:
        secret = json.loads(secret_string)
        password = secret["password"]

    except (json.JSONDecodeError, KeyError):
        password = secret_string

    if not isinstance(password, str) or not password:
        raise RuntimeError(
            "Database secret does not contain a valid password."
        )

    config = {
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

    logger.info(
        "Database configuration loaded. Database=%s Host=%s",
        config["database"],
        config["host"],
    )

    return config


def get_connection():
    logger.info("Connecting to database.")

    connection = pymysql.connect(**get_db_config())

    logger.info("Database connection established.")

    return connection


# ---------------------------------------------------------
# TIME HELPERS
# ---------------------------------------------------------

def normalize_time(value):
    """
    Normalize MySQL TIME values to Python time objects.
    """

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


def get_local_today():
    return datetime.now(
        ZoneInfo(TIMEZONE)
    ).date()


# ---------------------------------------------------------
# DOCTOR RETRIEVAL
# ---------------------------------------------------------

def get_doctors_with_shifts(cursor, doctor_id=None):
    """
    If doctor_id is provided, process only that doctor.

    If doctor_id is None, process all doctors with saved shifts.
    This supports both shift selection and scheduled execution.
    """

    if doctor_id is not None:

        logger.info(
            "Fetching saved shifts for doctor_id=%s",
            doctor_id,
        )

        cursor.execute(
            """
            SELECT DISTINCT d.doctor_id
            FROM doctor d
            INNER JOIN doctor_shift ds
                ON ds.doctor_id = d.doctor_id
            WHERE d.doctor_id = %s
            """,
            (doctor_id,),
        )

    else:

        logger.info(
            "Fetching all doctors with saved shifts."
        )

        cursor.execute(
            """
            SELECT DISTINCT d.doctor_id
            FROM doctor d
            INNER JOIN doctor_shift ds
                ON ds.doctor_id = d.doctor_id
            ORDER BY d.doctor_id
            """
        )

    doctors = cursor.fetchall()

    logger.info(
        "Doctors found for materialization: %s",
        len(doctors),
    )

    return doctors


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
        raise RuntimeError(
            f"Doctor not found: {doctor_id}"
        )


# ---------------------------------------------------------
# DOCTOR SHIFT TIMINGS
# ---------------------------------------------------------

def get_doctor_weekly_shift(cursor, doctor_id):
    logger.info(
        "Fetching weekly shift timings for doctor_id=%s",
        doctor_id,
    )

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

    rows = cursor.fetchall()

    shifts = {}

    for row in rows:

        day_of_week = int(row["day_of_week"])

        start_time = normalize_time(
            row["start_time"]
        )

        end_time = normalize_time(
            row["end_time"]
        )

        if start_time >= end_time:
            raise RuntimeError(
                f"Invalid shift timings for doctor_id={doctor_id}, "
                f"day_of_week={day_of_week}, "
                f"start_time={start_time}, "
                f"end_time={end_time}"
            )

        shifts[day_of_week] = {
            "start_time": start_time,
            "end_time": end_time,
        }

        logger.info(
            "Doctor=%s Day=%s Start=%s End=%s",
            doctor_id,
            day_of_week,
            start_time,
            end_time,
        )

    if not shifts:
        logger.warning(
            "No saved weekly shifts found for doctor_id=%s",
            doctor_id,
        )

    return shifts


# ---------------------------------------------------------
# CONSULTATION TYPES
# ---------------------------------------------------------

def get_doctor_consultation_types(cursor, doctor_id):
    logger.info(
        "Fetching active consultation types for doctor_id=%s",
        doctor_id,
    )

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

    rows = cursor.fetchall()

    consultation_types = []

    for row in rows:

        consultation_type_id = int(
            row["consultation_type_id"]
        )

        try:
            duration_decimal = Decimal(
                str(row["duration_min"])
            )

        except (InvalidOperation, TypeError):
            raise RuntimeError(
                f"Invalid consultation duration. "
                f"Doctor={doctor_id}, "
                f"ConsultationType={consultation_type_id}"
            )

        if not duration_decimal.is_finite():
            raise RuntimeError(
                f"Non-finite consultation duration. "
                f"Doctor={doctor_id}, "
                f"ConsultationType={consultation_type_id}"
            )

        if duration_decimal <= 0:
            raise RuntimeError(
                f"Consultation duration must be positive. "
                f"Doctor={doctor_id}, "
                f"ConsultationType={consultation_type_id}"
            )

        duration_seconds_decimal = (
            duration_decimal * Decimal("60")
        )

        if duration_seconds_decimal != (
            duration_seconds_decimal.to_integral_value()
        ):
            raise RuntimeError(
                f"Consultation duration must resolve to "
                f"whole seconds. Doctor={doctor_id}, "
                f"ConsultationType={consultation_type_id}"
            )

        duration_seconds = int(
            duration_seconds_decimal
        )

        try:
            capacity = int(row["capacity"])

        except (TypeError, ValueError):
            raise RuntimeError(
                f"Invalid consultation capacity. "
                f"Doctor={doctor_id}, "
                f"ConsultationType={consultation_type_id}"
            )

        if capacity <= 0:
            raise RuntimeError(
                f"Consultation capacity must be positive. "
                f"Doctor={doctor_id}, "
                f"ConsultationType={consultation_type_id}"
            )

        consultation_types.append({
            "consultation_type_id": consultation_type_id,
            "duration_seconds": duration_seconds,
            "capacity": capacity,
            "name": row["name"],
        })

        logger.info(
            "Doctor=%s ConsultationType=%s Name=%s "
            "DurationSeconds=%s Capacity=%s",
            doctor_id,
            consultation_type_id,
            row["name"],
            duration_seconds,
            capacity,
        )

    if not consultation_types:
        raise RuntimeError(
            f"No active consultation types found "
            f"for doctor_id={doctor_id}."
        )

    logger.info(
        "Active consultation types found for doctor=%s: %s",
        doctor_id,
        len(consultation_types),
    )

    return consultation_types


# ---------------------------------------------------------
# BLACKOUT DATES
# ---------------------------------------------------------

def get_blackout_dates(
    cursor,
    doctor_id,
    start_date,
    end_date,
):
    logger.info(
        "Fetching blackout dates. Doctor=%s Start=%s End=%s",
        doctor_id,
        start_date,
        end_date,
    )

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
            end_date,
        ),
    )

    blackout_dates = {
        row["blackout_date"]
        for row in cursor.fetchall()
    }

    logger.info(
        "Blackout dates found for doctor=%s: %s",
        doctor_id,
        len(blackout_dates),
    )

    for blackout_date in sorted(blackout_dates):
        logger.info(
            "Doctor=%s BlackoutDate=%s",
            doctor_id,
            blackout_date,
        )

    return blackout_dates


# ---------------------------------------------------------
# EXISTING SLOT CLEANUP
# ---------------------------------------------------------

def delete_unbooked_slots(cursor, doctor_id, slot_date):
    """
    Delete only slots without booked appointments.

    Slots referenced by appointment records are preserved,
    including cancelled or historical appointment records.
    """

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
        (
            doctor_id,
            slot_date,
        ),
    )

    deleted_count = cursor.rowcount

    if deleted_count > 0:
        logger.info(
            "Deleted existing unbooked slots. "
            "Doctor=%s Date=%s Deleted=%s",
            doctor_id,
            slot_date,
            deleted_count,
        )

    return deleted_count


# ---------------------------------------------------------
# SLOT GENERATION
# ---------------------------------------------------------

def generate_day_slots(
    slot_date,
    shift,
    consultation_types,
    doctor_id,
):
    generated_slots = []

    if not shift:
        logger.warning(
            "No shift found. Doctor=%s Date=%s",
            doctor_id,
            slot_date,
        )
        return generated_slots

    if not consultation_types:
        logger.warning(
            "No consultation types found. "
            "Doctor=%s Date=%s",
            doctor_id,
            slot_date,
        )
        return generated_slots

    working_start = shift["start_time"]
    working_end = shift["end_time"]

    if working_start >= working_end:
        raise RuntimeError(
            f"Invalid working hours. Doctor={doctor_id}, "
            f"Date={slot_date}, "
            f"Start={working_start}, End={working_end}"
        )

    current_datetime = datetime.combine(
        slot_date,
        working_start,
    )

    working_end_datetime = datetime.combine(
        slot_date,
        working_end,
    )

    consultation_index = 0

    while current_datetime < working_end_datetime:

        remaining_seconds = (
            working_end_datetime - current_datetime
        ).total_seconds()

        consultation_type = None

        # Select the next consultation type that fits
        # within the remaining working hours.

        for offset in range(len(consultation_types)):

            candidate = consultation_types[
                (
                    consultation_index + offset
                ) % len(consultation_types)
            ]

            if (
                candidate["duration_seconds"]
                <= remaining_seconds
            ):
                consultation_type = candidate

                consultation_index += offset + 1

                break

        # Nothing fits in the remaining working hours.

        if consultation_type is None:
            logger.info(
                "Remaining working time is too short "
                "for any consultation. Doctor=%s Date=%s "
                "RemainingSeconds=%s",
                doctor_id,
                slot_date,
                remaining_seconds,
            )
            break

        end_datetime = current_datetime + timedelta(
            seconds=consultation_type["duration_seconds"]
        )

        generated_slots.append({
            "doctor_id": doctor_id,
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

    logger.info(
        "Slot generation completed for doctor=%s Date=%s "
        "Start=%s End=%s Generated=%s",
        doctor_id,
        slot_date,
        working_start,
        working_end,
        len(generated_slots),
    )

    return generated_slots


# ---------------------------------------------------------
# INSERT GENERATED SLOTS
# ---------------------------------------------------------

def insert_generated_slots(
    cursor,
    doctor_id,
    generated_slots,
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
                slot["capacity"],
            ),
        )

        if cursor.rowcount == 1:
            inserted_count += 1

    logger.info(
        "Slot insertion completed. Doctor=%s "
        "Attempted=%s Inserted=%s",
        doctor_id,
        len(generated_slots),
        inserted_count,
    )

    return inserted_count


# ---------------------------------------------------------
# MATERIALIZE SLOTS FOR ONE DOCTOR
# ---------------------------------------------------------

def materialize_doctor_slots(
    connection,
    doctor_id,
    start_date,
    end_date,
):
    logger.info(
        "Starting doctor materialization. "
        "Doctor=%s Start=%s End=%s",
        doctor_id,
        start_date,
        end_date,
    )

    doctor_inserted = 0
    doctor_deleted = 0
    doctor_processed_days = 0
    doctor_blackout_days = 0

    try:

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
                start_date,
                end_date,
            )

            current_date = start_date

            while current_date <= end_date:

                logger.info(
                    "Processing doctor=%s date=%s",
                    doctor_id,
                    current_date,
                )

                deleted = delete_unbooked_slots(
                    cursor,
                    doctor_id,
                    current_date,
                )

                doctor_deleted += deleted

                # Do not generate slots on blackout dates.

                if current_date in blackout_dates:

                    doctor_blackout_days += 1

                    logger.info(
                        "Skipping blackout date. "
                        "Doctor=%s Date=%s",
                        doctor_id,
                        current_date,
                    )

                    current_date += timedelta(days=1)

                    continue

                # Python isoweekday:
                # Monday=1, Tuesday=2, ..., Sunday=7.

                weekday_number = current_date.isoweekday()

                shift = weekly_shifts.get(
                    weekday_number
                )

                if not shift:

                    logger.info(
                        "No working shift for this weekday. "
                        "Doctor=%s Date=%s Weekday=%s",
                        doctor_id,
                        current_date,
                        weekday_number,
                    )

                    current_date += timedelta(days=1)

                    continue

                generated_slots = generate_day_slots(
                    slot_date=current_date,
                    shift=shift,
                    consultation_types=consultation_types,
                    doctor_id=doctor_id,
                )

                inserted = insert_generated_slots(
                    cursor,
                    doctor_id,
                    generated_slots,
                )

                doctor_inserted += inserted
                doctor_processed_days += 1

                logger.info(
                    "Daily materialization summary. "
                    "Doctor=%s Date=%s Generated=%s "
                    "Deleted=%s Inserted=%s",
                    doctor_id,
                    current_date,
                    len(generated_slots),
                    deleted,
                    inserted,
                )

                current_date += timedelta(days=1)

            # Commit only after the entire doctor's
            # 30-day schedule has been processed.

            connection.commit()

            logger.info(
                "Database transaction committed. "
                "Doctor=%s",
                doctor_id,
            )

    except Exception:

        logger.exception(
            "Doctor materialization failed. "
            "Doctor=%s. Rolling back transaction.",
            doctor_id,
        )

        connection.rollback()

        raise

    result = {
        "doctor_id": doctor_id,
        "startDate": str(start_date),
        "endDate": str(end_date),
        "daysProcessed": doctor_processed_days,
        "blackoutDaysSkipped": doctor_blackout_days,
        "slotsDeleted": doctor_deleted,
        "slotsInserted": doctor_inserted,
    }

    logger.info(
        "Doctor materialization completed: %s",
        json.dumps(result),
    )

    return result


# ---------------------------------------------------------
# MATERIALIZE SLOTS
# ---------------------------------------------------------

def materialize_slots(doctor_id=None):
    connection = None

    total_inserted = 0
    total_deleted = 0
    total_processed_days = 0
    total_blackout_days = 0
    doctors_processed = 0
    doctor_results = []

    local_today = get_local_today()

    end_date = local_today + timedelta(
        days=GENERATION_DAYS - 1
    )

    logger.info(
        "Starting slot materialization. "
        "Environment=%s Doctor=%s Start=%s End=%s "
        "GenerationDays=%s Timezone=%s",
        ENV,
        doctor_id,
        local_today,
        end_date,
        GENERATION_DAYS,
        TIMEZONE,
    )

    try:

        connection = get_connection()

        with connection.cursor() as cursor:

            doctors = get_doctors_with_shifts(
                cursor,
                doctor_id,
            )

        if not doctors:

            logger.warning(
                "No doctors found with saved shifts. "
                "RequestedDoctor=%s",
                doctor_id,
            )

            return {
                "message": "No doctors with saved shifts found.",
                "doctor_id": doctor_id,
                "doctorsProcessed": 0,
                "daysProcessed": 0,
                "slotsInserted": 0,
            }

        for doctor in doctors:

            current_doctor_id = doctor["doctor_id"]

            logger.info(
                "Beginning materialization for doctor=%s",
                current_doctor_id,
            )

            doctor_result = materialize_doctor_slots(
                connection=connection,
                doctor_id=current_doctor_id,
                start_date=local_today,
                end_date=end_date,
            )

            doctors_processed += 1

            total_inserted += doctor_result["slotsInserted"]
            total_deleted += doctor_result["slotsDeleted"]

            total_processed_days += (
                doctor_result["daysProcessed"]
            )

            total_blackout_days += (
                doctor_result["blackoutDaysSkipped"]
            )

            doctor_results.append(doctor_result)

        result = {
            "message": "Slot materialization completed.",
            "startDate": str(local_today),
            "endDate": str(end_date),
            "generationDays": GENERATION_DAYS,
            "requestedDoctorId": doctor_id,
            "doctorsProcessed": doctors_processed,
            "daysProcessed": total_processed_days,
            "blackoutDaysSkipped": total_blackout_days,
            "slotsDeleted": total_deleted,
            "slotsInserted": total_inserted,
            "doctorResults": doctor_results,
        }

        logger.info(
            "Slot materialization completed successfully: %s",
            json.dumps(result),
        )

        return result

    except Exception:

        logger.exception(
            "Slot materialization failed. "
            "RequestedDoctor=%s",
            doctor_id,
        )

        raise

    finally:

        if connection:

            connection.close()

            logger.info(
                "Database connection closed."
            )


# ---------------------------------------------------------
# LAMBDA HANDLER
# ---------------------------------------------------------

def lambda_handler(event, context):
    request_id = getattr(
        context,
        "aws_request_id",
        "unknown",
    )

    event = event or {}

    logger.info(
        "Slot materializer invoked. "
        "RequestId=%s Event=%s",
        request_id,
        json.dumps(event, default=str),
    )

    try:

        doctor_id = event.get("doctor_id")

        # When called from the shift API, process
        # only the doctor who selected the shift.
        #
        # When called from EventBridge Scheduler
        # without doctor_id, process all doctors.

        if doctor_id is not None:

            doctor_id = str(doctor_id).strip()

            if not doctor_id:

                raise ValueError(
                    "doctor_id cannot be empty."
                )

        logger.info(
            "Materialization request accepted. "
            "RequestId=%s Doctor=%s",
            request_id,
            doctor_id,
        )

        result = materialize_slots(
            doctor_id=doctor_id
        )

        logger.info(
            "Slot materializer completed. "
            "RequestId=%s Doctor=%s",
            request_id,
            doctor_id,
        )

        return {
            "statusCode": 200,
            "headers": {
                "Content-Type": "application/json",
            },
            "body": json.dumps(result),
        }

    except OperationalError:

        logger.exception(
            "MySQL operational error. "
            "RequestId=%s",
            request_id,
        )

        return {
            "statusCode": 503,
            "body": json.dumps({
                "message": "Database temporarily unavailable.",
                "requestId": request_id,
            }),
        }

    except (ClientError, BotoCoreError):

        logger.exception(
            "AWS service error while retrieving database "
            "configuration. RequestId=%s",
            request_id,
        )

        return {
            "statusCode": 500,
            "body": json.dumps({
                "message": "Unable to retrieve database configuration.",
                "requestId": request_id,
            }),
        }

    except Exception:

        logger.exception(
            "Slot materialization failed. "
            "RequestId=%s",
            request_id,
        )

        return {
            "statusCode": 500,
            "body": json.dumps({
                "message": "Slot materialization failed.",
                "requestId": request_id,
            }),
        }