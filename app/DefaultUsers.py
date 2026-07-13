from .extensions import db
from .models import User, UserRole, Patient, TestCatalog, SampleType


# Emails of seeded demo accounts. These accounts are exempt from the
# "must change password on first login" enforcement popover.
DEFAULT_USER_EMAILS = {
    "admin@nmbhlab.com",
    "doctor@nmbhlab.com",
    "tech@nmbhlab.com",
    "manager@nmbhlab.com",
    "patient@nmbhlab.com",
}

DEFAULT_USER_PASSWORDS = {
    "admin@nmbhlab.com": "Admin@123",
    "doctor@nmbhlab.com": "Doctor@123",
    "tech@nmbhlab.com": "Tech@123",
    "manager@nmbhlab.com": "Manager@123",
    "patient@nmbhlab.com": "Patient@123",
}


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
        {"email": "admin@nmbhlab.com",   "password": DEFAULT_USER_PASSWORDS["admin@nmbhlab.com"],   "roles": ["admin"],          "name": "System Admin"},
        {"email": "doctor@nmbhlab.com",  "password": DEFAULT_USER_PASSWORDS["doctor@nmbhlab.com"],  "roles": ["doctor"],         "name": "Group Doctor"},
        {"email": "tech@nmbhlab.com",    "password": DEFAULT_USER_PASSWORDS["tech@nmbhlab.com"],    "roles": ["lab_technician"], "name": "Lab Technician"},
        {"email": "manager@nmbhlab.com", "password": DEFAULT_USER_PASSWORDS["manager@nmbhlab.com"], "roles": ["lab_manager"],    "name": "Lab Manager"},
        {"email": "patient@nmbhlab.com", "password": DEFAULT_USER_PASSWORDS["patient@nmbhlab.com"], "roles": ["patient"],        "name": "Jane Patient"},
    ]

    for u in users:
        existing = User.query.filter_by(email=u["email"]).first()
        if not existing:
            # Default seeded accounts are exempt from forced password change.
            user = User(email=u["email"], full_name=u["name"], must_change_password=False)
            user.set_password(u["password"])
            db.session.add(user)
            db.session.flush()
            for role in u["roles"]:
                db.session.add(UserRole(user_id=user.id, role=role))
            if "patient" in u["roles"]:
                db.session.add(Patient(
                    profile_id=user.id, mrn="MRN-" + user.id[:8],
                    full_name=u["name"], email=u["email"],
                ))
        else:
            # Make sure existing seeded accounts stay exempt across upgrades.
            if existing.must_change_password:
                existing.must_change_password = False

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
