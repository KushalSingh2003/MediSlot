import os
import json
import time
import base64
import hashlib
import secrets
import logging
from uuid import uuid4

import boto3
import pymysql
import bcrypt
import jwt

from botocore.exceptions import ClientError


# ============================================================
# LOGGING
# ============================================================

logger = logging.getLogger()
logger.setLevel(logging.INFO)


# ============================================================
# AWS CLIENTS
# ============================================================

ssm = boto3.client("ssm")
secretsmanager = boto3.client("secretsmanager")
dynamodb = boto3.resource("dynamodb")


# ============================================================
# ENVIRONMENT VARIABLES
# ============================================================

ENV = os.environ.get("ENV", "dev")
SSM_PREFIX = os.environ.get("SSM_PREFIX", f"/medislot/{ENV}")

REFRESH_TOKENS_TABLE = os.environ["REFRESH_TOKENS_TABLE"]

refresh_table = dynamodb.Table(REFRESH_TOKENS_TABLE)


# ============================================================
# HTTP RESPONSE
# ============================================================

def response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
            "Pragma": "no-cache"
        },
        "body": json.dumps(body)
    }


# ============================================================
# SSM PARAMETER STORE
# ============================================================

def get_parameter(parameter_path):
    """
    Retrieve a parameter using its complete SSM path.
    """

    result = ssm.get_parameter(
        Name=parameter_path,
        WithDecryption=True
    )

    return result["Parameter"]["Value"]


def get_db_parameter(name):
    return get_parameter(f"{SSM_PREFIX}/db/{name}")


# ============================================================
# SECRETS MANAGER
# ============================================================

def get_secret_value(secret_arn):
    """
    Retrieve a secret from Secrets Manager.
    Supports plain-text secrets and JSON secrets.
    """

    result = secretsmanager.get_secret_value(
        SecretId=secret_arn
    )

    secret_string = result.get("SecretString")

    if secret_string is None:
        raise ValueError(
            "Expected a text-based Secrets Manager secret"
        )

    return secret_string


def get_db_password():
    """
    The database secret ARN is stored in SSM.
    The database password is stored in Secrets Manager.
    """

    secret_arn = get_db_parameter("secret-arn")
    secret_string = get_secret_value(secret_arn)

    try:
        secret_json = json.loads(secret_string)

        if isinstance(secret_json, dict):
            password = secret_json.get("password")

            if password is None:
                raise ValueError(
                    "Database secret does not contain 'password'"
                )

            return password

    except json.JSONDecodeError:
        # Support a plain-text database password.
        pass

    return secret_string


def get_jwt_secret():
    """
    Correct SSM path:
    /medislot/dev/auth/jwt_signing_secret_arn

    The SSM parameter contains the Secrets Manager ARN.
    """

    secret_arn = get_parameter(
        f"{SSM_PREFIX}/auth/jwt_signing_secret_arn"
    )

    secret_string = get_secret_value(secret_arn)

    try:
        secret_json = json.loads(secret_string)

        if isinstance(secret_json, dict):
            # Change this key if your secret uses another name.
            jwt_secret = secret_json.get("jwt_secret")

            if not isinstance(jwt_secret, str) or not jwt_secret:
                raise ValueError(
                    "JWT secret JSON must contain a non-empty "
                    "'jwt_secret' field"
                )

            return jwt_secret

    except json.JSONDecodeError:
        # The JWT secret is stored as plain text.
        pass

    if not secret_string:
        raise ValueError("JWT signing secret is empty")

    return secret_string


# ============================================================
# TOKEN CONFIGURATION
# ============================================================

def get_jwt_issuer():
    return get_parameter(
        f"{SSM_PREFIX}/auth/jwt_issuer"
    )


def get_access_token_ttl():
    value = get_parameter(
        f"{SSM_PREFIX}/auth/access_token_ttl_minutes"
    )

    minutes = int(value)

    if minutes <= 0:
        raise ValueError("Invalid access-token lifetime")

    return minutes * 60


def get_refresh_token_ttl():
    value = get_parameter(
        f"{SSM_PREFIX}/auth/refresh_token_ttl_days"
    )

    days = int(value)

    if days <= 0:
        raise ValueError("Invalid refresh-token lifetime")

    return days * 24 * 60 * 60


# ============================================================
# RDS MYSQL CONNECTION
# ============================================================

def get_db_connection():
    endpoint = get_db_parameter("endpoint")
    port = int(get_db_parameter("port"))
    database = get_db_parameter("name")
    username = get_db_parameter("username")
    password = get_db_password()

    return pymysql.connect(
        host=endpoint,
        port=port,
        user=username,
        password=password,
        database=database,
        connect_timeout=10,
        read_timeout=30,
        write_timeout=30,
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor
    )


# ============================================================
# REQUEST PARSING
# ============================================================

def parse_body(event):
    body = event.get("body")

    if not body:
        raise ValueError("Request body is required")

    if event.get("isBase64Encoded", False):
        body = base64.b64decode(body).decode("utf-8")

    if isinstance(body, str):
        body = json.loads(body)

    if not isinstance(body, dict):
        raise ValueError("Request body must be a JSON object")

    email = body.get("email")
    password = body.get("password")

    if not isinstance(email, str):
        raise ValueError("Email is required")

    if not isinstance(password, str):
        raise ValueError("Password is required")

    email = email.strip().lower()

    if not email or not password:
        raise ValueError("Email and password are required")

    if len(email) > 255 or len(password) > 1024:
        raise ValueError("Invalid request")

    return email, password


