import os
import json
import re
import uuid
import logging
import base64
import boto3
import pymysql
import bcrypt

from datetime import date
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# --------------------------------------------------
# AWS CLIENTS AND CONFIGURATION
# --------------------------------------------------

ssm = boto3.client("ssm")
secretsmanager = boto3.client("secretsmanager")

ENVIRONMENT = os.environ.get("ENVIRONMENT", "dev")
PARAMETER_PREFIX = f"/medislot/{ENVIRONMENT}/db"


# --------------------------------------------------
# DATABASE CONNECTION: SAME AS DB-MIGRATIONS LAMBDA
# --------------------------------------------------

def get_parameter(name):
    response = ssm.get_parameter(
        Name=f"{PARAMETER_PREFIX}/{name}",
        WithDecryption=True
    )
    return response["Parameter"]["Value"]


def get_db_connection():
    logger.info("Reading database configuration")

    endpoint = get_parameter("endpoint")
    port = int(get_parameter("port"))
    username = get_parameter("username")
    database = get_parameter("name")
    secret_arn = get_parameter("secret-arn")

    logger.info("Retrieving database credentials")

    secret_response = secretsmanager.get_secret_value(
        SecretId=secret_arn
    )
    secret = json.loads(secret_response["SecretString"])

    logger.info("Connecting to RDS MySQL")

    return pymysql.connect(
        host=endpoint,
        port=port,
        user=username,
        password=secret["password"],
        database=database,
        connect_timeout=10,
        read_timeout=30,
        write_timeout=30,
        autocommit=False,
        charset="utf8mb4"
    )


# --------------------------------------------------
# RESPONSE HELPER
# --------------------------------------------------

def response(status_code, message, data=None):
    body = {"message": message}

    if data is not None:
        body["data"] = data

    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json"
        },
        "body": json.dumps(body, default=str)
    }


# --------------------------------------------------
# GENERAL VALIDATION
# --------------------------------------------------

def get_string(data, key, label=None, required=False, max_length=255):
    label = label or key
    value = data.get(key)

    if value is None:
        if required:
            raise ValueError(f"{label} is required.")
        return None

    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string.")

    value = value.strip()

    if not value:
        if required:
            raise ValueError(f"{label} is required.")
        return None

    if len(value) > max_length:
        raise ValueError(
            f"{label} must not exceed {max_length} characters."
        )

    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError(f"{label} contains invalid characters.")

    return value


def validate_name(value, label="name", max_length=150):
    if not isinstance(value, str):
        raise ValueError(f"{label} is required.")

    value = value.strip()

    if not 2 <= len(value) <= max_length:
        raise ValueError(
            f"{label} must contain 2 to {max_length} characters."
        )

    if not all(
        char.isalpha() or char in " .'-"
        for char in value
    ):
        raise ValueError(f"{label} contains invalid characters.")

    return value


def validate_email(value):
    if not isinstance(value, str):
        raise ValueError("email is required.")

    value = value.strip().lower()

    if len(value) > 255:
        raise ValueError("Email must not exceed 255 characters.")

    pattern = (
        r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
        r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+$"
    )

    if not re.fullmatch(pattern, value):
        raise ValueError("Enter a valid email address.")

    return value


def validate_password(value):
    if not isinstance(value, str):
        raise ValueError("password is required.")

    if not 12 <= len(value) <= 72:
        raise ValueError(
            "Password must contain 12 to 72 characters."
        )

    if len(value.encode("utf-8")) > 72:
        raise ValueError(
            "Password must not exceed 72 UTF-8 bytes."
        )

    if not re.search(r"[a-z]", value):
        raise ValueError(
            "Password must contain a lowercase letter."
        )

    if not re.search(r"[A-Z]", value):
        raise ValueError(
            "Password must contain an uppercase letter."
        )

    if not re.search(r"[0-9]", value):
        raise ValueError("Password must contain a number.")

    if not re.search(r"[^A-Za-z0-9]", value):
        raise ValueError(
            "Password must contain a special character."
        )

    return bcrypt.hashpw(
        value.encode("utf-8"),
        bcrypt.gensalt(rounds=12)
    ).decode("utf-8")


