import os
import secrets

from .extensions import db
from .models import User, UserRole, Patient, TestCatalog, SampleType


DEFAULT_USER_SPECS = [
    ("admin@nmbhlab.com", "DEFAULT_ADMIN_PASSWORD", ["admin"], "System Admin"),
    ("doctor@nmbhlab.com", "DEFAULT_DOCTOR_PASSWORD", ["doctor"], "Group Doctor"),
    ("tech@nmbhlab.com", "DEFAULT_TECHNICIAN_PASSWORD", ["lab_technician"], "Lab Technician"),
    ("manager@nmbhlab.com", "DEFAULT_MANAGER_PASSWORD", ["lab_manager"], "Lab Manager"),
    ("patient@nmbhlab.com", "DEFAULT_PATIENT_PASSWORD", ["patient"], "Jane Patient"),
]

def _seed_password(email, env_name):
    password = os.environ.get(env_name) or os.environ.get("DEFAULT_USER_PASSWORD")
    if password:
        return password, False
    password = secrets.token_urlsafe(12)
    print(f"Generated temporary password for seeded account {email}: {password}")
    return password, True


CATALOG = [
    ("CBC", "Complete Blood Count",       "Haematology", None,      None, None, 12),
    ("HGB", "Haemoglobin",                "Haematology", "g/dL",    12.0, 17.5, 8),
    ("WBC", "White Blood Cells",          "Haematology", "10^9/L",  4.0, 11.0, 10),
    ("PLT", "Platelet Count",             "Haematology", "10^9/L",  150, 450, 9),

    # ultra-fast ( < 5 min )
    ("GLU", "Random Glucose (POC)",       "Chemistry",   "mmol/L",  3.9, 5.5, 3),
    ("HGBPOC", "Point-of-Care Hb",        "Haematology", "g/dL",    12.0, 17.5, 2),
    ("URISTIX", "Urine Dipstick",         "Urine",       "",        None, None, 4),
    ("PREG", "Pregnancy Test",            "Serology",    "",        None, None, 3),

    ("CRE", "Creatinine",                 "Chemistry",   "umol/L",  60, 110, 12),
    ("ALT", "Alanine Aminotransferase",   "Chemistry",   "U/L",     7, 56, 13),
    ("AST", "Aspartate Aminotransferase", "Chemistry",   "U/L",     10, 40, 11),
    ("UREA", "Urea",                      "Chemistry",   "mmol/L",  2.5, 7.8, 8),
    ("CRP", "C-Reactive Protein",         "Inflammation", "mg/L",   0, 5, 10),
    ("ESR", "Erythrocyte Sedimentation",  "Inflammation", "mm/hr",  0, 20, 15),
    ("INR", "Internat. Normal. Ratio",    "Coagulation", "",        0.8, 1.2, 14),
    ("PT", "Prothrombin Time",            "Coagulation", "sec",     11, 15, 10),
    ("HIV", "HIV Screen",                 "Serology",    "",        None, None, 15),
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
    "GLU": "Serum",
    "HGBPOC": "Whole Blood",
    "URISTIX": "Urine",
    "PREG": "Urine",
    "CRE": "Serum",
    "ALT": "Serum",
    "AST": "Serum",
    "UREA": "Serum",
    "CRP": "Serum",
    "ESR": "EDTA Blood",
    "INR": "Citrated Plasma",
    "PT": "Citrated Plasma",
    "HIV": "Serum",
}

def create_default_users():
    users = [
        {"email": email, "env_name": env_name, "roles": roles, "name": name}
        for email, env_name, roles, name in DEFAULT_USER_SPECS
    ]

    for u in users:
        existing = User.query.filter_by(email=u["email"]).first()
        if not existing:
            password, generated = _seed_password(u["email"], u["env_name"])
            user = User(
                email=u["email"],
                full_name=u["name"],
                must_change_password=generated,
            )
            user.set_password(password)
            if generated:
                user.temp_password = password
            db.session.add(user)
            db.session.flush()
            for role in u["roles"]:
                db.session.add(UserRole(user_id=user.id, role=role))
            if "patient" in u["roles"]:
                db.session.add(Patient(
                    profile_id=user.id, mrn="MRN-" + user.id[:8],
                    full_name=u["name"], email=u["email"],
                ))
    # Drop any legacy super_admin role rows from older databases.
    UserRole.query.filter_by(role="super_admin").delete()

    for sample_type in DEFAULT_SAMPLE_TYPES:
        if not SampleType.query.filter_by(name=sample_type).first():
            db.session.add(SampleType(
                name=sample_type,
                description=f"{sample_type} sample type",
                active=True,
            ))

    # Seed test catalog if empty so patients/doctors have tests to pick.
    if not TestCatalog.query.first():
        for code, name, cat, units, lo, hi, tat in CATALOG:
            db.session.add(TestCatalog(
                code=code, name=name, category=cat, units=units,
                reference_low=lo, reference_high=hi,
                sample_type=CATALOG_SAMPLE_TYPES.get(code),
                turnaround_hours=tat, active=True,
            ))

    # Backfill sample types for seeded/default tests created before sample_type
    # was saved with the catalog record.
    for code, sample_type in CATALOG_SAMPLE_TYPES.items():
        test = TestCatalog.query.filter_by(code=code).first()
        if test and not test.sample_type:
            test.sample_type = sample_type

    db.session.commit()
    db.session.close()
