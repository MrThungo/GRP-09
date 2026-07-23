"""Seed default users + sample test catalog using SQLAlchemy ORM."""
import os
import secrets

from .extensions import db
from .models import User, UserRole, Patient, TestCatalog, SampleType


DEFAULT_USERS = [
    (
        "superadmin@nmb.example.com",
        "SEED_SUPER_ADMIN_PASSWORD",
        "Super Admin",
        ("admin", "super_admin"),
    ),
    ("admin@nmb.example.com",      "SEED_ADMIN_PASSWORD",      "Admin User",   ("admin",)),
    ("manager@nmb.example.com",    "SEED_MANAGER_PASSWORD",    "Lab Manager",  ("lab_manager",)),
    ("doctor@nmb.example.com",     "SEED_DOCTOR_PASSWORD",     "Dr. House",    ("doctor",)),
    ("technician@nmb.example.com", "SEED_TECHNICIAN_PASSWORD", "Tech User",    ("lab_technician",)),
    ("patient@nmb.example.com",    "SEED_PATIENT_PASSWORD",    "Jane Patient", ("patient",)),
]

CATALOG = [
    ("CBC", "Complete Blood Count",      "Haematology", None,    None, None,  24),
    ("HGB", "Haemoglobin",               "Haematology", "g/dL",  12.0, 17.5,  4),
    ("WBC", "White Blood Cells",         "Haematology", "10^9/L", 4.0, 11.0,  4),
    ("PLT", "Platelet Count",            "Haematology", "10^9/L", 150,  450,  4),
    ("INR", "Internat. Normal. Ratio",   "Coagulation", "",       0.8, 1.2,   6),
    ("GLU", "Fasting Glucose",           "Chemistry",   "mmol/L", 3.9, 5.5,   4),
    ("CRE", "Creatinine",                "Chemistry",   "umol/L", 60,  110,   8),
]

DEFAULT_SAMPLE_TYPES = [
    "Whole Blood",
    "EDTA Blood",
    "Citrated Plasma",
    "Plasma",
    "Serum",
    "Urine",
    "Bone Marrow Aspirate",
]

CATALOG_SAMPLE_TYPES = {
    "CBC": "EDTA Blood",
    "HGB": "EDTA Blood",
    "WBC": "EDTA Blood",
    "PLT": "EDTA Blood",
    "INR": "Citrated Plasma",
    "GLU": "Serum",
    "CRE": "Serum",
}


def _seed_password(email, env_name):
    password = os.environ.get(env_name) or os.environ.get("SEED_DEFAULT_USER_PASSWORD")
    if password:
        return password, False
    password = secrets.token_urlsafe(12)
    print(f"Generated temporary password for seeded account {email}: {password}")
    return password, True


def seed_database():
    db.create_all()

    if User.query.first():
        return

    for email, env_name, name, roles in DEFAULT_USERS:
        password, generated = _seed_password(email, env_name)
        user = User(
            email=email,
            full_name=name,
            must_change_password=generated,
        )
        user.set_password(password)
        if generated:
            user.temp_password = password
        db.session.add(user)
        db.session.flush()
        for role in roles:
            db.session.add(UserRole(user_id=user.id, role=role))
        if "patient" in roles:
            db.session.add(Patient(
                profile_id=user.id,
                mrn="MRN-" + user.id[:8],
                full_name=name,
                email=email,
            ))

    for code, name, cat, units, lo, hi, tat in CATALOG:
        db.session.add(TestCatalog(
            code=code, name=name, category=cat, units=units,
            reference_low=lo, reference_high=hi,
            sample_type=CATALOG_SAMPLE_TYPES.get(code),
            turnaround_hours=tat, active=True,
        ))

    for sample_type in DEFAULT_SAMPLE_TYPES:
        db.session.add(SampleType(
            name=sample_type,
            description=f"{sample_type} sample type",
            active=True,
        ))

    db.session.commit()
