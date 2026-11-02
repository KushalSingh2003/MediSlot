import os
import json
import logging
from pathlib import Path

import boto3
import pymysql

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ssm = boto3.client("ssm")
secretsmanager = boto3.client("secretsmanager")

ENVIRONMENT = os.environ.get("ENVIRONMENT", "dev")
PARAMETER_PREFIX = f"/medislot/{ENVIRONMENT}/db"


def get_parameter(name):
    """Retrieve a database configuration parameter from SSM."""
    response = ssm.get_parameter(
        Name=f"{PARAMETER_PREFIX}/{name}",
        WithDecryption=True
    )
    return response["Parameter"]["Value"]


def get_db_connection():
    logger.info("Reading database endpoint")
    endpoint = get_parameter("endpoint")

    logger.info("Reading database port")
    port = int(get_parameter("port"))

    logger.info("Reading database username")
    username = get_parameter("username")

    logger.info("Reading secret ARN")
    secret_arn = get_parameter("secret-arn")

    logger.info("Retrieving database secret")
    secret_response = secretsmanager.get_secret_value(
        SecretId=secret_arn
    )
    secret = json.loads(secret_response["SecretString"])

    logger.info("Connecting to RDS")
    return pymysql.connect(
        host=endpoint,
        port=port,
        user=username,
        password=secret["password"],
        database=get_parameter("name"),
        connect_timeout=10,
        read_timeout=30,
        write_timeout=30,
        autocommit=False
    )


def load_schema():
    """Read schema.sql included in the Lambda deployment package."""
    schema_path = Path(__file__).parent / "schema.sql"

    if not schema_path.exists():
        raise FileNotFoundError(
            f"schema.sql not found at {schema_path}"
        )

    return schema_path.read_text(encoding="utf-8")


def lambda_handler(event, context):
    connection = None

    try:
        schema = load_schema()
        connection = get_db_connection()

        with connection.cursor() as cursor:
            statements = [
                statement.strip()
                for statement in schema.split(";")
                if statement.strip()
            ]

            for statement in statements:
                cursor.execute(statement)

        connection.commit()

        logger.info("Database schema migration completed successfully.")

        return {
            "statusCode": 200,
            "body": "Database tables created successfully."
        }

    except Exception:
        if connection:
            connection.rollback()

        logger.exception("Database migration failed.")
        raise

    finally:
        if connection:
            connection.close()
