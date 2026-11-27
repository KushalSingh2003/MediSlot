import hashlib
import json
import logging
import os
import random
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

import boto3
import pymysql
from botocore.exceptions import BotoCoreError, ClientError
from pymysql.cursors import DictCursor


logger = logging.getLogger()
logger.setLevel(logging.INFO)

ENV = os.environ.get("ENV", "dev")
SSM_PREFIX = os.environ["SSM_PREFIX"].rstrip("/")
TIMEZONE = ZoneInfo(os.environ.get("TIMEZONE", "Asia/Kolkata"))

IDEMPOTENCY_TABLE_NAME = os.environ["IDEMPOTENCY_TABLE_NAME"]
EVENT_BUS_NAME = os.environ["EVENT_BUS_NAME"]

IDEMPOTENCY_TTL_SECONDS = 24 * 60 * 60
BOOKING_MAX_ATTEMPTS = 4
OUTBOX_BATCH_SIZE = 10

ssm = boto3.client("ssm")
secretsmanager = boto3.client("secretsmanager")
dynamodb = boto3.resource("dynamodb")
events = boto3.client("events")
cloudwatch = boto3.client("cloudwatch")

idempotency_table = dynamodb.Table(IDEMPOTENCY_TABLE_NAME)


class ApiError(Exception):
    def __init__(self, status_code, message):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
        },
        "body": json.dumps(body, default=str),
    }


def short_key(key):
    return f"{key[:8]}..." if key else None


def get_parameter(name):
    result = ssm.get_parameter(
        Name=f"{SSM_PREFIX}/{name}",
        WithDecryption=True,
    )
    return result["Parameter"]["Value"]


def get_connection():
    logger.info("Opening database connection.")

    secret_arn = get_parameter("db/secret-arn")
    secret_response = secretsmanager.get_secret_value(SecretId=secret_arn)
    secret_value = secret_response.get("SecretString")

    if not secret_value:
        raise RuntimeError("Database secret is empty")

    try:
        secret = json.loads(secret_value)
        password = secret["password"]
    except (json.JSONDecodeError, KeyError):
        password = secret_value

    connection = pymysql.connect(
        host=get_parameter("db/endpoint"),
        port=int(get_parameter("db/port")),
        user=get_parameter("db/username"),
        password=password,
        database=get_parameter("db/name"),
        charset="utf8mb4",
        cursorclass=DictCursor,
        connect_timeout=10,
        read_timeout=30,
        write_timeout=30,
        autocommit=False,
    )

    logger.info("Database connection established.")
    return connection


def get_method(event):
    method = event.get("httpMethod")
    if not method:
        method = (
            (event.get("requestContext") or {})
            .get("http", {})
            .get("method")
        )
    return method.upper() if isinstance(method, str) else None


def get_patient_id(event):
    authorizer = (
        (event.get("requestContext") or {})
        .get("authorizer", {})
    )
    patient_id = authorizer.get("userId")
    role = authorizer.get("role")

    if not patient_id or str(role).lower() != "patient":
        logger.warning("Patient access denied. user_id=%s role=%s", patient_id, role)
        raise ApiError(403, "Patient access is required")

    return str(patient_id)


def get_header(event, header_name):
    headers = event.get("headers") or {}
    for key, value in headers.items():
        if key.lower() == header_name.lower():
            return value
    return None


def parse_json_body(event):
    raw_body = event.get("body")
    if not isinstance(raw_body, str) or not raw_body.strip():
        raise ApiError(400, "A JSON request body is required")

    try:
        body = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise ApiError(400, "Request body must be valid JSON") from exc

    if not isinstance(body, dict):
        raise ApiError(400, "Request body must be a JSON object")

    return body


def parse_slot_id(event):
    body = parse_json_body(event)
    slot_id = body.get("slot_id")

    if isinstance(slot_id, bool):
        raise ApiError(400, "slot_id must be a positive integer")

    try:
        slot_id = int(slot_id)
    except (TypeError, ValueError) as exc:
        raise ApiError(400, "slot_id must be a positive integer") from exc

    if slot_id <= 0:
        raise ApiError(400, "slot_id must be a positive integer")

    return slot_id