def validate_phone(value, required=False):
    if value is None or value == "":
        if required:
            raise ValueError("phone is required.")
        return None

    if not isinstance(value, str):
        raise ValueError("phone must be a string.")

    value = value.strip()

    if len(value) > 20:
        raise ValueError("Phone must not exceed 20 characters.")

    if not re.fullmatch(r"\+?[0-9()\s.-]+", value):
        raise ValueError("Phone number format is invalid.")

    digits = re.sub(r"\D", "", value)

    if not 7 <= len(digits) <= 15:
        raise ValueError(
            "Phone must contain between 7 and 15 digits."
        )

    return value


def validate_date_of_birth(value):
    if value is None or value == "":
        return None

    if not isinstance(value, str):
        raise ValueError(
            "dateOfBirth must use YYYY-MM-DD format."
        )

    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValueError(
            "dateOfBirth must use YYYY-MM-DD format."
        )

    if parsed.isoformat() != value:
        raise ValueError(
            "dateOfBirth must use YYYY-MM-DD format."
        )

    if parsed > date.today():
        raise ValueError(
            "Date of birth cannot be in the future."
        )

    return parsed


def validate_experience(value):
    if value is None:
        return None

    if type(value) is not int or not 0 <= value <= 80:
        raise ValueError(
            "yearsExperience must be an integer between 0 and 80."
        )

    return value


# --------------------------------------------------
# LOOKUP TABLE HELPER
# --------------------------------------------------

def get_or_create_lookup(cursor, table, id_column, name, max_length):
    """
    Get an existing lookup record or create one.
    Table and column names are fixed by internal calls,
    never supplied directly by the request.
    """
    name = get_string(
        {"value": name},
        "value",
        label=f"{table} name",
        required=True,
        max_length=max_length
    )

    cursor.execute(
        f"SELECT `{id_column}` FROM `{table}` "
        "WHERE LOWER(`name`) = LOWER(%s) LIMIT 1",
        (name,)
    )

    row = cursor.fetchone()

    if row:
        return row[0]

    cursor.execute(
        f"INSERT INTO `{table}` (`name`) VALUES (%s)",
        (name,)
    )

    return cursor.lastrowid


# --------------------------------------------------
# DOCTOR VALIDATION
# --------------------------------------------------

def validate_doctor(data):
    if not isinstance(data, dict):
        raise ValueError("Request body must be a JSON object.")

    name = validate_name(
        data.get("name"),
        "name",
        150
    )

    email = validate_email(data.get("email"))
    password_hash = validate_password(data.get("password"))

    phone = validate_phone(
        data.get("phone"),
        required=False
    )

    experience = validate_experience(
        data.get("yearsExperience")
    )

    licence_number = get_string(
        data,
        "licenceNumber",
        required=True,
        max_length=50
    )

    if len(licence_number) < 2:
        raise ValueError(
            "licenceNumber must contain at least 2 characters."
        )

    authority = get_string(
        data,
        "licenceAuthority",
        required=False,
        max_length=150
    )

    specialisation = get_string(
        data,
        "specialisation",
        required=True,
        max_length=100
    )

    # Qualifications must be a list of objects.
    qualifications = data.get("qualifications", [])

    if not isinstance(qualifications, list):
        raise ValueError("qualifications must be a list.")

    if len(qualifications) > 20:
        raise ValueError("Maximum 20 qualifications are allowed.")

    validated_qualifications = []

    for item in qualifications:
        if not isinstance(item, dict):
            raise ValueError(
                "Each qualification must be a JSON object."
            )

        degree = get_string(
            item, "degree",
            required=True,
            max_length=100
        )

        institution = get_string(
            item, "institution",
            required=False,
            max_length=150
        )

        year = item.get("year")

        if year is not None:
            if type(year) is not int:
                raise ValueError(
                    "Qualification year must be an integer."
                )

            if not 1900 <= year <= date.today().year:
                raise ValueError(
                    "Qualification year is outside the valid range."
                )

        validated_qualifications.append({
            "degree": degree,
            "institution": institution,
            "year": year
        })

    # Address is optional.
    address = data.get("address", {})

    if address is None:
        address = {}

    if not isinstance(address, dict):
        raise ValueError("address must be a JSON object.")

    validated_address = {
        "address_line_1": get_string(
            address, "addressLine1", max_length=255
        ),
        "address_line_2": get_string(
            address, "addressLine2", max_length=255
        ),
        "city": get_string(
            address, "city", max_length=100
        ),
        "postal_code": get_string(
            address, "postalCode", max_length=20
        )
    }

    # Languages are optional.
    languages = data.get("languagesSpoken", [])

    if not isinstance(languages, list):
        raise ValueError("languagesSpoken must be a list.")

    if len(languages) > 30:
        raise ValueError("Maximum 30 languages are allowed.")

    validated_languages = []

    for language in languages:
        if not isinstance(language, str):
            raise ValueError("Each language must be a string.")

        language = language.strip()

        if not 1 <= len(language) <= 50:
            raise ValueError(
                "Each language must contain 1 to 50 characters."
            )

        if any(ord(c) < 32 or ord(c) == 127 for c in language):
            raise ValueError("Language contains invalid characters.")

        validated_languages.append(language)

    # Hospital and department are optional.
    hospital = data.get("hospital")

    if hospital is not None and not isinstance(hospital, dict):
        raise ValueError("hospital must be a JSON object.")

    hospital_name = None
    department_name = None

    if hospital:
        hospital_name = get_string(
            hospital, "name", max_length=150
        )

        department_name = get_string(
            hospital, "department", max_length=100
        )

        if bool(hospital_name) != bool(department_name):
            raise ValueError(
                "Provide both hospital name and department."
            )

    return {
        "doctor_id": str(uuid.uuid4()),
        "email": email,
        "password_hash": password_hash,
        "name": name,
        "phone": phone,
        "years_experience": experience,
        "licence_number": licence_number,
        "licence_authority": authority,
        "specialisation": specialisation,
        "qualifications": validated_qualifications,
        "address": validated_address,
        "languages": list(dict.fromkeys(validated_languages)),
        "hospital_name": hospital_name,
        "department_name": department_name
    }


