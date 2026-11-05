# import os
# import json
# import boto3
# import pymysqlimport boto3
# import os

# s3_client = boto3.client("s3")


# ssm = boto3.client("ssm")
# secretsmanager = boto3.client("secretsmanager")

# ENVIRONMENT = os.environ.get("ENVIRONMENT", "dev")
# PARAMETER_PREFIX = f"/medislot/{ENVIRONMENT}/db"


# def get_parameter(name):
#     response = ssm.get_parameter(
#         Name=f"{PARAMETER_PREFIX}/{name}",
#         WithDecryption=True
#     )
#     return response["Parameter"]["Value"]
# def generate_profile_photo_url(event):
#     try:
#         # Get authenticated doctor ID from Lambda Authorizer
#         authorizer = event.get("requestContext", {}).get("authorizer", {})
#         doctor_id = authorizer.get("userId")

#         if not doctor_id:
#             return {
#                 "statusCode": 401,
#                 "body": json.dumps({
#                     "message": "Unauthorized."
#                 })
#             }

#         # Fetch photo key from RDS
#         connection = get_db_connection()

#         try:
#             with connection.cursor() as cursor:
#                 cursor.execute(
#                     """
#                     SELECT photo_key
#                     FROM doctors
#                     WHERE doctor_id = %s
#                     """,
#                     (doctor_id,)
#                 )

#                 doctor = cursor.fetchone()

#         finally:
#             connection.close()

#         if not doctor or not doctor.get("photo_key"):
#             return {
#                 "statusCode": 404,
#                 "body": json.dumps({
#                     "message": "Profile picture not found."
#                 })
#             }

#         photo_key = doctor["photo_key"]

#         # Generate pre-signed GET URL
#         photo_url = s3_client.generate_presigned_url(
#             "get_object",
#             Params={
#                 "Bucket": os.environ["PHOTOS_BUCKET"],
#                 "Key": photo_key
#             },
#             ExpiresIn=300
#         )

#         return {
#             "statusCode": 200,
#             "headers": {
#                 "Content-Type": "application/json"
#             },
#             "body": json.dumps({
#                 "message": "Profile picture URL generated successfully.",
#                 "photoUrl": photo_url,
#                 "expiresIn": 300
#             })
#         }

#     except Exception:
#         logger.exception("Failed to generate profile photo URL")

#         return {
#             "statusCode": 500,
#             "body": json.dumps({
#                 "message": "Unable to generate profile picture URL."
#             })
#         }


# def get_db_connection():
#     secret_arn = get_parameter("secret-arn")

#     secret_response = secretsmanager.get_secret_value(
#         SecretId=secret_arn
#     )
#     secret = json.loads(secret_response["SecretString"])

#     return pymysql.connect(
#         host=get_parameter("endpoint"),
#         port=int(get_parameter("port")),
#         user=get_parameter("username"),
#         password=secret["password"],
#         database=get_parameter("name"),
#         cursorclass=pymysql.cursors.DictCursor,
#         autocommit=False,
#         connect_timeout=5
#     )


# def response(status_code, body):
#     return {
#         "statusCode": status_code,
#         "headers": {
#             "Content-Type": "application/json"
#         },
#         "body": json.dumps(body, default=str)
#     }


# def get_doctor_id(event):
#     authorizer = (
#         event.get("requestContext", {})
#         .get("authorizer", {})
#     )

#     # Use identity supplied by the Lambda Authorizer.
#     doctor_id = authorizer.get("userId")
#     role = authorizer.get("role")

#     if not doctor_id:
#         return None, None

#     if role and role.upper() != "DOCTOR":
#         return None, "forbidden"

#     return doctor_id, None


# def lambda_handler(event, context):
#     http_method = event.get("httpMethod", "").upper()

#     if http_method not in ("GET", "PUT"):
#         return response(405, {
#             "message": "Method not allowed"
#         })

#     doctor_id, auth_error = get_doctor_id(event)

#     if auth_error == "forbidden":
#         return response(403, {
#             "message": "Doctor access required"
#         })

#     if not doctor_id:
#         return response(401, {
#             "message": "Unauthorized"
#         })

#     connection = None
#     http_method = event.get("httpMethod")
#     path = event.get("path", "")

#     if http_method == "GET" and path.endswith("/doctors/me/photo"):
#     return generate_profile_photo_url(event)

#     try:
#         connection = get_db_connection()

#         with connection.cursor() as cursor:

#             # GET /doctors/me
#             if http_method == "GET":
#                 cursor.execute(
#                     """
#                     SELECT
#                         doctor_id,
#                         email,
#                         name,
#                         phone,
#                         years_experience,
#                         photo_key,
#                         created_at
#                     FROM doctor
#                     WHERE doctor_id = %s
#                     """,
#                     (doctor_id,)
#                 )

