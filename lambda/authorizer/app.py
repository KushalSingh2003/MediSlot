import os
import json
import time
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

TOKEN_DENYLIST_TABLE = os.environ["TOKEN_DENYLIST_TABLE"]
denylist_table = dynamodb.Table(TOKEN_DENYLIST_TABLE)


# ============================================================
# CONFIGURATION
# ============================================================

ALLOWED_ROLES = {"doctor", "patient"}


# ============================================================
# SSM PARAMETER STORE
# ============================================================

def get_parameter(name):
    logger.info("Reading SSM parameter: %s", name)

    result = ssm.get_parameter(
        Name=name,
        WithDecryption=True
    )

    logger.info("SSM parameter retrieved successfully: %s", name)
    return result["Parameter"]["Value"]


# ============================================================
# JWT SIGNING SECRET
# ============================================================

def get_jwt_secret():
    secret_arn = get_parameter(
        f"{SSM_PREFIX}/auth/jwt_signing_secret_arn"
    )

    logger.info("Retrieving JWT signing secret from Secrets Manager")

    result = secretsmanager.get_secret_value(
        SecretId=secret_arn
    )

    secret_string = result.get("SecretString")

    if not secret_string:
        logger.error("JWT signing secret is empty")
        raise ValueError("JWT signing secret is empty")

    # Support JSON secrets and plain-text secrets.
    try:
        secret_json = json.loads(secret_string)

        if isinstance(secret_json, dict):
            secret_string = secret_json.get("jwt_secret")

            if not isinstance(secret_string, str) or not secret_string:
                logger.error("JWT secret JSON is missing jwt_secret")
                raise ValueError("Invalid JWT secret configuration")

    except json.JSONDecodeError:
        # The secret is stored as plain text.
        pass

    logger.info("JWT signing secret retrieved successfully")
    return secret_string


# ============================================================
# JWT ISSUER
# ============================================================

def get_jwt_issuer():
    return get_parameter(
        f"{SSM_PREFIX}/auth/jwt_issuer"
    )


# ============================================================
# TOKEN EXTRACTION
# ============================================================

def get_access_token(event):
    headers = event.get("headers") or {}

    authorization = next(
        (
            value
            for key, value in headers.items()
            if key.lower() == "authorization"
        ),
        None
    )

    if not isinstance(authorization, str):
        logger.warning("Authorization header is missing")
        return None

    scheme, separator, token = authorization.partition(" ")

    if (
        not separator
        or scheme.lower() != "bearer"
        or not token.strip()
    ):
        logger.warning("Authorization header has an invalid format")
        return None

    logger.info("Bearer token extracted successfully")
    return token.strip()


# ============================================================
# JWT VALIDATION
# ============================================================

def validate_access_token(token):
    logger.info("Starting access-token validation")

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

    user_id = claims.get("sub")
    role = claims.get("role")
    jti = claims.get("jti")

    if not isinstance(user_id, str) or not user_id:
        raise jwt.InvalidTokenError("Invalid subject")

    if not isinstance(jti, str) or not jti:
        raise jwt.InvalidTokenError("Invalid token identifier")

    if role not in ALLOWED_ROLES:
        raise jwt.InvalidTokenError("Invalid role")

    logger.info(
        "JWT validated: user_id=%s role=%s",
        user_id,
        role
    )

    return claims


# ============================================================
# TOKEN DENYLIST CHECK
# ============================================================

def is_token_revoked(jti):
    logger.info("Checking token denylist")

    result = denylist_table.get_item(
        Key={"jti": jti},
        ConsistentRead=True
    )

    item = result.get("Item")

    if not item:
        logger.info("Token is not present in denylist")
        return False

    expires_at = int(item.get("expiresAt", 0))

    if expires_at <= int(time.time()):
        logger.info("Expired denylist entry found; token is expired")
        return True

    logger.warning("Token has been revoked")
    return True


# ============================================================
# API GATEWAY IAM POLICY
# ============================================================

def generate_policy(principal_id, effect, resource, claims=None):
    policy = {
        "principalId": principal_id,
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Action": "execute-api:Invoke",
                    "Effect": effect,
                    "Resource": resource
                }
            ]
        }
    }

    if claims:
        # API Gateway authorizer context values must be scalar.
        policy["context"] = {
            "userId": str(claims["sub"]),
            "role": claims["role"],
            "jti": claims["jti"]
        }

    return policy


# ============================================================
# LAMBDA HANDLER
# ============================================================

def lambda_handler(event, context):
    request_id = getattr(context, "aws_request_id", "unknown")

    # REST API Lambda authorizers receive methodArn.
    resource = event.get("methodArn")

    logger.info(
        "Authorizer invoked: request_id=%s resource=%s",
        request_id,
        resource
    )

    if not resource:
        logger.error("Missing methodArn in authorizer event")
        raise Exception("Unauthorized")

    try:
        # 1. Extract token.
        token = get_access_token(event)

        if not token:
            logger.warning("Authorization denied: no valid Bearer token")
            raise Exception("Unauthorized")

        # 2. Validate JWT.
        try:
            claims = validate_access_token(token)

        except jwt.ExpiredSignatureError:
            logger.warning("Authorization denied: access token expired")
            raise Exception("Unauthorized")

        except jwt.InvalidTokenError:
            logger.warning("Authorization denied: JWT validation failed")
            raise Exception("Unauthorized")

        # 3. Check revocation.
        if is_token_revoked(claims["jti"]):
            logger.warning(
                "Authorization denied: revoked token, user_id=%s",
                claims["sub"]
            )
            raise Exception("Unauthorized")

        # 4. Allow this API method and pass identity to the backend.
        policy = generate_policy(
            principal_id=claims["sub"],
            effect="Allow",
            resource=resource,
            claims=claims
        )

        logger.info(
            "Authorization allowed: user_id=%s role=%s request_id=%s",
            claims["sub"],
            claims["role"],
            request_id
        )

        return policy

    except Exception as exc:
        # Never log the JWT or secret.
        if isinstance(exc, ClientError):
            logger.exception(
                "AWS error during authorization: request_id=%s",
                request_id
            )
            # Fail closed if AWS cannot verify token revocation.
            raise Exception("Unauthorized")

        if str(exc) == "Unauthorized":
            raise

        logger.exception(
            "Unexpected authorizer error: request_id=%s",
            request_id
        )
        raise Exception("Unauthorized")