def validate_idempotency_key(event):
    key = get_header(event, "Idempotency-Key")

    if not isinstance(key, str) or not key.strip():
        raise ApiError(400, "Idempotency-Key header is required")

    key = key.strip()
    if len(key) > 128:
        raise ApiError(400, "Idempotency-Key must be 128 characters or fewer")

    return key


def request_fingerprint(slot_id):
    canonical = json.dumps(
        {"slot_id": slot_id},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def metric(name, value=1, dimensions=None):
    try:
        datum = {
            "MetricName": name,
            "Unit": "Count",
            "Value": value,
        }
        if dimensions:
            datum["Dimensions"] = [
                {"Name": key, "Value": str(val)}
                for key, val in dimensions.items()
            ]

        cloudwatch.put_metric_data(
            Namespace=f"{os.environ.get('PROJECT_NAME', 'medislot')}/{ENV}/Appointments",
            MetricData=[datum],
        )
    except (BotoCoreError, ClientError):
        # Metrics are observability signals; a metrics outage must not
        # undo an already-committed booking or cancellation.
        logger.exception("Could not publish metric %s", name)


def complete_idempotency(key, status_code, body):
    idempotency_table.update_item(
        Key={"idem_key": key},
        UpdateExpression=(
            "SET #status = :completed, "
            "response_status = :response_status, "
            "response_body = :response_body, "
            "updated_at = :updated_at"
        ),
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={
            ":completed": "COMPLETED",
            ":response_status": status_code,
            ":response_body": json.dumps(body, default=str),
            ":updated_at": int(time.time()),
        },
    )
    logger.info("Idempotency completed. key=%s response_status=%s", short_key(key), status_code)


def mark_idempotency_retryable(key):
    try:
        idempotency_table.update_item(
            Key={"idem_key": key},
            UpdateExpression="SET #status = :retryable, updated_at = :now",
            ConditionExpression="#status = :in_progress",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":retryable": "RETRYABLE",
                ":in_progress": "IN_PROGRESS",
                ":now": int(time.time()),
            },
        )
        logger.warning("Idempotency key marked retryable. key=%s", short_key(key))
    except (BotoCoreError, ClientError):
        logger.exception("Could not mark idempotency key retryable")


def claim_idempotency(key, patient_id, fingerprint):
    now = int(time.time())
    item = {
        "idem_key": key,
        "patient_id": patient_id,
        "request_hash": fingerprint,
        "status": "IN_PROGRESS",
        "created_at": now,
        "updated_at": now,
        "ttl": now + IDEMPOTENCY_TTL_SECONDS,
    }

    try:
        idempotency_table.put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(idem_key) OR #ttl <= :now", # putting a new item if it doesn't exist or if the existing item has expired
            ExpressionAttributeNames={"#ttl": "ttl"}, # these are the attribute names that we are using in the condition expression, since ttl is a reserved word in dynamodb, we need to use an expression attribute name for it
            ExpressionAttributeValues={":now": now},
        )
        logger.info("Idempotency key claimed. key=%s", short_key(key))
        return "CLAIMED", None # none becuase there was no existing item, we just claimed it

    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise

    existing = idempotency_table.get_item(
        Key={"idem_key": key},
        ConsistentRead=True,
    ).get("Item")

    if not existing:
        logger.warning("Idempotency item expired between check and read; retrying claim. key=%s", short_key(key))
        return claim_idempotency(key, patient_id, fingerprint)

    if (
        existing.get("patient_id") != patient_id
        or existing.get("request_hash") != fingerprint
    ):
        logger.warning("Idempotency key reuse mismatch. key=%s patient_id=%s", short_key(key), patient_id)
        raise ApiError(
            409,
            "Idempotency-Key was already used for a different request",
        )

    if existing.get("status") == "COMPLETED":
        logger.info("Idempotency replay of completed request. key=%s", short_key(key))
        return "COMPLETED", existing

    if existing.get("status") == "RETRYABLE":
        try:
            idempotency_table.update_item(
                Key={"idem_key": key},
                UpdateExpression=(
                    "SET #status = :in_progress, "
                    "updated_at = :now, #ttl = :ttl"
                ),
                ConditionExpression="#status = :retryable",
                ExpressionAttributeNames={
                    "#status": "status",
                    "#ttl": "ttl",
                },
                ExpressionAttributeValues={
                    ":in_progress": "IN_PROGRESS",
                    ":retryable": "RETRYABLE",
                    ":now": now,
                    ":ttl": now + IDEMPOTENCY_TTL_SECONDS,
                },
            )
            logger.info("Idempotency key re-claimed after earlier failure. key=%s", short_key(key))
            return "CLAIMED", None
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise

    logger.info("Idempotency key already in progress. key=%s", short_key(key))
    return "IN_PROGRESS", existing


