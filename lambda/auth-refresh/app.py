
import os
import json
import time
import hashlib
import secrets
import logging
import boto3
import pymysql
import jwt

from botocore.exceptions import ClientError
from pymysql.cursors import DictCursor

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ssm = boto3.client("ssm")
secrets_manager = boto3.client("secretsmanager")
dynamodb = boto3.resource("dynamodb")

ENV = os.environ["ENV"]
SSM_PREFIX = os.environ["SSM_PREFIX"]
REFRESH_TOKENS_TABLE = os.environ["REFRESH_TOKENS_TABLE"]

refresh_table = dynamodb.Table(REFRESH_TOKENS_TABLE)


# ---------- Helpers ----------

def response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json"
        },
        "body": json.dumps(body)
    }


def get_parameter(name):
    result = ssm.get_parameter(
        Name=f"{SSM_PREFIX}/{name}",
        WithDecryption=True
    )
    return result["Parameter"]["Value"]


def get_jwt_secret():
    secret_arn = get_parameter("auth/jwt_signing_secret_arn")

    result = secrets_manager.get_secret_value(
        SecretId=secret_arn
    )

    secret_value = result.get("SecretString", "")

    try:
        secret_json = json.loads(secret_value)
        return secret_json["jwt_secret"]
    except (json.JSONDecodeError, TypeError):
        return secret_value


def get_db_connection():
    endpoint = get_parameter("db/endpoint")
    port = int(get_parameter("db/port"))
    database = get_parameter("db/name")
    username = get_parameter("db/username")
    secret_arn = get_parameter("db/secret-arn")

    result = secrets_manager.get_secret_value(
        SecretId=secret_arn
    )

    secret_value = result["SecretString"]

    try:
        db_secret = json.loads(secret_value)
        password = db_secret["password"]
    except (json.JSONDecodeError, TypeError):
        password = secret_value

    return pymysql.connect(
        host=endpoint,
        port=port,
        user=username,
        password=password,
        database=database,
        cursorclass=DictCursor,
        connect_timeout=5,
        read_timeout=5,
        write_timeout=5,
        autocommit=True
    )


def hash_refresh_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_access_token(user_id, role, secret, issuer, ttl_minutes):
    now = int(time.time())

    payload = {
        "sub": str(user_id),
        "role": role,
        "iss": issuer,
        "iat": now,
        "exp": now + ttl_minutes * 60,
        "jti": secrets.token_urlsafe(16)
    }

    return jwt.encode(
        payload,
        secret,
        algorithm="HS256"
    )


# ---------- Handler ----------

def lambda_handler(event, context):
    connection = None

    try:
        # 1. Read refresh token from request
        body = event.get("body") or "{}"

        if isinstance(body, str):
            body = json.loads(body)

        refresh_token = body.get("refresh_token")

        if not refresh_token:
            return response(400, {
                "message": "refresh_token is required"
            })

        # Debug refresh-token lookup
        token_hash = hash_refresh_token(refresh_token)

        logger.info("Refresh token hash calculated")

        result = refresh_table.get_item(
            Key={"token_hash": token_hash},
            ConsistentRead=True
        )

        token_record = result.get("Item")

        logger.info(
            "Refresh token lookup result: %s",
            "FOUND" if token_record else "NOT FOUND"
        )

        if not token_record:
            return response(401, {
                "message": "Invalid or expired refresh token"
            })

        logger.info(
            "Token metadata: userId=%s, role=%s, expiresAt=%s",
            token_record.get("userId"),
            token_record.get("role"),
            token_record.get("expiresAt")
        )
        



        # 3. Check expiry explicitly; DynamoDB TTL deletion is asynchronous
        # 3. Check refresh-token expiry
        now = int(time.time())
        token_expiry = int(token_record.get("expiresAt", 0))

        if token_expiry <= now:
            refresh_table.delete_item(
                Key={"token_hash": token_hash}
            )
            return response(401, {
                "message": "Invalid or expired refresh token"
            })

        

        # 4. Read JWT configuration
        issuer = get_parameter("auth/jwt_issuer")
        access_ttl = int(
            get_parameter("auth/access_token_ttl_minutes")
        )
        
        jwt_secret = get_jwt_secret()

        # 5. Verify that the user still exists in RDS
        user_id = str(token_record["userId"])
        role = token_record["role"]
        

        if role not in ("doctor", "patient"):
            return response(401, {
                "message": "Invalid refresh token"
            })

        connection = get_db_connection()

        with connection.cursor() as cursor:
            if role == "doctor":
                cursor.execute(
                    """
                    SELECT doctor_id
                    FROM doctors
                    WHERE doctor_id = %s
                    """,
                    (user_id,)
                )
            else:
                cursor.execute(
                    """
                    SELECT patient_id
                    FROM patients
                    WHERE patient_id = %s
                    """,
                    (user_id,)
                )

            user = cursor.fetchone()

        if not user:
            refresh_table.delete_item(
                Key={"token_hash": token_hash}
            )
            return response(401, {
                "message": "User no longer exists"
            })

        # 6. Generate replacement tokens
        new_access_token = generate_access_token(
            user_id=user_id,
            role=role,
            secret=jwt_secret,
            issuer=issuer,
            ttl_minutes=access_ttl
        )

       
       
        

        # 8. Return the new tokens
        return response(200, {
            "message": "Token refreshed successfully",
            "access_token": new_access_token,
            
            "token_type": "Bearer",
            "expires_in": access_ttl * 60
        })

    except ClientError as exc:
        logger.exception("AWS service error during token refresh")
        return response(500, {
            "message": "Unable to refresh token"
        })

    except Exception:
        logger.exception("Unexpected error during token refresh")
        return response(500, {
            "message": "Unable to refresh token"
        })

    finally:
        if connection:
            connection.close()