# --------------------------------------------------
# DOCTOR DATABASE INSERTION
# --------------------------------------------------

def register_doctor(connection, doctor):
    with connection.cursor() as cursor:
        doctor_id = doctor["doctor_id"]

        # 1. Insert core doctor profile.
        cursor.execute(
            """
            INSERT INTO doctors (
                doctor_id,
                email,
                password_hash,
                name,
                phone,
                years_experience
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                doctor_id,
                doctor["email"],
                doctor["password_hash"],
                doctor["name"],
                doctor["phone"],
                doctor["years_experience"]
            )
        )

        # 2. Insert licence.
        cursor.execute(
            """
            INSERT INTO doctor_licences (
                doctor_id,
                licence_no,
                authority,
                verification_status
            )
            VALUES (%s, %s, %s, %s)
            """,
            (
                doctor_id,
                doctor["licence_number"],
                doctor["licence_authority"],
                "PENDING"
            )
        )

        # 3. Link specialisation.
        specialisation_id = get_or_create_lookup(
            cursor,
            "specialisations",
            "specialisation_id",
            doctor["specialisation"],
            100
        )

        cursor.execute(
            """
            INSERT INTO doctor_specialisations (
                doctor_id,
                specialisation_id
            )
            VALUES (%s, %s)
            """,
            (doctor_id, specialisation_id)
        )

        # 4. Insert qualifications.
        for qualification in doctor["qualifications"]:
            cursor.execute(
                """
                INSERT INTO doctor_qualifications (
                    doctor_id,
                    degree,
                    institution,
                    year
                )
                VALUES (%s, %s, %s, %s)
                """,
                (
                    doctor_id,
                    qualification["degree"],
                    qualification["institution"],
                    qualification["year"]
                )
            )

        # 5. Insert address.
        address = doctor["address"]

        if any(address.values()):
            cursor.execute(
                """
                INSERT INTO doctor_addresses (
                    doctor_id,
                    address_line_1,
                    address_line_2,
                    city,
                    postal_code
                )
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    doctor_id,
                    address["address_line_1"],
                    address["address_line_2"],
                    address["city"],
                    address["postal_code"]
                )
            )

        # 6. Link languages.
        for language in doctor["languages"]:
            language_id = get_or_create_lookup(
                cursor,
                "languages",
                "language_id",
                language,
                50
            )

            cursor.execute(
                """
                INSERT INTO doctor_languages (
                    doctor_id,
                    language_id
                )
                VALUES (%s, %s)
                """,
                (doctor_id, language_id)
            )

        # 7. Link hospital and department.
        if doctor["hospital_name"]:
            hospital_id = get_or_create_lookup(
                cursor,
                "hospitals",
                "hospital_id",
                doctor["hospital_name"],
                150
            )

            department_id = get_or_create_lookup(
                cursor,
                "departments",
                "department_id",
                doctor["department_name"],
                100
            )

            cursor.execute(
                """
                INSERT INTO doctor_hospitals (
                    doctor_id,
                    hospital_id,
                    department_id
                )
                VALUES (%s, %s, %s)
                """,
                (doctor_id, hospital_id, department_id)
            )

    return doctor_id


