"""Patient self-service profile CRUD. Identity comes only from API Gateway authorizer."""
import json
import logging
import os
import re
from datetime import date

import boto3
import pymysql

logger = logging.getLogger()
logger.setLevel(logging.INFO)
ssm = boto3.client("ssm")
secrets = boto3.client("secretsmanager")
SSM_PREFIX = os.environ["SSM_PREFIX"].rstrip("/")


def db_connection():
    names = [f"{SSM_PREFIX}/db/{key}" for key in ("endpoint", "port", "name", "secret-arn")]
    result = ssm.get_parameters(Names=names, WithDecryption=True)
    params = {p["Name"].rsplit("/", 1)[-1]: p["Value"] for p in result["Parameters"]}
    if result.get("InvalidParameters") or len(params) != 4:
        raise RuntimeError("Database configuration is incomplete")
    secret = json.loads(secrets.get_secret_value(SecretId=params["secret-arn"])["SecretString"])
    return pymysql.connect(host=params["endpoint"], port=int(params["port"]),
        user=secret.get("username", params.get("username")), password=secret["password"],
        database=params["name"], connect_timeout=5, read_timeout=10, write_timeout=10,
        cursorclass=pymysql.cursors.DictCursor, autocommit=False)


def response(code, body):
    return {"statusCode": code, "headers": {"Content-Type": "application/json"},
            "body": json.dumps(body, default=str)}


def parse_body(event):
    raw = event.get("body")
    if not raw:
        return {}
    if event.get("isBase64Encoded"):
        raise ValueError("Base64 request bodies are not supported")
    body = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(body, dict):
        raise ValueError("Request body must be a JSON object")
    return body


def lambda_handler(event, context):
    connection = None
    try:
        auth = (event.get("requestContext") or {}).get("authorizer") or {}
        patient_id, role = auth.get("userId"), auth.get("role")
        if not patient_id or not role:
            return response(401, {"message": "Unauthorized"})
        if str(role).lower() != "patient":
            return response(403, {"message": "Patient access required"})

        method = (event.get("httpMethod") or "").upper()
        connection = db_connection()

        if method == "GET":
            with connection.cursor() as cur:
                cur.execute("""SELECT patient_id, name, email, phone, city, date_of_birth,
                    gender, emergency_contact, created_at, updated_at
                    FROM patients WHERE patient_id=%s LIMIT 1""", (patient_id,))
                patient = cur.fetchone()
            if not patient:
                return response(404, {"message": "Patient profile not found"})
            return response(200, {"message": "Patient profile retrieved successfully", "profile": patient})

        if method == "PUT":
            body = parse_body(event)
            mapping = {"name": "name", "phone": "phone", "city": "city",
                       "dateOfBirth": "date_of_birth", "gender": "gender",
                       "emergencyContact": "emergency_contact"}
            unknown = set(body) - set(mapping)
            if unknown:
                return response(400, {"message": "Unsupported fields", "fields": sorted(unknown)})
            if not body:
                return response(400, {"message": "At least one field is required"})
            updates, values = [], []
            for api_field, db_field in mapping.items():
                if api_field not in body:
                    continue
                value = body[api_field]
                if value is not None and not isinstance(value, str):
                    return response(400, {"message": f"{api_field} must be a string or null"})
                if isinstance(value, str):
                    value = value.strip() or None
                if api_field == "name" and (not value or len(value) > 255):
                    return response(400, {"message": "name is required and must be at most 255 characters"})
                if api_field in ("phone", "emergencyContact") and value and not re.fullmatch(r"[+0-9() .-]{7,30}", value):
                    return response(400, {"message": f"Invalid {api_field}"})
                if api_field == "dateOfBirth" and value:
                    try:
                        date.fromisoformat(value)
                    except ValueError:
                        return response(400, {"message": "dateOfBirth must use YYYY-MM-DD"})
                if api_field == "gender" and value and len(value) > 20:
                    return response(400, {"message": "gender must be at most 20 characters"})
                if api_field == "city" and value and len(value) > 100:
                    return response(400, {"message": "city must be at most 100 characters"})
                updates.append(f"{db_field}=%s")
                values.append(value)
            values.append(patient_id)
            with connection.cursor() as cur:
                cur.execute(f"UPDATE patients SET {', '.join(updates)} WHERE patient_id=%s", values)
                if cur.rowcount == 0:
                    cur.execute("SELECT patient_id FROM patients WHERE patient_id=%s", (patient_id,))
                    if not cur.fetchone():
                        connection.rollback()
                        return response(404, {"message": "Patient profile not found"})
                cur.execute("""SELECT patient_id, name, email, phone, city, date_of_birth,
                    gender, emergency_contact, created_at, updated_at
                    FROM patients WHERE patient_id=%s""", (patient_id,))
                profile = cur.fetchone()
            connection.commit()
            return response(200, {"message": "Patient profile updated successfully", "profile": profile})

        if method == "DELETE":
            with connection.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS total FROM appointments WHERE patient_id=%s", (patient_id,))
                count = cur.fetchone()["total"]
                if count:
                    return response(409, {"message": "Profile cannot be deleted while appointment records exist"})
                cur.execute("DELETE FROM patients WHERE patient_id=%s", (patient_id,))
                if cur.rowcount == 0:
                    return response(404, {"message": "Patient profile not found"})
            connection.commit()
            return response(200, {"message": "Patient profile deleted successfully"})

        return response(405, {"message": "Method not allowed"})
    except (ValueError, json.JSONDecodeError) as exc:
        if connection:
            connection.rollback()
        return response(400, {"message": str(exc)})
    except Exception:
        if connection:
            connection.rollback()
        logger.exception("Patient profile operation failed")
        return response(500, {"message": "Unable to process patient profile request"})
    finally:
        if connection:
            connection.close()
