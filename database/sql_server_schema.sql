-- NMB-HLabSys SQL Server schema.
-- Run manually in SQL Server Management Studio before using a mssql+pyodbc
-- DATABASE_URL. The Flask app does not auto-create SQL Server tables.

CREATE TABLE users (
    id NVARCHAR(36) NOT NULL PRIMARY KEY,
    email NVARCHAR(255) NOT NULL UNIQUE,
    password_hash NVARCHAR(255) NOT NULL,
    temp_password NVARCHAR(64) NULL,
    title NVARCHAR(32) NULL,
    full_name NVARCHAR(255) NULL,
    surname NVARCHAR(120) NULL,
    gender NVARCHAR(32) NULL,
    phone NVARCHAR(50) NULL,
    employee_number NVARCHAR(64) NULL UNIQUE,
    sa_id_number NVARCHAR(32) NULL UNIQUE,
    hpcsa_number NVARCHAR(64) NULL UNIQUE,
    avatar_url NVARCHAR(500) NULL,
    must_change_password BIT NOT NULL DEFAULT 1,
    is_blocked BIT NOT NULL DEFAULT 0,
    is_deactivated BIT NOT NULL DEFAULT 0,
    deactivated_at DATETIME2 NULL,
    last_seen DATETIME2 NULL,
    created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    updated_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()
);
CREATE INDEX ix_users_email ON users(email);