def replay_response(item):
    return response(
        int(item["response_status"]),
        json.loads(item["response_body"]),
    )


def get_existing_booking(connection, patient_id, idempotency_key):
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                a.appointment_id,
                a.slot_id,
                a.status,
                a.amount,
                s.slot_date,
                s.start_time,
                s.end_time,
                s.doctor_id,
                d.name AS doctor_name,
                s.consultation_type_id
            FROM appointments a
            JOIN appointment_slot s
                ON s.slot_id = a.slot_id
            JOIN doctor d
                ON d.doctor_id = s.doctor_id
            WHERE a.patient_id = %s
            AND a.booking_reference = %s
            LIMIT 1
            
            """,
            (patient_id, idempotency_key),
        )
        return cursor.fetchone()


def appointment_body(row):
    return {
        "appointment_id": row["appointment_id"],
        "slot_id": row["slot_id"],
        "doctor_id": row.get("doctor_id"),
        "consultation_type_id": row.get("consultation_type_id"),
        "status": row["status"],
        "appointment_date": str(row.get("slot_date")),
        "start_time": str(row.get("start_time")),
        "end_time": str(row.get("end_time")),
        "amount": str(row["amount"]),
        "doctor_name": row.get("doctor_name"),
    }


def recover_idempotent_booking(connection, key, patient_id):
    existing = get_existing_booking(connection, patient_id, key)
    if not existing:
        return None

    body = appointment_body(existing)
    complete_idempotency(key, 201, body)
    logger.info("Recovered committed booking. key=%s appointment_id=%s", short_key(key), existing["appointment_id"])
    return response(201, body)


def insert_outbox_event(cursor, detail_type, detail):
    event_id = str(uuid.uuid4())
    detail = dict(detail)
    detail["eventId"] = event_id

    cursor.execute(
        """
        INSERT INTO event_outbox (
            event_id,
            source,
            detail_type,
            detail_json
        )
        VALUES (%s, %s, %s, %s)
        """,
        (
            event_id,
            "medislot.appointments",
            detail_type,
            json.dumps(detail, default=str),
        ),
    )
    logger.info("Outbox event queued. event_id=%s type=%s", event_id, detail_type)


def dispatch_outbox(connection):
    """
    Publish pending outbox events. Configure a scheduled EventBridge
    invocation of this same Lambda (for example, rate(1 minute)) so
    pending events are retried even when no API requests arrive.
    """
    connection.begin()

    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT event_id, source, detail_type, detail_json
                FROM event_outbox
                WHERE published_at IS NULL
                ORDER BY created_at
                LIMIT %s
                FOR UPDATE SKIP LOCKED
                """,
                (OUTBOX_BATCH_SIZE,),
            )
            rows = cursor.fetchall()

            if not rows:
                connection.commit()
                return

            logger.info("Dispatching outbox events. count=%d", len(rows))
            published_count = 0
            failed_count = 0

            entries = []
            for row in rows:
                detail = row["detail_json"]
                if isinstance(detail, str):
                    detail = json.loads(detail)

                entries.append({
                    "EventBusName": EVENT_BUS_NAME,
                    "Source": row["source"],
                    "DetailType": row["detail_type"],
                    "Detail": json.dumps(detail, default=str),
                })

            result = events.put_events(Entries=entries)
            failed = result.get("Entries", [])

            for index, row in enumerate(rows):
                entry_result = failed[index] if index < len(failed) else {}
                if entry_result.get("ErrorCode"):
                    logger.error(
                        "Event delivery failed: event_id=%s code=%s",
                        row["event_id"],
                        entry_result.get("ErrorCode"),
                    )
                    failed_count += 1
                    continue

                cursor.execute(
                    """
                    UPDATE event_outbox
                    SET published_at = UTC_TIMESTAMP()
                    WHERE event_id = %s
                    """,
                    (row["event_id"],),
                )
                published_count += 1

        connection.commit()
        logger.info("Outbox dispatch finished. published=%d failed=%d", published_count, failed_count)

    except Exception:
        connection.rollback()
        logger.exception("Outbox dispatch failed; events remain pending")