# --------------------------------------------------
# PATIENT VALIDATION
# --------------------------------------------------

def validate_patient(data):
    if not isinstance(data, dict):
        raise ValueError("Request body must be a JSON object.")

    first_name = validate_name(
        data.get("firstName"),
        "firstName",
        100
    )

    last_name = validate_name(
        data.get("lastName"),
        "lastName",
        100
    )

    email = validate_email(data.get("email"))
    password_hash = validate_password(data.get("password"))

    phone = validate_phone(
        data.get("phone"),
        required=False
    )

    city = get_string(
        data,
        "city",
        required=False,
        max_length=100
    )

    dob = validate_date_of_birth(
        data.get("dateOfBirth")
    )

    gender = get_string(
        data,
        "gender",
        required=False,
        max_length=20
    )

    if gender and not re.fullmatch(
        r"[A-Za-z][A-Za-z '\-]*", gender
    ):
        raise ValueError("gender contains invalid characters.")

    return {
        "first_name": first_name,
        "last_name": last_name,
        "email": email,
        "password_hash": password_hash,
        "phone": phone,
        "city": city,
        "date_of_birth": dob,
        "gender": gender
    }


# --------------------------------------------------
# PATIENT DATABASE INSERTION
# --------------------------------------------------

def register_patient(connection, patient):
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO patients (
                email,
                password_hash,
                first_name,
                last_name,
                phone,
                city,
                date_of_birth,
                gender
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                patient["email"],
                patient["password_hash"],
                patient["first_name"],
                patient["last_name"],
                patient["phone"],
                patient["city"],
                patient["date_of_birth"],
                patient["gender"]
            )
        )

        return cursor.lastrowid


# --------------------------------------------------
# LAMBDA HANDLER
# --------------------------------------------------

def lambda_handler(event, context):
    connection = None

    try:
        request_context = event.get("requestContext", {})
        http_context = request_context.get("http", {})

        method = (
            http_context.get("method")
            or event.get("httpMethod")
            or ""
        ).upper()

        route = (
            event.get("rawPath")
            or event.get("path")
            or event.get("routeKey", "").split(" ", 1)[-1]
        ).rstrip("/").lower()

        if method != "POST":
            return response(405, "Method not allowed.")

        if route == "/auth/doctors/register":
            registration_type = "doctor"

        elif route == "/auth/patients/register":
            registration_type = "patient"

        else:
            return response(404, "Registration route not found.")

        body = event.get("body")

        if not body:
            return response(400, "Request body is required.")

        if event.get("isBase64Encoded"):
            body = base64.b64decode(body).decode("utf-8")

        try:
            data = json.loads(body) if isinstance(body, str) else body
        except (json.JSONDecodeError, UnicodeDecodeError):
            return response(400, "Request body must contain valid JSON.")

        if not isinstance(data, dict):
            return response(400, "Request body must be a JSON object.")

        # Validate before connecting to the database.
        if registration_type == "doctor":
            record = validate_doctor(data)
        else:
            record = validate_patient(data)

        connection = get_db_connection()

        if registration_type == "doctor":
            record_id = register_doctor(connection, record)
            id_field = "doctorId"
        else:
            record_id = register_patient(connection, record)
            id_field = "patientId"

        # Commit all inserts as one transaction.
        connection.commit()

        logger.info(
            "Registration succeeded: type=%s request_id=%s",
            registration_type,
            getattr(context, "aws_request_id", "unknown")
        )

        return response(
            201,
            f"{registration_type.capitalize()} registered successfully.",
            {id_field: record_id}
        )

    except ValueError as exc:
        return response(400, str(exc))

    except pymysql.err.IntegrityError as exc:
        if connection:
            connection.rollback()

        if exc.args and exc.args[0] == 1062:
            return response(
                409,
                "Email or licence number already exists."
            )

        logger.exception("Database integrity error.")
        return response(500, "Registration could not be completed.")

    except (ClientError, pymysql.MySQLError):
        if connection:
            connection.rollback()

        logger.exception("AWS or database operation failed.")
        return response(500, "Registration could not be completed.")

    except Exception:
        if connection:
            connection.rollback()

        logger.exception("Unexpected registration error.")
        return response(500, "An internal error occurred.")

    finally:
        if connection:
            connection.close()