CREATE TABLE user_roles (
    id NVARCHAR(36) NOT NULL PRIMARY KEY,
    user_id NVARCHAR(36) NOT NULL,
    role NVARCHAR(32) NOT NULL,
    created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    CONSTRAINT uq_user_roles_user_role UNIQUE(user_id, role),
    CONSTRAINT fk_user_roles_user FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE conditions (
    id NVARCHAR(36) NOT NULL PRIMARY KEY,
    name NVARCHAR(120) NOT NULL UNIQUE,
    category NVARCHAR(64) NOT NULL DEFAULT 'General',
    description NVARCHAR(MAX) NULL,
    active BIT NOT NULL DEFAULT 1,
    created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()
);
CREATE TABLE allergies (
    id NVARCHAR(36) NOT NULL PRIMARY KEY,
    name NVARCHAR(120) NOT NULL UNIQUE,
    category NVARCHAR(64) NOT NULL DEFAULT 'General',
    description NVARCHAR(MAX) NULL,
    active BIT NOT NULL DEFAULT 1,
    created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()
);
CREATE TABLE medications (
    id NVARCHAR(36) NOT NULL PRIMARY KEY,
    name NVARCHAR(120) NOT NULL UNIQUE,
    category NVARCHAR(64) NOT NULL DEFAULT 'General',
    description NVARCHAR(MAX) NULL,
    active BIT NOT NULL DEFAULT 1,
    created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()
);
CREATE TABLE test_categories (
    id NVARCHAR(36) NOT NULL PRIMARY KEY,
    name NVARCHAR(120) NOT NULL UNIQUE,
    description NVARCHAR(MAX) NULL,
    active BIT NOT NULL DEFAULT 1,
    created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()
);
CREATE TABLE sample_types (
    id NVARCHAR(36) NOT NULL PRIMARY KEY,
    name NVARCHAR(120) NOT NULL UNIQUE,
    description NVARCHAR(MAX) NULL,
    active BIT NOT NULL DEFAULT 1,
    created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()
);

CREATE TABLE patients (
    id NVARCHAR(36) NOT NULL PRIMARY KEY,
    profile_id NVARCHAR(36) NULL UNIQUE,
    mrn NVARCHAR(64) NOT NULL UNIQUE,
    full_name NVARCHAR(255) NOT NULL,
    surname NVARCHAR(120) NULL,
    id_number NVARCHAR(32) NULL UNIQUE,
    date_of_birth DATE NULL,
    gender NVARCHAR(32) NULL,
    blood_type NVARCHAR(8) NULL,
    phone NVARCHAR(50) NULL,
    email NVARCHAR(255) NULL,
    address NVARCHAR(MAX) NULL,
    chronic_conditions NVARCHAR(MAX) NULL,
    allergies NVARCHAR(MAX) NULL,
    current_medication NVARCHAR(MAX) NULL,
    created_by NVARCHAR(36) NULL,
    created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    updated_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    CONSTRAINT fk_patients_profile FOREIGN KEY(profile_id) REFERENCES users(id) ON DELETE SET NULL,
    CONSTRAINT fk_patients_created_by FOREIGN KEY(created_by) REFERENCES users(id)
);

CREATE TABLE patient_conditions (
    patient_id NVARCHAR(36) NOT NULL,
    condition_id NVARCHAR(36) NOT NULL,
    PRIMARY KEY(patient_id, condition_id),
    FOREIGN KEY(patient_id) REFERENCES patients(id) ON DELETE CASCADE,
    FOREIGN KEY(condition_id) REFERENCES conditions(id) ON DELETE CASCADE
);
CREATE TABLE patient_allergies (
    patient_id NVARCHAR(36) NOT NULL,
    allergy_id NVARCHAR(36) NOT NULL,
    PRIMARY KEY(patient_id, allergy_id),
    FOREIGN KEY(patient_id) REFERENCES patients(id) ON DELETE CASCADE,
    FOREIGN KEY(allergy_id) REFERENCES allergies(id) ON DELETE CASCADE
);
CREATE TABLE patient_medications (
    patient_id NVARCHAR(36) NOT NULL,
    medication_id NVARCHAR(36) NOT NULL,
    PRIMARY KEY(patient_id, medication_id),
    FOREIGN KEY(patient_id) REFERENCES patients(id) ON DELETE CASCADE,
    FOREIGN KEY(medication_id) REFERENCES medications(id) ON DELETE CASCADE
);

CREATE TABLE suppliers (
    id NVARCHAR(36) NOT NULL PRIMARY KEY,
    name NVARCHAR(255) NOT NULL UNIQUE,
    contact_name NVARCHAR(255) NULL,
    email NVARCHAR(255) NULL,
    phone NVARCHAR(50) NULL,
    address NVARCHAR(MAX) NULL,
    active BIT NOT NULL DEFAULT 1,
    created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    updated_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()
);

CREATE TABLE consumables (
    id NVARCHAR(36) NOT NULL PRIMARY KEY,
    sku NVARCHAR(64) NOT NULL UNIQUE,
    name NVARCHAR(255) NOT NULL UNIQUE,
    category NVARCHAR(64) NOT NULL,
    unit NVARCHAR(32) NOT NULL DEFAULT 'unit',
    supplier_id NVARCHAR(36) NULL,
    reorder_level INT NOT NULL DEFAULT 10,
    current_stock INT NOT NULL DEFAULT 0,
    unit_cost DECIMAL(12,2) NULL,
    expiry_date DATE NULL,
    active BIT NOT NULL DEFAULT 1,
    created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    updated_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    CONSTRAINT fk_consumables_supplier FOREIGN KEY(supplier_id) REFERENCES suppliers(id) ON DELETE SET NULL
);

CREATE TABLE test_catalog (
    id NVARCHAR(36) NOT NULL PRIMARY KEY,
    code NVARCHAR(32) NOT NULL UNIQUE,
    name NVARCHAR(255) NOT NULL UNIQUE,
    category NVARCHAR(64) NOT NULL,
    units NVARCHAR(32) NULL,
    reference_low DECIMAL(12,3) NULL,
    reference_high DECIMAL(12,3) NULL,
    reference_text NVARCHAR(MAX) NULL,
    turnaround_hours INT NOT NULL DEFAULT 1440,
    active BIT NOT NULL DEFAULT 1,
    created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    sample_type NVARCHAR(100) NULL,
    consumables_used NVARCHAR(MAX) NULL,
    assigned_technician NVARCHAR(100) NULL
);

CREATE TABLE test_consumables (
    id NVARCHAR(36) NOT NULL PRIMARY KEY,
    test_id NVARCHAR(36) NULL,
    consumable_id NVARCHAR(36) NULL,
    quantity_required INT NOT NULL DEFAULT 1,
    FOREIGN KEY(test_id) REFERENCES test_catalog(id),
    FOREIGN KEY(consumable_id) REFERENCES consumables(id)
);

CREATE TABLE technician_tests (
    id NVARCHAR(36) NOT NULL PRIMARY KEY,
    technician_id NVARCHAR(36) NULL,
    test_id NVARCHAR(36) NULL,
    FOREIGN KEY(technician_id) REFERENCES users(id),
    FOREIGN KEY(test_id) REFERENCES test_catalog(id)
);

CREATE TABLE test_requests (
    id NVARCHAR(36) NOT NULL PRIMARY KEY,
    request_number NVARCHAR(64) NOT NULL UNIQUE,
    patient_id NVARCHAR(36) NOT NULL,
    doctor_id NVARCHAR(36) NULL,
    status NVARCHAR(32) NOT NULL DEFAULT 'submitted',
    priority NVARCHAR(16) NOT NULL DEFAULT 'routine',
    clinical_notes NVARCHAR(MAX) NULL,
    release_note NVARCHAR(MAX) NULL,
    released_at DATETIME2 NULL,
    cancel_reason NVARCHAR(MAX) NULL,
    cancelled_by NVARCHAR(36) NULL,
    cancelled_at DATETIME2 NULL,
    created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    updated_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    FOREIGN KEY(patient_id) REFERENCES patients(id),
    FOREIGN KEY(doctor_id) REFERENCES users(id),
    FOREIGN KEY(cancelled_by) REFERENCES users(id)
);
CREATE INDEX idx_requests_doctor ON test_requests(doctor_id);
CREATE INDEX idx_requests_patient ON test_requests(patient_id);
CREATE INDEX idx_requests_status ON test_requests(status);

CREATE TABLE test_request_items (
    id NVARCHAR(36) NOT NULL PRIMARY KEY,
    request_id NVARCHAR(36) NOT NULL,
    test_id NVARCHAR(36) NOT NULL,
    status NVARCHAR(16) NOT NULL DEFAULT 'submitted',
    result_value DECIMAL(12,3) NULL,
    result_text NVARCHAR(MAX) NULL,
    result_notes NVARCHAR(MAX) NULL,
    abnormal_flag NVARCHAR(16) NULL,
    assigned_to NVARCHAR(36) NULL,
    captured_by NVARCHAR(36) NULL,
    started_at DATETIME2 NULL,
    captured_at DATETIME2 NULL,
    completed_at DATETIME2 NULL,
    verified_by NVARCHAR(36) NULL,
    verified_at DATETIME2 NULL,
    verification_notes NVARCHAR(MAX) NULL,
    review_notes NVARCHAR(MAX) NULL,
    created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    FOREIGN KEY(request_id) REFERENCES test_requests(id) ON DELETE CASCADE,
    FOREIGN KEY(test_id) REFERENCES test_catalog(id),
    FOREIGN KEY(assigned_to) REFERENCES users(id),
    FOREIGN KEY(captured_by) REFERENCES users(id),
    FOREIGN KEY(verified_by) REFERENCES users(id)
);
CREATE INDEX idx_request_items_request ON test_request_items(request_id);

CREATE TABLE samples (
    id NVARCHAR(36) NOT NULL PRIMARY KEY,
    request_id NVARCHAR(36) NOT NULL,
    barcode NVARCHAR(100) NOT NULL UNIQUE,
    sample_type NVARCHAR(50) NOT NULL,
    status NVARCHAR(20) NOT NULL DEFAULT 'collected',
    collected_at DATETIME2 NULL,
    received_by NVARCHAR(36) NULL,
    received_at DATETIME2 NULL,
    FOREIGN KEY(request_id) REFERENCES test_requests(id) ON DELETE CASCADE,
    FOREIGN KEY(received_by) REFERENCES users(id)
);

CREATE TABLE test_result_reviews (
    id NVARCHAR(36) NOT NULL PRIMARY KEY,
    item_id NVARCHAR(36) NOT NULL,
    reviewer_id NVARCHAR(36) NOT NULL,
    action NVARCHAR(24) NOT NULL,
    note NVARCHAR(MAX) NULL,
    created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    FOREIGN KEY(item_id) REFERENCES test_request_items(id) ON DELETE CASCADE,
    FOREIGN KEY(reviewer_id) REFERENCES users(id)
);

CREATE TABLE online_consultations (
    id NVARCHAR(36) NOT NULL PRIMARY KEY,
    request_id NVARCHAR(36) NOT NULL,
    patient_id NVARCHAR(36) NOT NULL,
    doctor_id NVARCHAR(36) NOT NULL,
    requested_by_id NVARCHAR(36) NULL,
    status NVARCHAR(24) NOT NULL DEFAULT 'offered',
    patient_preference NVARCHAR(24) NULL,
    patient_response NVARCHAR(24) NULL,
    offered_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    patient_responded_at DATETIME2 NULL,
    scheduled_at DATETIME2 NULL,
    scheduled_end_at DATETIME2 NULL,
    doctor_started_at DATETIME2 NULL,
    ended_at DATETIME2 NULL,
    invite_message NVARCHAR(MAX) NULL,
    decline_reason NVARCHAR(MAX) NULL,
    session_record_filename NVARCHAR(255) NULL,
    session_record_mime NVARCHAR(80) NULL,
    session_record_size INT NULL,
    session_record_body NVARCHAR(MAX) NULL,
    room_token NVARCHAR(64) NOT NULL,
    created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    updated_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    FOREIGN KEY(request_id) REFERENCES test_requests(id) ON DELETE CASCADE,
    FOREIGN KEY(patient_id) REFERENCES patients(id) ON DELETE CASCADE,
    FOREIGN KEY(doctor_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY(requested_by_id) REFERENCES users(id)
);
CREATE INDEX idx_online_consults_doctor_status ON online_consultations(doctor_id, status, scheduled_at);
CREATE INDEX idx_online_consults_patient_status ON online_consultations(patient_id, status, scheduled_at);
CREATE INDEX idx_online_consults_request_created ON online_consultations(request_id, created_at);

CREATE TABLE consultation_signals (
    id NVARCHAR(36) NOT NULL PRIMARY KEY,
    consultation_id NVARCHAR(36) NOT NULL,
    sender_id NVARCHAR(36) NOT NULL,
    signal_type NVARCHAR(32) NOT NULL,
    payload NVARCHAR(MAX) NOT NULL,
    created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    FOREIGN KEY(consultation_id) REFERENCES online_consultations(id) ON DELETE CASCADE,
    FOREIGN KEY(sender_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX idx_consult_signals_room_created ON consultation_signals(consultation_id, created_at);

CREATE TABLE stock_movements (
    id NVARCHAR(36) NOT NULL PRIMARY KEY,
    consumable_id NVARCHAR(36) NOT NULL,
    movement_type NVARCHAR(16) NOT NULL,
    quantity INT NOT NULL,
    notes NVARCHAR(MAX) NULL,
    created_by NVARCHAR(36) NULL,
    created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    FOREIGN KEY(consumable_id) REFERENCES consumables(id) ON DELETE CASCADE
);

CREATE TABLE notifications (
    id NVARCHAR(36) NOT NULL PRIMARY KEY,
    user_id NVARCHAR(36) NOT NULL,
    title NVARCHAR(255) NOT NULL,
    body NVARCHAR(MAX) NULL,
    link NVARCHAR(500) NULL,
    read BIT NOT NULL DEFAULT 0,
    created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()
);
CREATE INDEX ix_notifications_user_id ON notifications(user_id);

CREATE TABLE audit_logs (
    id NVARCHAR(36) NOT NULL PRIMARY KEY,
    actor_id NVARCHAR(36) NULL,
    action NVARCHAR(64) NOT NULL,
    entity_type NVARCHAR(64) NULL,
    entity_id NVARCHAR(36) NULL,
    details NVARCHAR(MAX) NULL,
    created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()
);
CREATE INDEX ix_audit_logs_created_at ON audit_logs(created_at);

CREATE TABLE consent_grants (
    id NVARCHAR(36) NOT NULL PRIMARY KEY,
    patient_id NVARCHAR(36) NOT NULL,
    doctor_id NVARCHAR(36) NOT NULL,
    granted_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    revoked_at DATETIME2 NULL,
    note NVARCHAR(MAX) NULL,
    FOREIGN KEY(patient_id) REFERENCES patients(id) ON DELETE CASCADE,
    FOREIGN KEY(doctor_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE consent_grant_items (
    grant_id NVARCHAR(36) NOT NULL,
    request_id NVARCHAR(36) NOT NULL,
    PRIMARY KEY(grant_id, request_id),
    FOREIGN KEY(grant_id) REFERENCES consent_grants(id) ON DELETE CASCADE,
    FOREIGN KEY(request_id) REFERENCES test_requests(id) ON DELETE CASCADE
);

CREATE TABLE access_requests (
    id NVARCHAR(36) NOT NULL PRIMARY KEY,
    doctor_id NVARCHAR(36) NOT NULL,
    patient_id NVARCHAR(36) NOT NULL,
    status NVARCHAR(16) NOT NULL DEFAULT 'pending',
    note NVARCHAR(MAX) NULL,
    created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    responded_at DATETIME2 NULL,
    grant_id NVARCHAR(36) NULL,
    FOREIGN KEY(doctor_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY(patient_id) REFERENCES patients(id) ON DELETE CASCADE,
    FOREIGN KEY(grant_id) REFERENCES consent_grants(id) ON DELETE SET NULL
);
CREATE INDEX ix_access_requests_created_at ON access_requests(created_at);

CREATE TABLE consumable_orders (
    id NVARCHAR(36) NOT NULL PRIMARY KEY,
    order_number NVARCHAR(64) NOT NULL UNIQUE,
    supplier_id NVARCHAR(36) NULL,
    consumable_id NVARCHAR(36) NULL,
    quantity INT NOT NULL DEFAULT 1,
    ordered_at DATETIME2 NULL,
    received_at DATETIME2 NULL,
    completed_at DATETIME2 NULL,
    cancelled_at DATETIME2 NULL,
    cancel_reason NVARCHAR(MAX) NULL,
    supplier_notified_at DATETIME2 NULL,
    received_quantity INT NOT NULL DEFAULT 0,
    status NVARCHAR(20) NOT NULL DEFAULT 'ordered',
    FOREIGN KEY(supplier_id) REFERENCES suppliers(id),
    FOREIGN KEY(consumable_id) REFERENCES consumables(id)
);

CREATE TABLE consumable_order_items (
    id NVARCHAR(36) NOT NULL PRIMARY KEY,
    order_id NVARCHAR(36) NOT NULL,
    consumable_id NVARCHAR(36) NOT NULL,
    quantity INT NOT NULL DEFAULT 1,
    received_quantity INT NOT NULL DEFAULT 0,
    status NVARCHAR(20) NOT NULL DEFAULT 'ordered',
    received_at DATETIME2 NULL,
    cancelled_at DATETIME2 NULL,
    cancel_reason NVARCHAR(MAX) NULL,
    FOREIGN KEY(order_id) REFERENCES consumable_orders(id) ON DELETE CASCADE,
    FOREIGN KEY(consumable_id) REFERENCES consumables(id)
);

INSERT INTO test_categories (id, name, description) VALUES
(CONVERT(NVARCHAR(36), NEWID()), 'Full Blood Count', 'Full Blood Count'),
(CONVERT(NVARCHAR(36), NEWID()), 'Differential Count', 'Differential Count'),
(CONVERT(NVARCHAR(36), NEWID()), 'Peripheral Blood Film', 'Peripheral Blood Film'),
(CONVERT(NVARCHAR(36), NEWID()), 'Coagulation Studies', 'Coagulation Studies'),
(CONVERT(NVARCHAR(36), NEWID()), 'Haematology', 'Haematology'),
(CONVERT(NVARCHAR(36), NEWID()), 'Chemistry', 'Chemistry'),
(CONVERT(NVARCHAR(36), NEWID()), 'Serology', 'Serology');

INSERT INTO sample_types (id, name, description) VALUES
(CONVERT(NVARCHAR(36), NEWID()), 'Whole Blood', 'Whole Blood'),
(CONVERT(NVARCHAR(36), NEWID()), 'EDTA Blood', 'EDTA Blood'),
(CONVERT(NVARCHAR(36), NEWID()), 'Citrated Plasma', 'Citrated Plasma'),
(CONVERT(NVARCHAR(36), NEWID()), 'Plasma', 'Plasma'),
(CONVERT(NVARCHAR(36), NEWID()), 'Serum', 'Serum'),
(CONVERT(NVARCHAR(36), NEWID()), 'Urine', 'Urine'),
(CONVERT(NVARCHAR(36), NEWID()), 'Bone Marrow Aspirate', 'Bone Marrow Aspirate');
