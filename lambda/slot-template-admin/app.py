
import json
import logging
import os

import boto3
import pymysql
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# --------------------------------------------------
# AWS CLIENTS AND CONFIGURATION
# --------------------------------------------------

ssm = boto3.client("ssm")
secretsmanager = boto3.client("secretsmanager")

ENVIRONMENT = os.environ.get("ENVIRONMENT", "dev")
PARAMETER_PREFIX = f"/medislot/{ENVIRONMENT}/db"

TEMPLATE_NAME = "UNIVERSAL_TEMPLATE"
WORKING_START_TIME = os.environ.get("WORKING_START_TIME", "09:00:00")
WORKING_END_TIME = os.environ.get("WORKING_END_TIME", "17:00:00")

# MySQL convention: Monday = 1, Friday = 5
WORKING_DAYS = range(1, 6)

# --------------------------------------------------
# PREDEFINED CONSULTATION TYPES
# --------------------------------------------------

CONSULTATION_TYPES = [
    {
        "name": "Quick Consult",
        "duration_min": 15,
        "capacity": 1,
    },
    {
        "name": "Standard Consult",
        "duration_min": 30,
        "capacity": 1,
    },
    {
        "name": "Detailed Consult",
        "duration_min": 60,
        "capacity": 1,
    },
    {
        "name": "Health Camp Session",
        "duration_min": 60,
        "capacity": 2,
    },
]

# --------------------------------------------------
# DATABASE CONNECTION
# --------------------------------------------------

def get_parameter(name):
    response = ssm.get_parameter(
        Name=f"{PARAMETER_PREFIX}/{name}",
        WithDecryption=True,
    )
    return response["Parameter"]["Value"]


def get_db_connection():
    logger.info("Reading database configuration")

    endpoint = get_parameter("endpoint")
    port = int(get_parameter("port"))
    username = get_parameter("username")
    database = get_parameter("name")
    secret_arn = get_parameter("secret-arn")

    logger.info("Retrieving database credentials")

    secret_response = secretsmanager.get_secret_value(
        SecretId=secret_arn
    )
    secret = json.loads(secret_response["SecretString"])

    logger.info("Connecting to RDS MySQL")

    return pymysql.connect(
        host=endpoint,
        port=port,
        user=username,
        password=secret["password"],
        database=database,
        connect_timeout=10,
        read_timeout=30,
        write_timeout=30,
        autocommit=False,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
    )


# --------------------------------------------------
# RESPONSE HELPERS
# --------------------------------------------------

def response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
        },
        "body": json.dumps(body, default=str),
    }


# --------------------------------------------------
# VALIDATE WORKING HOURS
# --------------------------------------------------

def validate_working_hours(cursor):
    cursor.execute(
        """
        SELECT
            TIME(%s) AS start_time,
            TIME(%s) AS end_time
        """,
        (WORKING_START_TIME, WORKING_END_TIME),
    )

    result = cursor.fetchone()

    if (
        not result
        or result["start_time"] is None
        or result["end_time"] is None
        or result["start_time"] >= result["end_time"]
    ):
        raise ValueError(
            "Invalid working hours. Configure valid HH:MM:SS "
            "values with end time later than start time."
        )


# --------------------------------------------------
# SEED PREDEFINED CONSULTATION TYPES
# --------------------------------------------------

def seed_consultation_types(cursor):
    """
    Ensure the four predefined consultation types exist.

    The existing consultation_type table does not contain a capacity
    column. Capacity is therefore configured when appointment slots
    are generated, not stored in this table.
    It checks whether the consultation type already exists. If it does, it verifies that the duration matches the predefined value. If the duration differs, it raises a ValueError to indicate a conflict. If the consultation type does not exist, it inserts a new record into the consultation_type table with the specified name and duration.
    """

    consultation_type_ids = {}

    for consultation_type in CONSULTATION_TYPES:
        cursor.execute(
            """
            SELECT id, duration_min
            FROM consultation_type
            WHERE name = %s
            FOR UPDATE
            """,
            (consultation_type["name"],),
        )

        existing = cursor.fetchone()

        if existing:
            # Keep the existing record and its duration unchanged.
            if float(existing["duration_min"]) != float(
                consultation_type["duration_min"]
            ):
                raise ValueError(
                    f"Consultation type '{consultation_type['name']}' "
                    "already exists with a different duration. "
                    "Resolve the schema data before initializing "
                    "the universal template."
                )

            consultation_type_ids[consultation_type["name"]] = (
                existing["id"]
            )
            continue

        cursor.execute(
            """
            INSERT INTO consultation_type (name, duration_min)
            VALUES (%s, %s)
            """,
            (
                consultation_type["name"],
                consultation_type["duration_min"],
            ),
        )

        consultation_type_ids[consultation_type["name"]] = (
            cursor.lastrowid
        )

    return consultation_type_ids