#                 doctor = cursor.fetchone()

#                 if not doctor:
#                     return response(404, {
#                         "message": "Doctor profile not found"
#                     })

#                 return response(200, {
#                     "doctorId": doctor["doctor_id"],
#                     "email": doctor["email"],
#                     "fullName": doctor["name"],
#                     "phone": doctor["phone"],
#                     "yearsExperience": doctor["years_experience"],
#                     "photoKey": doctor["photo_key"],
#                     "createdAt": doctor["created_at"]
#                 })

#             # PUT /doctors/me
#             if http_method == "PUT":
#                 body = event.get("body")

#                 if isinstance(body, str):
#                     body = json.loads(body)

#                 if not isinstance(body, dict):
#                     return response(400, {
#                         "message": "Request body must be a JSON object"
#                     })

#                 allowed_fields = {
#                     "fullName": "name",
#                     "phone": "phone",
#                     "yearsExperience": "years_experience",
#                     "photoKey": "photo_key"
#                 }

#                 if not body:
#                     return response(400, {
#                         "message": "Request body cannot be empty"
#                     })

#                 invalid_fields = set(body.keys()) - set(allowed_fields)

#                 if invalid_fields:
#                     return response(400, {
#                         "message": "Unsupported fields",
#                         "fields": sorted(invalid_fields)
#                     })

#                 updates = []
#                 values = []

#                 for api_field, db_field in allowed_fields.items():
#                     if api_field in body:
#                         value = body[api_field]

#                         if api_field in ("fullName", "phone", "photoKey"):
#                             if value is not None and not isinstance(value, str):
#                                 return response(400, {
#                                     "message": f"{api_field} must be a string or null"
#                                 })

#                         if api_field == "yearsExperience":
#                             if (
#                                 value is not None
#                                 and (
#                                     isinstance(value, bool)
#                                     or not isinstance(value, int)
#                                     or value < 0
#                                 )
#                             ):
#                                 return response(400, {
#                                     "message": "yearsExperience must be a non-negative integer"
#                                 })

#                         updates.append(f"{db_field} = %s")
#                         values.append(value)

#                 values.append(doctor_id)

#                 cursor.execute(
#                     f"""
#                     UPDATE doctor
#                     SET {", ".join(updates)}
#                     WHERE doctor_id = %s
#                     """,
#                     tuple(values)
#                 )

#                 if cursor.rowcount == 0:
#                     # Distinguish unchanged values from a missing doctor.
#                     cursor.execute(
#                         """
#                         SELECT doctor_id
#                         FROM doctor
#                         WHERE doctor_id = %s
#                         """,
#                         (doctor_id,)
#                     )

#                     if not cursor.fetchone():
#                         connection.rollback()
#                         return response(404, {
#                             "message": "Doctor profile not found"
#                         })

#                 connection.commit()

#                 return response(200, {
#                     "message": "Profile updated successfully"
#                 })

#     except json.JSONDecodeError:
#         if connection:
#             connection.rollback()

#         return response(400, {
#             "message": "Invalid JSON body"
#         })

#     except Exception:
#         if connection:
#             connection.rollback()

#         print("Error processing doctor profile request")

#         return response(500, {
#             "message": "Internal server error"
#         })

#     finally:
#         if connection:
#             connection.close()
import os
import json
import boto3
import pymysql
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3_client = boto3.client("s3")
ssm = boto3.client("ssm")
secretsmanager = boto3.client("secretsmanager")

ENVIRONMENT = os.environ.get("ENVIRONMENT", "dev")
PARAMETER_PREFIX = f"/medislot/{ENVIRONMENT}/db"


def get_parameter(name):
    response = ssm.get_parameter(
        Name=f"{PARAMETER_PREFIX}/{name}",
        WithDecryption=True
    )
    return response["Parameter"]["Value"]


def get_db_connection():
    secret_arn = get_parameter("secret-arn")

    secret_response = secretsmanager.get_secret_value(
        SecretId=secret_arn
    )

    secret = json.loads(secret_response["SecretString"])

    return pymysql.connect(
        host=get_parameter("endpoint"),
        port=int(get_parameter("port")),
        user=get_parameter("username"),
        password=secret["password"],
        database=get_parameter("name"),
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
        connect_timeout=5
    )


def response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json"
        },
        "body": json.dumps(body, default=str)
    }


def get_doctor_id(event):
    authorizer = (
        event.get("requestContext", {})
        .get("authorizer", {})
    )

    doctor_id = authorizer.get("userId")
    role = authorizer.get("role")

    if not doctor_id:
        return None, None

    if role and role.upper() != "DOCTOR":
        return None, "forbidden"

    return doctor_id, None


