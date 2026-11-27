
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
                cursor.execute(
                    """
                    SELECT l.name
                    FROM doctor_language AS dl
                    JOIN language AS l
                        ON l.language_id = dl.language_id
                    WHERE dl.doctor_id = %s
                    ORDER BY l.name
                    """,
                    (doctor_id,)
                )

                languages_spoken = [row["name"] for row in cursor.fetchall()]
                # Get hospitals where the doctor works
                cursor.execute(
                    """
                    SELECT
                        h.hospital_id,
                        h.name AS hospital_name,
                        d.name AS department_name
                    FROM doctor_hospital AS dh
                    JOIN hospitals AS h
                        ON h.hospital_id = dh.hospital_id
                    JOIN department AS d
                        ON d.department_id = dh.department_id
                    WHERE dh.doctor_id = %s
                    ORDER BY h.name
                    """,
                    (doctor_id,)
                )

                hospitals = [
                    {
                        "hospitalId": row["hospital_id"],
                        "hospitalName": row["hospital_name"],
                        "department": row["department_name"]
                    }
                    for row in cursor.fetchall()
                ]
                # Get doctor's specialisations
                cursor.execute(
                    """
                    SELECT s.name
                    FROM doctor_specialisation AS ds
                    JOIN specialisations AS s
                        ON s.specialisation_id = ds.specialisation_id
                    WHERE ds.doctor_id = %s
                    ORDER BY s.name
                    """,
                    (doctor_id,)
                )

                specialisations = [
                    row["name"]
                    for row in cursor.fetchall()
                ]
                # Get doctor's addresses
                cursor.execute(
                    """
                    SELECT
                        address_line_1,
                        address_line_2,
                        city,
                        postal_code
                    FROM doctor_address
                    WHERE doctor_id = %s
                    ORDER BY id
                    """,
                    (doctor_id,)
                )

                addresses = [
                    {
                        "addressLine1": row["address_line_1"],
                        "addressLine2": row["address_line_2"],
                        "city": row["city"],
                        "postalCode": row["postal_code"]
                    }
                    for row in cursor.fetchall()
                ]
                # Get doctor's medical licences
                cursor.execute(
                    """
                    SELECT
                        licence_no,
                        authority,
                        dates,
                        verification_status
                    FROM doctor_licence
                    WHERE doctor_id = %s
                    ORDER BY licence_no
                    """,
                    (doctor_id,)
                )

                licences = [
                    {
                        "licenceNumber": row["licence_no"],
                        "authority": row["authority"],
                        "dates": row["dates"],
                        "verificationStatus": row["verification_status"]
                    }
                    for row in cursor.fetchall()
                ]
                
                

                return response(200, {
                    "doctorId": doctor["doctor_id"],
                    "email": doctor["email"],
                    "fullName": doctor["name"],
                    "phone": doctor["phone"],
                    "yearsExperience": doctor["years_experience"],
                    "photoKey": doctor["photo_key"],
                    "createdAt": doctor["created_at"],
                    "languagesSpoken": languages_spoken,
                    "hospitals": hospitals,
                    "specialisations": specialisations,
                    "addresses": addresses,
                    "licences": licences
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

                supported_fields = set(allowed_fields) | {"languagesSpoken"}
                invalid_fields = set(body) - supported_fields

                if invalid_fields:
                    return response(400, {
                        "message": "Unsupported fields",
                        "fields": sorted(invalid_fields)
                    })
                languages_spoken = None
                if "languagesSpoken" in body:
                    raw_languages = body["languagesSpoken"]
                    if not isinstance(raw_languages, list) or len(raw_languages) > 30:
                        return response(400, {
                            "message": "languagesSpoken must be a list of at most 30 languages."
                        })

                    languages_spoken = []
                    seen_languages = set()

                    for language in raw_languages:
                        if not isinstance(language, str):
                            return response(400, {
                                "message": "Each language must be a string."
                            })

                        language = language.strip()
                        if not 1 <= len(language) <= 100:
                            return response(400, {
                                "message": "Each language must contain 1 to 100 characters."
                            })

                        if any(ord(char) < 32 or ord(char) == 127 for char in language):
                            return response(400, {
                                "message": "Language contains invalid characters."
                            })

                        normalized = language.casefold()
                        if normalized not in seen_languages:
                            languages_spoken.append(language)
                            seen_languages.add(normalized)

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

                if updates:
                    values.append(doctor_id)
                    cursor.execute(
                        f"""
                        UPDATE doctor
                        SET {", ".join(updates)}
                        WHERE doctor_id = %s
                        """,
                        tuple(values)
                    )

                if languages_spoken is not None:
                    cursor.execute(
                        "DELETE FROM doctor_language WHERE doctor_id = %s",
                        (doctor_id,)
                    )

                    for language in languages_spoken:
                        cursor.execute(
                            """
                            SELECT language_id
                            FROM language
                            WHERE LOWER(name) = LOWER(%s)
                            LIMIT 1
                            """,
                            (language,)
                        )
                        language_row = cursor.fetchone()

                        if language_row:
                            language_id = language_row["language_id"]
                        else:
                            cursor.execute(
                                "INSERT INTO language (name) VALUES (%s)",
                                (language,)
                            )
                            language_id = cursor.lastrowid

                        cursor.execute(
                            """
                            INSERT INTO doctor_language (doctor_id, language_id)
                            VALUES (%s, %s)
                            """,
                            (doctor_id, language_id)
                        )

                
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