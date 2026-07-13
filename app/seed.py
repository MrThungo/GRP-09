"""Seed default users + sample test catalog using SQLAlchemy ORM."""
from .extensions import db
from .models import User, UserRole, Patient, TestCatalog, SampleType


DEFAULT_USERS = [
    ("admin@nmb.example.com",      "admin123",   "Admin User",   "admin"),
    ("manager@nmb.example.com",    "manager123", "Lab Manager",  "lab_manager"),
    ("doctor@nmb.example.com",     "doctor123",  "Dr. House",    "doctor"),
    ("technician@nmb.example.com", "tech123",    "Tech User",    "lab_technician"),
    ("patient@nmb.example.com",    "patient123", "Jane Patient", "patient"),
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


def seed_database():
    db.create_all()

    if User.query.first():
        return

    for email, pwd, name, role in DEFAULT_USERS:
        user = User(email=email, full_name=name, must_change_password=False)
        user.set_password(pwd)
        db.session.add(user)
        db.session.flush()
        db.session.add(UserRole(user_id=user.id, role=role))
        if role == "patient":
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
