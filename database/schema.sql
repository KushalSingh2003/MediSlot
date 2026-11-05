-- Medi-slot schema (MySQL 8 / Amazon RDS)
-- Parent tables are created before the tables that reference them.

-- =========================================================
-- Lookup / reference tables
-- =========================================================

CREATE TABLE specialisations (
    specialisation_id  INT AUTO_INCREMENT PRIMARY KEY,
    name               VARCHAR(255) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE language (
    language_id  INT AUTO_INCREMENT PRIMARY KEY,
    name         VARCHAR(100) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE hospitals (
    hospital_id  INT AUTO_INCREMENT PRIMARY KEY,
    name         VARCHAR(255) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE department (
    department_id  INT AUTO_INCREMENT PRIMARY KEY,
    name           VARCHAR(255) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE consultation_type (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    name          VARCHAR(255) NOT NULL,
    duration_min  DECIMAL(6,2) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE slot_template (
    template_id  INT AUTO_INCREMENT PRIMARY KEY,
    name         VARCHAR(255) NOT NULL,
    is_active    BOOLEAN NOT NULL DEFAULT TRUE,
    working_start_time TIME NOT NULL DEFAULT '09:00:00',
    working_end_time TIME NOT NULL DEFAULT '16:00:00'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


-- =========================================================
-- Doctor and doctor-related tables
-- =========================================================

CREATE TABLE doctor (
    doctor_id         VARCHAR(36) PRIMARY KEY,
    email             VARCHAR(255) NOT NULL UNIQUE,
    password_hash     VARCHAR(255) NOT NULL,
    name              VARCHAR(255) NOT NULL,
    phone             VARCHAR(30),
    years_experience  INT,
    photo_key         VARCHAR(512),
    created_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE doctor_licence (
    doctor_id            VARCHAR(36) NOT NULL,
    licence_no           VARCHAR(100) NOT NULL UNIQUE,
    authority            VARCHAR(255),
    dates                VARCHAR(100),
    verification_status  VARCHAR(50) NOT NULL DEFAULT 'PENDING',
    FOREIGN KEY (doctor_id) REFERENCES doctor(doctor_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE doctor_address (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    doctor_id       VARCHAR(36) NOT NULL,
    address_line_1  VARCHAR(255) NOT NULL,
    address_line_2  VARCHAR(255),
    city            VARCHAR(100) NOT NULL,
    postal_code     VARCHAR(20),
    FOREIGN KEY (doctor_id) REFERENCES doctor(doctor_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE doctor_specialisation (
    doctor_id          VARCHAR(36) NOT NULL,
    specialisation_id  INT NOT NULL,
    PRIMARY KEY (doctor_id, specialisation_id),
    FOREIGN KEY (doctor_id) REFERENCES doctor(doctor_id) ON DELETE CASCADE,
    FOREIGN KEY (specialisation_id) REFERENCES specialisations(specialisation_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE doctor_qualification (
    id           INT AUTO_INCREMENT PRIMARY KEY,
    doctor_id    VARCHAR(36) NOT NULL,
    degree       VARCHAR(255) NOT NULL,
    institution  VARCHAR(255),
    year         INT,
    FOREIGN KEY (doctor_id) REFERENCES doctor(doctor_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE blackout (
    id INT AUTO_INCREMENT PRIMARY KEY,
    doctor_id VARCHAR(36) NOT NULL,
    blackout_date DATE NOT NULL,

    CONSTRAINT fk_blackout_doctor
        FOREIGN KEY (doctor_id)
        REFERENCES doctor(doctor_id)
        ON DELETE CASCADE,

    CONSTRAINT unique_doctor_blackout
        UNIQUE (doctor_id, blackout_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE doctor_language (
    doctor_id    VARCHAR(36) NOT NULL,
    language_id  INT NOT NULL,
    PRIMARY KEY (doctor_id, language_id),
    FOREIGN KEY (doctor_id) REFERENCES doctor(doctor_id) ON DELETE CASCADE,
    FOREIGN KEY (language_id) REFERENCES language(language_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE doctor_hospital (
    doctor_id      VARCHAR(36) NOT NULL,
    hospital_id    INT NOT NULL,
    department_id  INT NOT NULL,
    PRIMARY KEY (doctor_id, hospital_id, department_id),
    FOREIGN KEY (doctor_id) REFERENCES doctor(doctor_id) ON DELETE CASCADE,
    FOREIGN KEY (hospital_id) REFERENCES hospitals(hospital_id),
    FOREIGN KEY (department_id) REFERENCES department(department_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE doctor_consultation_type (
    doctor_id             VARCHAR(36) NOT NULL,
    consultation_type_id  INT NOT NULL,
    fee                   DECIMAL(10,2) NOT NULL,
    capacity              INT NOT NULL DEFAULT 1,
    active                BOOLEAN NOT NULL DEFAULT TRUE,
    PRIMARY KEY (doctor_id, consultation_type_id),
    FOREIGN KEY (doctor_id) REFERENCES doctor(doctor_id) ON DELETE CASCADE,
    FOREIGN KEY (consultation_type_id) REFERENCES consultation_type(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- =========================================================
-- Slot templates
-- =========================================================
CREATE TABLE template_consultation_rule (
    rule_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    template_id INT NOT NULL,
    day_of_week TINYINT NOT NULL,
    consultation_type_id INT NOT NULL,
    allocation_weight INT NOT NULL DEFAULT 1,
    time_period ENUM('MORNING', 'AFTERNOON', 'ANY')
        NOT NULL DEFAULT 'ANY',
    is_active BOOLEAN NOT NULL DEFAULT TRUE,

    CONSTRAINT fk_rule_template
        FOREIGN KEY (template_id)
        REFERENCES slot_template(template_id)
        ON DELETE CASCADE,

    CONSTRAINT fk_rule_consultation_type
        FOREIGN KEY (consultation_type_id)
        REFERENCES consultation_type(id),

    CONSTRAINT chk_rule_weekday
        CHECK (day_of_week BETWEEN 1 AND 7),

    CONSTRAINT chk_rule_weight
        CHECK (allocation_weight > 0),

    UNIQUE (
        template_id,
        day_of_week,
        consultation_type_id,
        time_period
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- =========================================================
-- Patients, slots, appointments, refunds
-- =========================================================

CREATE TABLE patients (
    patient_id     VARCHAR(36) PRIMARY KEY,
    name           VARCHAR(255) NOT NULL,
    phone          VARCHAR(30),
    email          VARCHAR(255) NOT NULL UNIQUE,
    password_hash  VARCHAR(255) NOT NULL,
    city           VARCHAR(100),
    date_of_birth  DATE,
    gender         VARCHAR(20),
    created_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE appointment_slot (
    slot_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    doctor_id VARCHAR(36) NOT NULL,
    consultation_type_id INT NOT NULL,
    slot_date DATE NOT NULL,
    start_time TIME NOT NULL,
    end_time TIME NOT NULL,
    status VARCHAR(30) NOT NULL DEFAULT 'AVAILABLE',
    hold_expires_at DATETIME NULL,
    capacity INT NOT NULL DEFAULT 1,
    booked_count INT NOT NULL DEFAULT 0,

    CONSTRAINT fk_slot_doctor
        FOREIGN KEY (doctor_id)
        REFERENCES doctor(doctor_id),

    CONSTRAINT fk_slot_consultation_type
        FOREIGN KEY (consultation_type_id)
        REFERENCES consultation_type(id),

    CONSTRAINT chk_slot_time
        CHECK (end_time > start_time),

    CONSTRAINT chk_slot_capacity
        CHECK (
            capacity > 0
            AND booked_count >= 0
            AND booked_count <= capacity
        ),

    UNIQUE (doctor_id, slot_date, start_time, end_time),

    INDEX idx_slot_availability
        (doctor_id, slot_date, status)  
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE appointments (
    appointment_id    BIGINT AUTO_INCREMENT PRIMARY KEY,
    slot_id           BIGINT NOT NULL,
    patient_id        VARCHAR(36) NOT NULL,
    status            VARCHAR(30) NOT NULL DEFAULT 'BOOKED',
    created_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    reschedule_count  INT NOT NULL DEFAULT 0,
    FOREIGN KEY (slot_id) REFERENCES appointment_slot(slot_id),
    FOREIGN KEY (patient_id) REFERENCES patients(patient_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE refund_ledger (
    refund_id       BIGINT AUTO_INCREMENT PRIMARY KEY,
    appointment_id  BIGINT NOT NULL UNIQUE,
    patient_id      VARCHAR(36) NOT NULL,
    refund_percent  INT NOT NULL,
    refund_amount   DECIMAL(10,2) NOT NULL,
    reason          VARCHAR(500),
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (appointment_id) REFERENCES appointments(appointment_id),
    FOREIGN KEY (patient_id) REFERENCES patients(patient_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;