def load_refund_policy():
    value = get_parameter("refunds/policy")
    try:
        policy = json.loads(value)
    except json.JSONDecodeError as exc:
        logger.error("Refund policy SSM parameter is not valid JSON")
        raise RuntimeError("Refund policy SSM parameter is invalid JSON") from exc

    logger.info("Refund policy loaded.")
    return policy


def patient_refund_percent(hours_before, policy):
    cancellation = policy["patient_cancellation"]
    thresholds = cancellation["refund_percentages"]

    if hours_before > Decimal(
        str(cancellation["full_refund_above_hours"])
    ):
        return int(thresholds["above_48_hours"]), "FULL"

    if hours_before >= Decimal(
        str(cancellation["refund_75_at_or_above_hours"])
    ):
        return int(thresholds["from_24_to_48_hours"]), "PARTIAL_75"

    if hours_before >= Decimal(
        str(cancellation["refund_50_at_or_above_hours"])
    ):
        return int(thresholds["from_6_to_under_24_hours"]), "PARTIAL_50"

    return int(thresholds["under_6_hours"]), "NONE"


def booking_response(connection, patient_id, key):
    existing = get_existing_booking(connection, patient_id, key)
    if not existing:
        raise RuntimeError("Committed appointment could not be loaded")
    return appointment_body(existing)


def book_appointment(connection, patient_id, slot_id, key):
    last_conflict = False

    for attempt in range(BOOKING_MAX_ATTEMPTS):
        connection.begin()
        logger.info("Booking attempt. patient_id=%s slot_id=%s attempt=%d/%d",
                    patient_id, slot_id, attempt + 1, BOOKING_MAX_ATTEMPTS)

        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        s.slot_id,
                        s.doctor_id,
                        s.consultation_type_id,
                        s.slot_date,
                        s.start_time,
                        s.end_time,
                        s.capacity,
                        s.booked_count,
                        s.version,
                        s.status,
                        dct.fee
                    FROM appointment_slot s
                    JOIN doctor_consultation_type dct
                      ON dct.doctor_id = s.doctor_id
                     AND dct.consultation_type_id = s.consultation_type_id
                     AND dct.active = TRUE
                    WHERE s.slot_id = %s
                    """,
                    (slot_id,),
                )
                slot = cursor.fetchone()

                if not slot:
                    raise ApiError(404, "Available slot not found")

                appointment_start = (
                    datetime.combine(
                        slot["slot_date"],
                        datetime.min.time(),
                        tzinfo=TIMEZONE,
                    )
                    + slot["start_time"]
                )

                if appointment_start <= datetime.now(TIMEZONE):
                    raise ApiError(409, "This slot is in the past")

                if (
                    slot["status"] != "AVAILABLE"
                    or slot["booked_count"] >= slot["capacity"]
                ):
                    raise ApiError(409, "This slot is full or unavailable")

                cursor.execute(
                    """
                    UPDATE appointment_slot
                    SET booked_count = booked_count + 1,
                        version = version + 1
                    WHERE slot_id = %s
                      AND version = %s
                      AND status = 'AVAILABLE'
                      AND booked_count < capacity
                    """,
                    (slot_id, slot["version"]),
                )

                if cursor.rowcount != 1:
                    logger.warning("Optimistic lock conflict. slot_id=%s version=%s attempt=%d",
                                   slot_id, slot["version"], attempt + 1)
                    connection.rollback()
                    last_conflict = True
                    metric("BookingConflictRetries", 1)
                    if attempt + 1 < BOOKING_MAX_ATTEMPTS:
                        time.sleep(
                            min(0.025 * (2 ** attempt), 0.2)
                            + random.uniform(0, 0.025)
                        )
                        continue
                    break

                cursor.execute(
                    """
                    INSERT INTO appointments (
                        slot_id,
                        patient_id,
                        status,
                        amount,
                        booking_reference
                    )
                    VALUES (%s, %s, 'BOOKED', %s, %s)
                    """,
                    (slot_id, patient_id, slot["fee"], key),
                )
                appointment_id = cursor.lastrowid
                logger.info("Appointment row inserted. appointment_id=%s slot_id=%s amount=%s",
                            appointment_id, slot_id, slot["fee"])

                insert_outbox_event(
                    cursor,
                    "AppointmentBooked",
                    {
                        "appointmentId": appointment_id,
                        "patientId": patient_id,
                        "doctorId": slot["doctor_id"],
                        "slotId": slot_id,
                        "consultationTypeId": slot["consultation_type_id"],
                        "appointmentStart": appointment_start.isoformat(),
                    },
                )

            connection.commit()
            logger.info("Booking committed. appointment_id=%s patient_id=%s", appointment_id, patient_id)
            return booking_response(connection, patient_id, key)

        except ApiError:
            connection.rollback()
            raise
        except Exception:
            logger.error("Booking transaction failed; rolling back. slot_id=%s attempt=%d", slot_id, attempt + 1)
            connection.rollback()
            raise

    if last_conflict:
        logger.warning("Booking attempts exhausted due to contention. slot_id=%s", slot_id)
        # Re-read after the last optimistic-write conflict.
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT capacity, booked_count, status
                FROM appointment_slot
                WHERE slot_id = %s
                """,
                (slot_id,),
            )
            current = cursor.fetchone()

        if (
            not current
            or current["status"] != "AVAILABLE"
            or current["booked_count"] >= current["capacity"]
        ):
            raise ApiError(409, "This slot is full or unavailable")

        raise ApiError(409, "Booking contention; retry using the same key")

    raise ApiError(409, "Unable to reserve this slot")