# --------------------------------------------------
# CREATE OR RETRIEVE UNIVERSAL TEMPLATE
# --------------------------------------------------

def get_or_create_template(cursor):
    cursor.execute(
        """
        SELECT
            template_id,
            name,
            is_active,
            working_start_time,
            working_end_time
        FROM slot_template
        WHERE name = %s
        FOR UPDATE
        """,
        (TEMPLATE_NAME,),
    )

    template = cursor.fetchone()

    if template:
        return template, False

    cursor.execute(
        """
        INSERT INTO slot_template (
            name,
            is_active,
            working_start_time,
            working_end_time
        )
        VALUES (%s, TRUE, %s, %s)
        """,
        (
            TEMPLATE_NAME,
            WORKING_START_TIME,
            WORKING_END_TIME,
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

    return cursor.fetchone(), True


# --------------------------------------------------
# CREATE MONDAY-FRIDAY CONSULTATION RULES
# --------------------------------------------------

def create_consultation_rules(cursor, template_id, type_ids):
    rules_created = 0

    for day_of_week in WORKING_DAYS:
        for consultation_type in CONSULTATION_TYPES:
            consultation_type_id = type_ids[
                consultation_type["name"]
            ]

            cursor.execute(
                """
                INSERT INTO template_consultation_rule (
                    template_id,
                    day_of_week,
                    consultation_type_id,
                    allocation_weight,
                    time_period,
                    is_active
                )
                VALUES (%s, %s, %s, 1, 'ANY', TRUE)
                ON DUPLICATE KEY UPDATE
                    rule_id = rule_id
                """,
                (
                    template_id,
                    day_of_week,
                    consultation_type_id,
                ),
            )

            if cursor.rowcount == 1:
                rules_created += 1

    return rules_created


# --------------------------------------------------
# LAMBDA HANDLER
# --------------------------------------------------

def lambda_handler(event, context):
    logger.info("Initializing universal slot template")

    connection = None

    try:
        connection = get_db_connection()

        with connection.cursor() as cursor:
            validate_working_hours(cursor)

            # 1. Ensure predefined consultation types exist.
            type_ids = seed_consultation_types(cursor)

            # 2. Create or retrieve the universal template.
            template, template_created = get_or_create_template(
                cursor
            )

            # 3. Generate Monday-Friday consultation rules.
            rules_created = create_consultation_rules(
                cursor,
                template["template_id"],
                type_ids,
            )

        connection.commit()

        logger.info(
            "Template initialized successfully: template_id=%s",
            template["template_id"],
        )

        return response(
            200,
            {
                "message": (
                    "Universal template created successfully."
                    if template_created
                    else "Universal template already exists; "
                         "missing consultation rules were added."
                ),
                "template_id": template["template_id"],
                "template_name": template["name"],
                "template_created": template_created,
                "working_start_time": str(
                    template["working_start_time"]
                ),
                "working_end_time": str(
                    template["working_end_time"]
                ),
                "consultation_types": [
                    {
                        "name": item["name"],
                        "consultation_type_id": type_ids[item["name"]],
                        "duration_min": item["duration_min"],
                        "default_slot_capacity": item["capacity"],
                    }
                    for item in CONSULTATION_TYPES
                ],
                "working_days": [
                    "Monday",
                    "Tuesday",
                    "Wednesday",
                    "Thursday",
                    "Friday",
                ],
                "rules_created": rules_created,
            },
        )

    except ValueError as exc:
        if connection:
            connection.rollback()

        logger.warning("Initialization validation failed: %s", exc)

        return response(
            400,
            {"message": str(exc)},
        )

    except (ClientError, pymysql.MySQLError):
        if connection:
            connection.rollback()

        logger.exception("Template initialization failed")

        return response(
            500,
            {"message": "Template initialization failed."},
        )

    except Exception:
        if connection:
            connection.rollback()

        logger.exception("Unexpected initialization error")

        return response(
            500,
            {"message": "An unexpected error occurred."},
        )

    finally:
        if connection:
            connection.close()