# ============================================================
# BCRYPT PASSWORD VERIFICATION
# ============================================================

def verify_password(password, stored_hash):
    """
    Verifies a password against the bcrypt hash
    created by the registration Lambda.
    """

    if not isinstance(stored_hash, (str, bytes)):
        return False

    try:
        if isinstance(stored_hash, str):
            stored_hash = stored_hash.encode("utf-8")

        return bcrypt.checkpw(
            password.encode("utf-8"),
            stored_hash
        )

    except (ValueError, TypeError, AttributeError):
        return False


# ============================================================
# USER LOOKUP
# ============================================================

def find_user(connection, email, role):
    """
    Query only the table associated with the requested role.
    """

    if role == "doctor":
        query = """
            SELECT
                doctor_id AS user_id,
                password_hash,
                name
            FROM doctors
            WHERE email = %s
            LIMIT 1
        """

    elif role == "patient":
        query = """
            SELECT
                patient_id AS user_id,
                password_hash,
                first_name AS name
            FROM patients
            WHERE email = %s
            LIMIT 1
        """

    else:
        raise ValueError("Unsupported user role")

    with connection.cursor() as cursor:
        cursor.execute(query, (email,))
        return cursor.fetchone()


# ============================================================
# ACCESS TOKEN GENERATION
# ============================================================

def create_access_token(user_id, role, jwt_secret, issuer, ttl_seconds):
    now = int(time.time())

    payload = {
        "sub": str(user_id),
        "role": role,
        "iss": issuer,
        "iat": now,
        "exp": now + ttl_seconds,
        "jti": str(uuid4())
    }

    return jwt.encode(
        payload,
        jwt_secret,
        algorithm="HS256"
    )


# ============================================================
# REFRESH TOKEN GENERATION
# ============================================================

def create_refresh_token(user_id, role, ttl_seconds):
    raw_token = secrets.token_urlsafe(32)

    token_hash = hashlib.sha256(
        raw_token.encode("utf-8")
    ).hexdigest()

    expires_at = int(time.time()) + ttl_seconds

    print(f"Creating refresh token for user_id={user_id}, role={role}")
    print(f"Target DynamoDB table: {REFRESH_TOKENS_TABLE}")

    try:
        refresh_table.put_item(
            Item={
                "token_hash": token_hash,
                "userId": str(user_id),
                "role": role,
                "expiresAt": expires_at
            },
            ConditionExpression="attribute_not_exists(token_hash)"
        )

        print("Refresh token saved successfully to DynamoDB")

    except Exception as e:
        print(f"Failed to save refresh token: {type(e).__name__}: {e}")
        raise

    return raw_token


# ============================================================
# LOGIN
# ============================================================

def login(event, role):
    connection = None

    try:
        # 1. Parse credentials.
        email, password = parse_body(event)

        # 2. Connect to RDS MySQL.
        connection = get_db_connection()

        # 3. Retrieve the user from the role-specific table.
        user = find_user(
            connection,
            email,
            role
        )

        # 4. Validate credentials.
        # Use the same message for unknown email and wrong password.
        if not user or not verify_password(
            password,
            user["password_hash"]
        ):
            return response(
                401,
                {
                    "message": "Invalid email or password"
                }
            )

        # 5. Retrieve JWT configuration.
        jwt_secret = get_jwt_secret()
        issuer = get_jwt_issuer()
        access_ttl_seconds = get_access_token_ttl()
        refresh_ttl_seconds = get_refresh_token_ttl()

        # 6. Generate the JWT access token.
        access_token = create_access_token(
            user_id=user["user_id"],
            role=role,
            jwt_secret=jwt_secret,
            issuer=issuer,
            ttl_seconds=access_ttl_seconds
        )

        # 7. Generate and store the refresh-token hash.
        refresh_token = create_refresh_token(
            user_id=user["user_id"],
            role=role,
            ttl_seconds=refresh_ttl_seconds
        )

        # 8. Return the tokens.
        return response(
            200,
            {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "token_type": "Bearer",
                "expires_in": access_ttl_seconds
            }
        )

    except (ValueError, json.JSONDecodeError):
        return response(
            400,
            {
                "message": "Invalid request"
            }
        )

    except ClientError:
        logger.exception(
            "AWS service error during login"
        )

        return response(
            500,
            {
                "message": "Internal server error"
            }
        )

    except pymysql.MySQLError:
        logger.exception(
            "RDS database error during login"
        )

        return response(
            500,
            {
                "message": "Internal server error"
            }
        )

    except Exception:
        logger.exception(
            "Unexpected login error"
        )

        return response(
            500,
            {
                "message": "Internal server error"
            }
        )

    finally:
        if connection:
            connection.close()


# ============================================================
# LAMBDA HANDLER
# ============================================================

def lambda_handler(event, context):
    method = event.get("httpMethod")

    # Support API Gateway HTTP API payload format 2.0.
    if not method:
        method = (
            event.get("requestContext", {})
            .get("http", {})
            .get("method")
        )

    path = event.get("path") or event.get("rawPath", "")
    path = path.rstrip("/")

    if method != "POST":
        return response(
            405,
            {
                "message": "Method not allowed"
            }
        )

    if path == "/auth/doctors/login":
        return login(event, "doctor")

    if path == "/auth/patients/login":
        return login(event, "patient")

    return response(
        404,
        {
            "message": "Route not found"
        }
    )