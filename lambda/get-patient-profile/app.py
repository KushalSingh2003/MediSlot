import json
import os
import logging

import boto3
import pymysql

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ssm = boto3.client("ssm")
secrets = boto3.client("secretsmanager")

ENV = os.environ["ENV"]
SSM_PREFIX = os.environ["SSM_PREFIX"].rstrip("/")


def get_db_credentials():
    """Retrieve database configuration from SSM and credentials from Secrets Manager."""

    response = ssm.get_parameters(
        Names=[
            f"{SSM_PREFIX}/db/endpoint",
            f"{SSM_PREFIX}/db/port",
            f"{SSM_PREFIX}/db/name",
            f"{SSM_PREFIX}/db/secret-arn"
        ],
        WithDecryption=True
    )

    parameters = {
        item["Name"].rsplit("/", 1)[-1]: item["Value"]
        for item in response["Parameters"]
    }

    if response.get("InvalidParameters"):
        raise RuntimeError(
            f"Missing SSM parameters: {response['InvalidParameters']}"
        )

    required_parameters = ["endpoint", "port", "name", "secret-arn"]

    missing = [
        key for key in required_parameters
        if key not in parameters
    ]

    if missing:
        raise RuntimeError(
            f"Required database parameters are missing: {missing}"
        )

    secret_response = secrets.get_secret_value(
        SecretId=parameters["secret-arn"]
    )

    secret = json.loads(secret_response["SecretString"])

    return {
        "host": parameters["endpoint"],
        "port": int(parameters["port"]),
        "dbname": parameters["name"],
        "username": secret["username"],
        "password": secret["password"]
    }


def get_db_connection():
    """Create a MySQL connection."""

    credentials = get_db_credentials()

    return pymysql.connect(
        host=credentials["host"],
        port=credentials["port"],
        user=credentials["username"],
        password=credentials["password"],
        database=credentials["dbname"],
        connect_timeout=5,
        read_timeout=5,
        write_timeout=5,
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True
    )


def response(status_code, body):
    """Build an API Gateway Lambda proxy response."""

    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json"
        },
        "body": json.dumps(body, default=str)
    }


def lambda_handler(event, context):
    connection = None

    try:
        # Identity is supplied by the API Gateway Lambda Authorizer.
        authorizer = (
            event.get("requestContext", {})
            .get("authorizer", {})
        )

        user_id = authorizer.get("userId")
        role = authorizer.get("role")

        if not user_id or not role:
            return response(401, {
                "message": "Unauthorized"
            })

        if role != "patient":
            return response(403, {
                "message": "Only patients can access this profile"
            })

        connection = get_db_connection()

        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    patient_id,
                    first_name,
                    last_name,
                    email,
                    phone,
                    city,
                    date_of_birth,
                    gender,
                    created_at,
                    updated_at
                FROM patients
                WHERE patient_id = %s
                LIMIT 1
                """,
                (user_id,)
            )

            patient = cursor.fetchone()

        if not patient:
            return response(404, {
                "message": "Patient profile not found"
            })

        return response(200, {
            "message": "Patient profile retrieved successfully",
            "profile": patient
        })

    except Exception:
        logger.exception("Failed to retrieve patient profile")

        return response(500, {
            "message": "Unable to retrieve patient profile"
        })

    finally:
        if connection:
            connection.close()