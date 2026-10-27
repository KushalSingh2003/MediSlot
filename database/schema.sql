-- MediSlot: Full RDS schema (MySQL) - doctor side + patient side
-- Run: mysql -h <rds-endpoint> -u <user> -p < schema.sql
CREATE DATABASE IF NOT EXISTS medislot;
USE medislot;

-- ---------- Lookup / parent tables ----------

CREATE TABLE IF NOT EXISTS doctors (
    doctor_id         VARCHAR(36)  NOT NULL,
    email             VARCHAR(255) NOT NULL,
    password_hash     VARCHAR(255) NOT NULL,
    name              VARCHAR(150) NOT NULL,
    phone             VARCHAR(20),
    years_experience  INT,
    photo_key         VARCHAR(255),          -- S3 object key
    created_at        TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (doctor_id),
    UNIQUE KEY uk_doctors_email (email)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS specialisations (
    specialisation_id INT          NOT NULL AUTO_INCREMENT,
    name              VARCHAR(100) NOT NULL,
    PRIMARY KEY (specialisation_id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS languages (
    language_id INT          NOT NULL AUTO_INCREMENT,
    name        VARCHAR(50)  NOT NULL,
    PRIMARY KEY (language_id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS hospitals (
    hospital_id INT          NOT NULL AUTO_INCREMENT,
    name        VARCHAR(150) NOT NULL,
    PRIMARY KEY (hospital_id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS departments (
    department_id INT          NOT NULL AUTO_INCREMENT,
    name          VARCHAR(100) NOT NULL,
    PRIMARY KEY (department_id)
) ENGINE=InnoDB;

-- ---------- Doctor child tables ----------

CREATE TABLE IF NOT EXISTS doctor_licences (
    doctor_id           VARCHAR(36)  NOT NULL,
    licence_no          VARCHAR(50)  NOT NULL,
    authority           VARCHAR(150),
    dates               VARCHAR(100),
    verification_status VARCHAR(30),
    UNIQUE KEY uk_licence_no (licence_no),
    CONSTRAINT fk_licences_doctor FOREIGN KEY (doctor_id)
        REFERENCES doctors (doctor_id) ON DELETE CASCADE
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS doctor_addresses (
    id             INT          NOT NULL AUTO_INCREMENT,
    doctor_id      VARCHAR(36)  NOT NULL,
    address_line_1 VARCHAR(255),
    address_line_2 VARCHAR(255),
    city           VARCHAR(100),
    postal_code    VARCHAR(20),
    PRIMARY KEY (id),
    CONSTRAINT fk_addresses_doctor FOREIGN KEY (doctor_id)
        REFERENCES doctors (doctor_id) ON DELETE CASCADE
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS doctor_qualifications (
    id          INT          NOT NULL AUTO_INCREMENT,
    doctor_id   VARCHAR(36)  NOT NULL,
    degree      VARCHAR(100),
    institution VARCHAR(150),
    year        INT,
    PRIMARY KEY (id),
    CONSTRAINT fk_qualifications_doctor FOREIGN KEY (doctor_id)
        REFERENCES doctors (doctor_id) ON DELETE CASCADE
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS doctor_specialisations (
    doctor_id         VARCHAR(36) NOT NULL,
    specialisation_id INT         NOT NULL,
    PRIMARY KEY (doctor_id, specialisation_id),
    CONSTRAINT fk_docspec_doctor FOREIGN KEY (doctor_id)
        REFERENCES doctors (doctor_id) ON DELETE CASCADE,
    CONSTRAINT fk_docspec_spec FOREIGN KEY (specialisation_id)
        REFERENCES specialisations (specialisation_id) ON DELETE CASCADE
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS doctor_languages (
    doctor_id   VARCHAR(36) NOT NULL,
    language_id INT         NOT NULL,
    PRIMARY KEY (doctor_id, language_id),
    CONSTRAINT fk_doclang_doctor FOREIGN KEY (doctor_id)
        REFERENCES doctors (doctor_id) ON DELETE CASCADE,
    CONSTRAINT fk_doclang_lang FOREIGN KEY (language_id)
        REFERENCES languages (language_id) ON DELETE CASCADE
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS doctor_hospitals (
    doctor_id     VARCHAR(36) NOT NULL,
    hospital_id   INT         NOT NULL,
    department_id INT         NOT NULL,
    PRIMARY KEY (doctor_id, hospital_id, department_id),
    CONSTRAINT fk_dochosp_doctor FOREIGN KEY (doctor_id)
        REFERENCES doctors (doctor_id) ON DELETE CASCADE,
    CONSTRAINT fk_dochosp_hospital FOREIGN KEY (hospital_id)
        REFERENCES hospitals (hospital_id) ON DELETE CASCADE,
    CONSTRAINT fk_dochosp_department FOREIGN KEY (department_id)
        REFERENCES departments (department_id) ON DELETE CASCADE
) ENGINE=InnoDB;

-- ---------- Scheduling tables ----------

CREATE TABLE IF NOT EXISTS consultation_types (
    id           INT           NOT NULL AUTO_INCREMENT,
    doctor_id    VARCHAR(36)   NOT NULL,
    name         VARCHAR(100)  NOT NULL,
    duration_min DECIMAL(5,2)  NOT NULL,
    fee          DECIMAL(10,2) NOT NULL,
    capacity     INT           NOT NULL DEFAULT 1,
    active       BOOLEAN       NOT NULL DEFAULT TRUE,
    PRIMARY KEY (id),
    CONSTRAINT fk_consult_doctor FOREIGN KEY (doctor_id)
        REFERENCES doctors (doctor_id) ON DELETE CASCADE
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS availability_patterns (
    id                   INT         NOT NULL AUTO_INCREMENT,
    doctor_id            VARCHAR(36) NOT NULL,
    weekday              INT         NOT NULL,   -- 0 = Monday ... 6 = Sunday
    start_time           TIME        NOT NULL,
    end_time             TIME        NOT NULL,
    consultation_type_id INT         NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT fk_avail_doctor FOREIGN KEY (doctor_id)
        REFERENCES doctors (doctor_id) ON DELETE CASCADE,
    CONSTRAINT fk_avail_consult FOREIGN KEY (consultation_type_id)
        REFERENCES consultation_types (id) ON DELETE CASCADE
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS blackouts (
    id            INT         NOT NULL AUTO_INCREMENT,
    doctor_id     VARCHAR(36) NOT NULL,
    blackout_date DATE        NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT fk_blackouts_doctor FOREIGN KEY (doctor_id)
        REFERENCES doctors (doctor_id) ON DELETE CASCADE
) ENGINE=InnoDB;

-- =====================================================================
-- PATIENT SIDE
-- =====================================================================

CREATE TABLE IF NOT EXISTS patients (
    patient_id    BIGINT       NOT NULL AUTO_INCREMENT,
    email         VARCHAR(255) NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    first_name    VARCHAR(100),
    last_name     VARCHAR(100),
    phone         VARCHAR(20),
    city          VARCHAR(100),
    date_of_birth DATE,
    gender        VARCHAR(20),
    created_at    TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (patient_id),
    UNIQUE KEY uk_patients_email (email)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS appointments (
    appointment_id   BIGINT        NOT NULL AUTO_INCREMENT,
    patient_id       BIGINT        NOT NULL,
    doctor_id        VARCHAR(36)   NOT NULL,   -- matches doctors.doctor_id (no FK, as in the diagram)
    slot_id          BIGINT,
    slot_start_utc   TIMESTAMP     NULL,
    slot_end_utc     TIMESTAMP     NULL,
    fee_amount       DECIMAL(10,2),
    status           VARCHAR(50),
    reschedule_count INT           NOT NULL DEFAULT 0,
    created_at       TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at       TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (appointment_id),
    KEY idx_appt_patient (patient_id),
    KEY idx_appt_doctor (doctor_id),
    CONSTRAINT fk_appt_patient FOREIGN KEY (patient_id)
        REFERENCES patients (patient_id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS refund_ledger (
    refund_id      BIGINT        NOT NULL AUTO_INCREMENT,
    appointment_id BIGINT        NOT NULL,
    patient_id     BIGINT        NOT NULL,
    refund_percent INT,
    refund_amount  DECIMAL(10,2),
    reason         VARCHAR(255),
    created_at     TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (refund_id),
    UNIQUE KEY uk_refund_appointment (appointment_id),   -- one refund per appointment (0..1)
    CONSTRAINT fk_refund_appointment FOREIGN KEY (appointment_id)
        REFERENCES appointments (appointment_id),
    CONSTRAINT fk_refund_patient FOREIGN KEY (patient_id)
        REFERENCES patients (patient_id)
) ENGINE=InnoDB;