def handle_booking(event, patient_id, connection):
    slot_id = parse_slot_id(event)
    key = validate_idempotency_key(event) # validate the idempotency key from the request header
    fingerprint = request_fingerprint(slot_id) # it helps us bind an idepotnecy key to a sepecific slot_id, so that if the same key is used for a different slot_id, we can detect it and return an error
    logger.info("Booking request received. patient_id=%s slot_id=%s key=%s",
                patient_id, slot_id, short_key(key))

    claim_status, item = claim_idempotency(
        key,
        patient_id,
        fingerprint,
    )

    if claim_status == "COMPLETED":
        return replay_response(item)

    if claim_status == "IN_PROGRESS":
        recovered = recover_idempotent_booking(
            connection,
            key,
            patient_id,
        )
        if recovered:
            return recovered
        return response(
            409,
            {"message": "This booking request is still processing; retry with the same key"},
        )

    try:
        body = book_appointment(connection, patient_id, slot_id, key)
        complete_idempotency(key, 201, body)
        return response(201, body)

    except ApiError as exc:
        logger.warning("Booking rejected. status=%s message=%s slot_id=%s key=%s",
                       exc.status_code, exc.message, slot_id, short_key(key))
        body = {"message": exc.message}
        complete_idempotency(key, exc.status_code, body)
        return response(exc.status_code, body)

    except Exception:
        logger.error("Booking failed unexpectedly; attempting recovery. slot_id=%s key=%s",
                     slot_id, short_key(key))
        # The RDS transaction rolled back, or its result can be recovered
        # by querying the unique booking_reference on the next retry.
        recovered = recover_idempotent_booking(
            connection,
            key,
            patient_id,
        )
        if recovered:
            return recovered
        mark_idempotency_retryable(key)
        raise


def list_appointments(connection, patient_id):
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                a.appointment_id,
                a.slot_id,
                a.status,
                a.amount,
                a.created_at,
                s.doctor_id,
                s.consultation_type_id,
                s.slot_date,
                s.start_time,
                s.end_time
            FROM appointments a
            JOIN appointment_slot s ON s.slot_id = a.slot_id
            WHERE a.patient_id = %s
            ORDER BY s.slot_date DESC, s.start_time DESC
            LIMIT 100
            """,
            (patient_id,),
        )
        rows = cursor.fetchall()

    logger.info("Appointments listed. patient_id=%s count=%d", patient_id, len(rows))
    return response(200, {
        "appointments": [appointment_body(row) for row in rows],
    })


def get_appointment(connection, patient_id, appointment_id):
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                a.appointment_id,
                a.slot_id,
                a.status,
                a.amount,
                a.created_at,
                s.doctor_id,
                s.consultation_type_id,
                s.slot_date,
                s.start_time,
                s.end_time
            FROM appointments a
            JOIN appointment_slot s ON s.slot_id = a.slot_id
            WHERE a.appointment_id = %s
              AND a.patient_id = %s
            """,
            (appointment_id, patient_id),
        )
        row = cursor.fetchone()

    if not row:
        logger.warning("Appointment not found. appointment_id=%s patient_id=%s", appointment_id, patient_id)
        raise ApiError(404, "Appointment not found")

    return response(200, {"appointment": appointment_body(row)})


