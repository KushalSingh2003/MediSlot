import os
import json
import time
import base64
import hashlib
import logging

import boto3
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
TOKEN_DENYLIST_TABLE = os.environ["TOKEN_DENYLIST_TABLE"]

refresh_table = dynamodb.Table(REFRESH_TOKENS_TABLE)
denylist_table = dynamodb.Table(TOKEN_DENYLIST_TABLE)


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

def get_parameter(name):
    result = ssm.get_parameter(
        Name=name,
        WithDecryption=True
    )

    return result["Parameter"]["Value"]


# ============================================================
# SECRETS MANAGER
# ============================================================

def get_jwt_secret():
    """
    SSM parameter:
    /medislot/dev/auth/jwt_signing_secret_arn

    Its value must be the Secrets Manager ARN.
    """

    secret_arn = get_parameter(
        f"{SSM_PREFIX}/auth/jwt_signing_secret_arn"
    )

    result = secretsmanager.get_secret_value(
        SecretId=secret_arn
    )

    secret_string = result.get("SecretString")

    if not secret_string:
        raise ValueError("JWT secret is empty")

    # Support either a plain-text secret or JSON.
    try:
        secret_json = json.loads(secret_string)

        if isinstance(secret_json, dict):
            secret_string = secret_json.get("jwt_secret")

            if not isinstance(secret_string, str) or not secret_string:
                raise ValueError(
                    "JWT secret JSON must contain 'jwt_secret'"
                )

    except json.JSONDecodeError:
        pass

    return secret_string


# ============================================================
# JWT ISSUER
# ============================================================

def get_jwt_issuer():
    return get_parameter(
        f"{SSM_PREFIX}/auth/jwt_issuer"
    )


# ============================================================
# REQUEST PARSING
# ============================================================

def parse_body(event):
    body = event.get("body")

    if not body:
        return {}

    if event.get("isBase64Encoded", False):
        body = base64.b64decode(body).decode("utf-8")

    if isinstance(body, str):
        body = json.loads(body)

    if not isinstance(body, dict):
        raise ValueError("Request body must be a JSON object")

    return body


def get_access_token(event):
    headers = event.get("headers") or {}

    # HTTP headers are case-insensitive.
    authorization = next(
        (
            value
            for key, value in headers.items()
            if key.lower() == "authorization"
        ),
        None
    )

    if not isinstance(authorization, str):
        return None

    scheme, separator, token = authorization.partition(" ")

    if (
        not separator
        or scheme.lower() != "bearer"
        or not token.strip()
    ):
        return None

    return token.strip()


# ============================================================
# TOKEN VALIDATION
# ============================================================

def validate_access_token(token):
    """
    Validate signature, issuer and expiry before trusting
    claims such as jti, sub and role.
    """

    jwt_secret = get_jwt_secret()
    issuer = get_jwt_issuer()

    claims = jwt.decode(
        token,
        jwt_secret,
        algorithms=["HS256"],
        issuer=issuer,
        options={
            "require": [
                "sub",
                "iss",
                "exp",
                "iat",
                "jti",
                "role"
            ]
        }
    )

    if claims.get("role") not in ("doctor", "patient"):
        raise jwt.InvalidTokenError("Invalid role")

    return claims


# ============================================================
# REFRESH TOKEN REVOCATION
# ============================================================

def revoke_refresh_token(raw_refresh_token):
    """
    Refresh-token table partition key: token_hash.

    Only the SHA-256 hash is stored in DynamoDB.
    """

    token_hash = hashlib.sha256(
        raw_refresh_token.encode("utf-8")
    ).hexdigest()

    refresh_table.delete_item(
        Key={
            "token_hash": token_hash
        }
    )


# ============================================================
# ACCESS TOKEN REVOCATION
# ============================================================

def revoke_access_token(claims):
    """
    Store the JWT identifier (jti) in the denylist.

    ASSUMPTION:
    The denylist table partition key is 'jti'.

    The authorizer must check this table before accepting
    an access token.
    """

    jti = claims["jti"]
    expires_at = int(claims["exp"])

    # Do not store an already-expired token.
    if expires_at <= int(time.time()):
        return

    denylist_table.put_item(
        Item={
            "jti": jti,
            "expiresAt": expires_at,
            "userId": str(claims["sub"]),
            "role": claims["role"]
        }
    )


# ============================================================
# LOGOUT
# ============================================================

def logout(event):
    try:
        # 1. Read the access token from Authorization header.
        access_token = get_access_token(event)

        if not access_token:
            return response(
                401,
                {"message": "Valid access token required"}
            )

        # 2. Validate the access token.
        try:
            claims = validate_access_token(access_token)

        except jwt.ExpiredSignatureError:
            return response(
                401,
                {"message": "Access token has expired"}
            )

        except jwt.InvalidTokenError:
            return response(
                401,
                {"message": "Invalid access token"}
            )

        # 3. Parse the refresh token.
        body = parse_body(event)
        refresh_token = body.get("refresh_token")

        if not isinstance(refresh_token, str) or not refresh_token:
            return response(
                400,
                {"message": "refresh_token is required"}
            )

        # 4. Revoke the refresh token.
        revoke_refresh_token(refresh_token)

        # 5. Revoke the access token until its expiry.
        revoke_access_token(claims)

        # 6. Tell the web application that logout succeeded.
        # The web app must destroy its own server-side session.
        return response(
            200,
            {"message": "Logout successful"}
        )

    except ClientError:
        logger.exception("AWS service error during logout")

        return response(
            500,
            {"message": "Internal server error"}
        )

    except (ValueError, json.JSONDecodeError):
        return response(
            400,
            {"message": "Invalid request"}
        )

    except Exception:
        logger.exception("Unexpected error during logout")

        return response(
            500,
            {"message": "Internal server error"}
        )


# ============================================================
# LAMBDA HANDLER
# ============================================================

def lambda_handler(event, context):
    method = event.get("httpMethod")

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
            {"message": "Method not allowed"}
        )

    if path not in ("/auth/logout", "/auth/logout/"):
        return response(
            404,
            {"message": "Route not found"}
        )

    return logout(event)