def generate_profile_photo_url(doctor_id):
    connection = None

    try:
        connection = get_db_connection()

        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT photo_key
                FROM doctor
                WHERE doctor_id = %s
                """,
                (doctor_id,)
            )

            doctor = cursor.fetchone()

        if not doctor or not doctor.get("photo_key"):
            return response(404, {
                "message": "Profile picture not found"
            })

        photo_key = doctor["photo_key"]

        photo_url = s3_client.generate_presigned_url(
            ClientMethod="get_object",
            Params={
                "Bucket": os.environ["PHOTOS_BUCKET"],
                "Key": photo_key
            },
            ExpiresIn=300
        )

        return response(200, {
            "message": "Profile picture URL generated successfully.",
            "photoUrl": photo_url,
            "expiresIn": 300
        })

    except Exception:
        logger.exception("Failed to generate profile photo URL")

        return response(500, {
            "message": "Unable to generate profile picture URL"
        })

    finally:
        if connection:
            connection.close()


def lambda_handler(event, context):

    http_method = event.get("httpMethod", "").upper()
    path = event.get("path", "")

    if http_method not in ("GET", "PUT"):
        return response(405, {
            "message": "Method not allowed"
        })

    # Authenticate doctor
    doctor_id, auth_error = get_doctor_id(event)

    if auth_error == "forbidden":
        return response(403, {
            "message": "Doctor access required"
        })

    if not doctor_id:
        return response(401, {
            "message": "Unauthorized"
        })

    # GET /doctors/me/photo
    if http_method == "GET" and path.endswith("/doctors/me/photo"):
        return generate_profile_photo_url(doctor_id)

    connection = None

    try:
        connection = get_db_connection()

        with connection.cursor() as cursor:

            # GET /doctors/me
            if http_method == "GET":

                cursor.execute(
                    """
                    SELECT
                        doctor_id,
                        email,
                        name,
                        phone,
                        years_experience,
                        photo_key,
                        created_at
                    FROM doctor
                    WHERE doctor_id = %s
                    """,
                    (doctor_id,)
                )

                doctor = cursor.fetchone()

                if not doctor:
                    return response(404, {
                        "message": "Doctor profile not found"
                    })

                return response(200, {
                    "doctorId": doctor["doctor_id"],
                    "email": doctor["email"],
                    "fullName": doctor["name"],
                    "phone": doctor["phone"],
                    "yearsExperience": doctor["years_experience"],
                    "photoKey": doctor["photo_key"],
                    "createdAt": doctor["created_at"]
                })

            # PUT /doctors/me
            if http_method == "PUT":

                body = event.get("body")

                if isinstance(body, str):
                    body = json.loads(body)

                if not isinstance(body, dict):
                    return response(400, {
                        "message": "Request body must be a JSON object"
                    })

                allowed_fields = {
                    "fullName": "name",
                    "phone": "phone",
                    "yearsExperience": "years_experience",
                    "photoKey": "photo_key"
                }

                if not body:
                    return response(400, {
                        "message": "Request body cannot be empty"
                    })

                invalid_fields = set(body.keys()) - set(allowed_fields)

                if invalid_fields:
                    return response(400, {
                        "message": "Unsupported fields",
                        "fields": sorted(invalid_fields)
                    })

                updates = []
                values = []

                for api_field, db_field in allowed_fields.items():

                    if api_field in body:

                        value = body[api_field]

                        if api_field in ("fullName", "phone", "photoKey"):
                            if value is not None and not isinstance(value, str):
                                return response(400, {
                                    "message": f"{api_field} must be a string or null"
                                })

                        if api_field == "yearsExperience":
                            if (
                                value is not None
                                and (
                                    isinstance(value, bool)
                                    or not isinstance(value, int)
                                    or value < 0
                                )
                            ):
                                return response(400, {
                                    "message": "yearsExperience must be a non-negative integer"
                                })

                        updates.append(f"{db_field} = %s")
                        values.append(value)

                values.append(doctor_id)

                cursor.execute(
                    f"""
                    UPDATE doctor
                    SET {", ".join(updates)}
                    WHERE doctor_id = %s
                    """,
                    tuple(values)
                )

                if cursor.rowcount == 0:

                    cursor.execute(
                        """
                        SELECT doctor_id
                        FROM doctor
                        WHERE doctor_id = %s
                        """,
                        (doctor_id,)
                    )

                    if not cursor.fetchone():
                        connection.rollback()

                        return response(404, {
                            "message": "Doctor profile not found"
                        })

                connection.commit()

                return response(200, {
                    "message": "Profile updated successfully"
                })

    except json.JSONDecodeError:
        if connection:
            connection.rollback()

        return response(400, {
            "message": "Invalid JSON body"
        })

    except Exception:
        if connection:
            connection.rollback()

        logger.exception("Error processing doctor profile request")

        return response(500, {
            "message": "Internal server error"
        })

    finally:
        if connection:
            connection.close()