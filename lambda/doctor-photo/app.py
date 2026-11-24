import json
import os
import uuid
import logging
import boto3

from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client("s3")
sts = boto3.client("sts")
print("Signing identity:", sts.get_caller_identity())

PHOTO_BUCKET = os.environ["PHOTOS_BUCKET"]

ALLOWED_CONTENT_TYPES = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}


def response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
        },
        "body": json.dumps(body),
    }


def lambda_handler(event, context):
    try:
        # Get identity from the existing Lambda Authorizer.
        request_context = event.get("requestContext") or {}
        authorizer = request_context.get("authorizer") or {}

        # Supports REST API Lambda Authorizer context.
        doctor_id = authorizer.get("userId")
        role = authorizer.get("role")

        if not doctor_id:
            return response(401, {
                "message": "Unauthorized."
            })
        logger.info("Authorizer context: %s", json.dumps(authorizer))
        logger.info("Doctor ID: %s", authorizer.get("userId"))
        logger.info("Role: %s", authorizer.get("role"))

        if not role or role.lower() != "doctor":
            return response(403, {
                "message": "Only doctors can upload profile photos."
            })

        # Parse request body.
        body = event.get("body") or "{}"

        if event.get("isBase64Encoded"):
            import base64
            body = base64.b64decode(body).decode("utf-8")

        if isinstance(body, str):
            body = json.loads(body)

        if not isinstance(body, dict):
            return response(400, {
                "message": "Request body must be a JSON object."
            })

        content_type = body.get("contentType")

        if content_type not in ALLOWED_CONTENT_TYPES:
            return response(400, {
                "message": (
                    "Unsupported image type. "
                    "Use image/jpeg, image/png, or image/webp."
                )
            })

        # Generate a unique S3 object key.
        extension = ALLOWED_CONTENT_TYPES[content_type]
        photo_key = (
            f"doctors/{doctor_id}/profile/{uuid.uuid4()}.{extension}"
        )

        # Generate a URL valid for 5 minutes.
        upload_url = s3.generate_presigned_url(
            ClientMethod="put_object",
            Params={
                "Bucket": PHOTO_BUCKET,
                "Key": photo_key,
                "ContentType": content_type,
            },
            ExpiresIn=300,
            HttpMethod="PUT",
        )

        return response(200, {
            "message": "Upload URL generated successfully.",
            "uploadUrl": upload_url,
            "photoKey": photo_key,
            "contentType": content_type,
            "expiresIn": 300,
        })

    except json.JSONDecodeError:
        return response(400, {
            "message": "Request body must contain valid JSON."
        })

    except ClientError:
        logger.exception("Failed to generate photo upload URL.")
        return response(500, {
            "message": "Could not generate upload URL."
        })

    except Exception:
        logger.exception("Unexpected error generating upload URL.")
        return response(500, {
            "message": "An internal error occurred."
        })