def cancel_appointment(connection, patient_id, appointment_id):
    logger.info("Cancellation requested. patient_id=%s appointment_id=%s", patient_id, appointment_id)

    policy = load_refund_policy()
    connection.begin()

    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    a.appointment_id,
                    a.slot_id,
                    a.patient_id,
                    a.status,
                    a.amount,
                    s.doctor_id,
                    s.slot_date,
                    s.start_time,
                    s.end_time
                FROM appointments a
                JOIN appointment_slot s ON s.slot_id = a.slot_id
                WHERE a.appointment_id = %s
                  AND a.patient_id = %s
                FOR UPDATE
                """,
                (appointment_id, patient_id),
            )
            appointment = cursor.fetchone()

            if not appointment:
                raise ApiError(404, "Appointment not found")

            if appointment["status"] == "CANCELLED":
                cursor.execute(
                    """
                    SELECT refund_percent, refund_amount
                    FROM refund_ledger
                    WHERE appointment_id = %s
                    """,
                    (appointment_id,),
                )
                existing_refund = cursor.fetchone()
                logger.info("Cancellation is a repeat; returning existing refund. appointment_id=%s", appointment_id)
                connection.commit()
                return response(200, {
                    "message": "Appointment is already cancelled",
                    "appointment_id": appointment_id,
                    "refund": existing_refund,
                })

            if appointment["status"] != "BOOKED":
                raise ApiError(409, "Appointment cannot be cancelled")

            appointment_start = (
                datetime.combine(
                    appointment["slot_date"],
                    datetime.min.time(),
                    tzinfo=TIMEZONE,
                )
                + appointment["start_time"]
            )
            now = datetime.now(TIMEZONE)

            if appointment_start <= now:
                raise ApiError(409, "Past appointments cannot be cancelled")

            hours_before = Decimal(
                str((appointment_start - now).total_seconds())
            ) / Decimal("3600")

            refund_percent, tier = patient_refund_percent(
                hours_before,
                policy,
            )
            amount = Decimal(str(appointment["amount"]))
            refund_amount = (
                amount * Decimal(refund_percent) / Decimal("100")
            ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

            logger.info("Refund calculated. appointment_id=%s hours_before=%.1f tier=%s percent=%s amount=%s refund=%s",
                        appointment_id, hours_before, tier, refund_percent, amount, refund_amount)

            cursor.execute(
                """
                UPDATE appointments
                SET status = 'CANCELLED',
                    updated_at = CURRENT_TIMESTAMP
                WHERE appointment_id = %s
                  AND patient_id = %s
                  AND status = 'BOOKED'
                """,
                (appointment_id, patient_id),
            )
            if cursor.rowcount != 1:
                raise ApiError(409, "Appointment was already changed")

            cursor.execute(
                """
                UPDATE appointment_slot
                SET booked_count = booked_count - 1,
                    version = version + 1
                WHERE slot_id = %s
                  AND booked_count > 0
                """,
                (appointment["slot_id"],),
            )
            if cursor.rowcount != 1:
                logger.error("Could not release slot seat. slot_id=%s appointment_id=%s",
                             appointment["slot_id"], appointment_id)
                raise RuntimeError("Could not release the reserved slot seat")

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
                    appointment_id,
                    patient_id,
                    refund_percent,
                    refund_amount,
                    f"PATIENT_CANCELLED_{tier}",
                ),
            )
            logger.info("Refund ledger entry created. appointment_id=%s refund_amount=%s", appointment_id, refund_amount)

            common_detail = {
                "appointmentId": appointment_id,
                "patientId": patient_id,
                "doctorId": appointment["doctor_id"],
                "slotId": appointment["slot_id"],
                "refundAmount": str(refund_amount),
                "refundPercent": refund_percent,
                "refundTier": tier,
            }

            insert_outbox_event(
                cursor,
                "AppointmentCancelled",
                common_detail,
            )
            insert_outbox_event(
                cursor,
                "SlotSeatFreed",
                {
                    "appointmentId": appointment_id,
                    "patientId": patient_id,
                    "doctorId": appointment["doctor_id"],
                    "slotId": appointment["slot_id"],
                    "slotDate": str(appointment["slot_date"]),
                    "startTime": str(appointment["start_time"]),
                    "endTime": str(appointment["end_time"]),
                },
            )

        connection.commit()
        logger.info("Cancellation committed. appointment_id=%s slot_id=%s", appointment_id, appointment["slot_id"])

    except ApiError:
        connection.rollback()
        raise
    except Exception:
        logger.error("Cancellation transaction failed; rolling back. appointment_id=%s", appointment_id)
        connection.rollback()
        raise

    metric(
        "CancellationsByTier",
        1,
        {"RefundTier": tier},
    )

    return response(200, {
        "message": "Appointment cancelled",
        "appointment_id": appointment_id,
        "refund_percent": refund_percent,
        "refund_amount": str(refund_amount),
        "refund_tier": tier,
    })


def list_refunds(connection, patient_id):
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                r.refund_id,
                r.appointment_id,
                r.refund_percent,
                r.refund_amount,
                r.reason,
                r.created_at
            FROM refund_ledger r
            WHERE r.patient_id = %s
            ORDER BY r.created_at DESC
            LIMIT 100
            """,
            (patient_id,),
        )
        rows = cursor.fetchall()

    logger.info("Refunds listed. patient_id=%s count=%d", patient_id, len(rows))
    return response(200, {"refunds": rows})


def dispatch_outbox_event(event):
    connection = get_connection()
    try:
        dispatch_outbox(connection)
    finally:
        connection.close()

    return response(200, {"message": "Outbox dispatch completed"})


def lambda_handler(event, context):
    request_id = getattr(context, "aws_request_id", "unknown")
    logger.info("Patient appointments request: request_id=%s", request_id)

    # Configure an EventBridge schedule to invoke this same Lambda
    # periodically so pending outbox events are retried.
    if event.get("source") == "aws.events":
        logger.info("Scheduled outbox dispatch triggered. request_id=%s", request_id)
        return dispatch_outbox_event(event)

    connection = None

    try:
        method = get_method(event)
        if method not in ("GET", "POST", "DELETE"):
            logger.warning("Method not allowed. method=%s request_id=%s", method, request_id)
            return response(405, {"message": "Method not allowed"})

        patient_id = get_patient_id(event)
        path_parameters = event.get("pathParameters") or {}
        appointment_id = path_parameters.get("appointmentId")
        logger.info("Request authorised. method=%s patient_id=%s appointment_id=%s request_id=%s",
                    method, patient_id, appointment_id, request_id)

        connection = get_connection()

        if method == "POST":
            result = handle_booking(event, patient_id, connection)
        elif method == "DELETE":
            if not appointment_id:
                raise ApiError(400, "appointmentId path parameter is required")
            result = cancel_appointment(
                connection,
                patient_id,
                int(appointment_id),
            )
        elif appointment_id:
            result = get_appointment(
                connection,
                patient_id,
                int(appointment_id),
            )
        elif "/refunds" in (
            event.get("resource")
            or event.get("path")
            or event.get("rawPath")
            or ""
        ):
            result = list_refunds(connection, patient_id)
        else:
            result = list_appointments(connection, patient_id)

        logger.info("Request completed. method=%s status=%s request_id=%s",
                    method, result["statusCode"], request_id)

        # Try to deliver queued events immediately. The scheduled
        # invocation retries any events still pending.
        dispatch_outbox(connection)
        return result

    except ApiError as exc:
        logger.warning("Request rejected. status=%s message=%s request_id=%s",
                       exc.status_code, exc.message, request_id)
        return response(exc.status_code, {"message": exc.message})

    except (pymysql.MySQLError, BotoCoreError, ClientError):
        logger.exception("Patient appointment service error: request_id=%s", request_id)
        return response(500, {"message": "Unable to process the request"})

    except Exception:
        logger.exception("Unexpected patient appointment error: request_id=%s", request_id)
        return response(500, {"message": "An unexpected error occurred"})

    finally:
        if connection:
            connection.close()