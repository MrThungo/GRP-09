import os
import uuid
from flask import Blueprint, render_template, request, redirect, url_for, flash, send_file, abort, current_app
from flask_login import login_required, current_user
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from datetime import date, datetime, timedelta
import json
import secrets
from werkzeug.utils import secure_filename
from ..extensions import db
from ..auth_utils import role_required
from ..models import (
    TestRequest, TestRequestItem, Consumable, Supplier, TestCatalog, StockMovement,
    TechnicianTest, UserRole, User, ConsumableOrder, ConsumableOrderItem,
    TestConsumable, TestCategory, SampleType, Patient, Condition, Allergy, Medication,
    ROLE_LABELS, TITLE_OPTIONS, GENDER_OPTIONS,
)
from ..notification_pages import clear_user_notifications, mark_user_notifications_read, render_user_notifications
from ..services import apply_stock_movement, log_audit, send_email
from ..reports import build_report_pdf, parse_range
from ..sa_id import validate_sa_id
from ..soft_delete import soft_delete, restore
from ..whatsapp import send_account_welcome_whatsapp

bp = Blueprint("manager", __name__, template_folder="../templates/manager")
ALLOWED_AVATAR_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

TEST_CATEGORIES = [
    "Haematology",
    "Chemistry",
    "Coagulation",
    "Serology",
    "Inflammation",
    "Urine",
    "Full Blood Count",
    "ESR Testing",
]

SAMPLE_TYPES = [
    "Whole Blood",
    "EDTA Blood",
    "Citrated Plasma",
    "Plasma",
    "Serum",
    "Urine",
    "Bone Marrow Aspirate",
]


def _ensure_reference_data():
    for name in TEST_CATEGORIES:
        if not TestCategory.query.filter_by(name=name).first():
            db.session.add(TestCategory(name=name, description=f"{name} test category"))
    for name in SAMPLE_TYPES:
        if not SampleType.query.filter_by(name=name).first():
            db.session.add(SampleType(name=name, description=f"{name} sample type"))
    db.session.commit()


def _category_names():
    _ensure_reference_data()
    return [row.name for row in TestCategory.query.filter_by(active=True).order_by(TestCategory.name).all()]


def _sample_type_names():
    _ensure_reference_data()
    return [row.name for row in SampleType.query.filter_by(active=True).order_by(SampleType.name).all()]


def _sample_type_choices(current=None):
    choices = _sample_type_names()
    current = (current or "").strip()
    if current and current not in choices:
        choices.append(current)
    return choices


def _posted_sample_type(extra_allowed=None):
    value = (request.form.get("sample_type") or "").strip()
    if not value:
        return None, "Sample type is required."
    choices = set(_sample_type_names())
    choices.update((item or "").strip() for item in (extra_allowed or []) if (item or "").strip())
    if value not in choices:
        return None, "Please select a valid active sample type."
    return value, None


def _live(model):
    return model.query.filter(model.deleted_at.is_(None))


def _live_or_404(model, record_id):
    record = db.session.get(model, record_id)
    if not record or getattr(record, "deleted_at", None):
        abort(404)
    return record


def _normalise_sa_id(value):
    raw = (value or "").strip()
    return "".join(ch for ch in raw if ch.isdigit()) if raw else None


def _sa_id_already_registered(sa_id, exclude_user_id=None):
    if not sa_id:
        return False
    user_query = User.query.filter(User.sa_id_number == sa_id)
    patient_query = Patient.query.filter(Patient.id_number == sa_id)
    if exclude_user_id:
        user_query = user_query.filter(User.id != exclude_user_id)
        patient_query = patient_query.filter(or_(
            Patient.profile_id.is_(None),
            Patient.profile_id != exclude_user_id,
        ))
    return user_query.first() is not None or patient_query.first() is not None


def _staff_dob_from_form():
    raw = (request.form.get("date_of_birth") or "").strip()
    raw_sa_id = (request.form.get("sa_id_number") or "").strip()
    dob = None
    if raw:
        try:
            dob = date.fromisoformat(raw)
        except ValueError:
            return None, "Date of birth must be a valid date."
    if raw_sa_id:
        valid, id_error, dob_from_id = validate_sa_id(raw_sa_id)
        if not valid:
            return None, id_error or "Invalid South African ID number."
        if dob and dob_from_id and dob != dob_from_id:
            return None, "Date of birth does not match the SA ID number."
        dob = dob or dob_from_id
    return dob, None


def _technician_assignment_ids(technicians):
    selected_ids = request.form.getlist("technician_ids")
    if selected_ids:
        return selected_ids
    mode = request.form.get("technician_assignment") or "single"
    if mode == "all":
        return [tech.id for tech in technicians]
    technician_id = (request.form.get("technician_id") or "").strip()
    return [technician_id] if technician_id else []


def _apply_test_technicians(test, technicians):
    TechnicianTest.query.filter_by(test_id=test.id).delete()
    technician_ids = _technician_assignment_ids(technicians)
    valid_ids = {tech.id for tech in technicians}
    selected_ids = []
    seen_ids = set()
    for tech_id in technician_ids:
        if tech_id in valid_ids and tech_id not in seen_ids:
            selected_ids.append(tech_id)
            seen_ids.add(tech_id)
    for technician_id in selected_ids:
        db.session.add(TechnicianTest(
            technician_id=technician_id,
            test_id=test.id,
        ))
    test.assigned_technician = ", ".join(
        tech.full_name or tech.email
        for tech in technicians
        if tech.id in selected_ids
    ) or None
    return selected_ids

TECHNICIANS = [
    {
        "id": 1,

        "name": "John Smith",
        "email": "john@lablinks.com",
        "specialties": ["Chemistry"]
    },
    {
        "id": 2,
        "name": "Sarah Johnson",
        "email": "sarah@lablinks.com",
        "specialties": []
    },
    {
        "id": 3,
        "name": "David Williams",
        "email": "david@lablinks.com",
        "specialties": ["Microbiology"]
    }
]

TECHNICIAN_ASSIGNMENTS = {
    "tech_id_1": [
        "test_id_1",
        "test_id_2"
    ],
    "tech_id_2": [
        "test_id_3"
    ]
}

@bp.before_request
@login_required
@role_required("lab_manager", "admin")
def _gate():
    pass


@bp.route("/notifications")
def notifications():
    return render_user_notifications("manager.mark_all_read", "manager.clear_all_notifications")


@bp.route("/notifications/mark-all-read", methods=["POST"])
def mark_all_read():
    return mark_user_notifications_read("manager.notifications")


@bp.route("/notifications/clear-all", methods=["POST"])
def clear_all_notifications():
    return clear_user_notifications("manager.notifications")


@bp.route("/profile", methods=["GET", "POST"])
def profile():
    if not current_user.has_role("lab_manager"):
        abort(403)

    manager = current_user
    if request.method == "POST":
        full_name = (request.form.get("full_name") or "").strip()
        surname = (request.form.get("surname") or "").strip() or None
        title = (request.form.get("title") or "").strip() or None
        gender = (request.form.get("gender") or "").strip() or None
        email = (request.form.get("email") or "").lower().strip()
        phone = (request.form.get("phone") or "").strip() or None
        employee_number = (request.form.get("employee_number") or "").strip() or None
        raw_sa_id = (request.form.get("sa_id_number") or "").strip()
        sa_id = "".join(ch for ch in raw_sa_id if ch.isdigit()) if raw_sa_id else None
        dob, dob_error = _staff_dob_from_form()

        if not full_name:
            flash("First name is required.", "error")
            return redirect(url_for("manager.profile"))
        if not email:
            flash("Email is required.", "error")
            return redirect(url_for("manager.profile"))
        if dob_error:
            flash(dob_error, "error")
            return redirect(url_for("manager.profile"))
        if title and title not in TITLE_OPTIONS:
            flash("Please select a valid title.", "error")
            return redirect(url_for("manager.profile"))
        if gender and gender not in GENDER_OPTIONS:
            flash("Please select a valid gender.", "error")
            return redirect(url_for("manager.profile"))
        if User.query.filter(User.email == email, User.id != manager.id).first():
            flash("That email is already used by another account.", "error")
            return redirect(url_for("manager.profile"))
        if employee_number and User.query.filter(User.employee_number == employee_number, User.id != manager.id).first():
            flash("That employee number is already used by another account.", "error")
            return redirect(url_for("manager.profile"))
        if sa_id and User.query.filter(User.sa_id_number == sa_id, User.id != manager.id).first():
            flash("That SA ID number is already used by another account.", "error")
            return redirect(url_for("manager.profile"))

        avatar = request.files.get("avatar")
        if avatar and avatar.filename:
            ext = os.path.splitext(avatar.filename)[1].lower()
            if ext not in ALLOWED_AVATAR_EXT:
                flash("Profile picture must be PNG/JPG/GIF/WEBP.", "error")
                return redirect(url_for("manager.profile"))
            filename = secure_filename(f"{manager.id}{ext}")
            avatar.save(os.path.join(current_app.config["AVATAR_UPLOAD_DIR"], filename))
            manager.avatar_url = (
                url_for("static", filename=f"avatars/{filename}")
                + f"?v={uuid.uuid4().hex[:6]}"
            )

        manager.title = title
        manager.full_name = full_name
        manager.surname = surname
        manager.gender = gender
        manager.email = email
        manager.phone = phone
        manager.employee_number = employee_number
        manager.sa_id_number = sa_id
        manager.date_of_birth = dob

        log_audit(current_user.id, "update_manager_profile", "user", manager.id)
        db.session.commit()
        flash("Profile updated successfully.", "success")
        return redirect(url_for("manager.profile"))

    return render_template(
        "manager/profile.html",
        manager=manager,
        title_options=TITLE_OPTIONS,
        gender_options=GENDER_OPTIONS,
    )


def _user_role_label(user):
    labels = [ROLE_LABELS.get(role.role, role.role) for role in user.user_roles]
    return ", ".join(labels) or "No role"


def _deleted_actor_names(rows):
    actor_ids = {
        row["deleted_by"]
        for section in rows
        for row in section["rows"]
        if row.get("deleted_by")
    }
    if not actor_ids:
        return {}
    users = User.query.filter(User.id.in_(actor_ids)).all()
    return {
        user.id: user.full_name or user.email
        for user in users
    }


def _recycle_row(kind, record, name, detail=""):
    return {
        "kind": kind,
        "id": record.id,
        "name": name,
        "detail": detail,
        "deleted_at": record.deleted_at,
        "deleted_by": record.deleted_by,
    }


def _deleted_user_rows(for_manager=False):
    query = User.query.filter(User.deleted_at.isnot(None))
    if for_manager:
        query = (
            query.join(UserRole)
            .filter(UserRole.role.in_(("doctor", "lab_technician")))
            .distinct()
        )
    rows = []
    for user in query.order_by(User.deleted_at.desc()).all():
        rows.append(_recycle_row(
            "users",
            user,
            user.full_name or user.email,
            f"{user.email} - {_user_role_label(user)}",
        ))
    return rows


def _deleted_catalog_rows(kind, model, label_field="name"):
    return [
        _recycle_row(
            kind,
            row,
            getattr(row, label_field),
            getattr(row, "category", "") or getattr(row, "email", "") or "",
        )
        for row in model.query.filter(model.deleted_at.isnot(None)).order_by(model.deleted_at.desc()).all()
    ]


def _recycle_sections():
    is_admin = current_user.primary_role == "admin"
    sections = [
        {"title": "Tests", "rows": _deleted_catalog_rows("tests", TestCatalog, "name")},
        {"title": "Suppliers", "rows": _deleted_catalog_rows("suppliers", Supplier, "name")},
        {"title": "Consumables", "rows": _deleted_catalog_rows("consumables", Consumable, "name")},
        {
            "title": "Users" if is_admin else "Doctors and technicians",
            "rows": _deleted_user_rows(for_manager=not is_admin),
        },
    ]
    if is_admin:
        sections.extend([
            {"title": "Chronic conditions", "rows": _deleted_catalog_rows("conditions", Condition, "name")},
            {"title": "Allergies", "rows": _deleted_catalog_rows("allergies", Allergy, "name")},
            {"title": "Medications", "rows": _deleted_catalog_rows("medications", Medication, "name")},
        ])
    return sections


def _restore_allowed(kind, record):
    if current_user.primary_role == "admin":
        return True
    if kind in {"tests", "suppliers", "consumables"}:
        return True
    if kind == "users":
        roles = {role.role for role in record.user_roles}
        return bool(roles.intersection({"doctor", "lab_technician"}))
    return False


def _recycle_models():
    return {
        "users": User,
        "tests": TestCatalog,
        "suppliers": Supplier,
        "consumables": Consumable,
        "conditions": Condition,
        "allergies": Allergy,
        "medications": Medication,
    }


def _hard_delete_block_reason(kind, record):
    if kind == "tests":
        if TestRequestItem.query.filter_by(test_id=record.id).first():
            return "This test is linked to existing test requests and cannot be permanently deleted."
    elif kind == "suppliers":
        if Consumable.query.filter_by(supplier_id=record.id).first():
            return "This supplier is linked to consumables and cannot be permanently deleted."
        if ConsumableOrder.query.filter_by(supplier_id=record.id).first():
            return "This supplier is linked to orders and cannot be permanently deleted."
    elif kind == "consumables":
        if StockMovement.query.filter_by(consumable_id=record.id).first():
            return "This consumable has stock movement history and cannot be permanently deleted."
        if ConsumableOrder.query.filter_by(consumable_id=record.id).first():
            return "This consumable is linked to orders and cannot be permanently deleted."
        if ConsumableOrderItem.query.filter_by(consumable_id=record.id).first():
            return "This consumable is linked to order items and cannot be permanently deleted."
        if TestConsumable.query.filter_by(consumable_id=record.id).first():
            return "This consumable is linked to tests and cannot be permanently deleted."
    elif kind == "users":
        if TestRequest.query.filter_by(doctor_id=record.id).first():
            return "This user is linked to test requests and cannot be permanently deleted."
        if TestRequestItem.query.filter(or_(
            TestRequestItem.assigned_to == record.id,
            TestRequestItem.captured_by == record.id,
            TestRequestItem.verified_by == record.id,
        )).first():
            return "This user is linked to test activity and cannot be permanently deleted."
        if StockMovement.query.filter_by(created_by=record.id).first():
            return "This user is linked to stock movement history and cannot be permanently deleted."
        if record.patient_record and TestRequest.query.filter_by(patient_id=record.patient_record.id).first():
            return "This user's patient record is linked to test requests and cannot be permanently deleted."
    return None


def _prepare_hard_delete(kind, record):
    if kind == "tests":
        TestConsumable.query.filter_by(test_id=record.id).delete()
        TechnicianTest.query.filter_by(test_id=record.id).delete()
    elif kind == "users":
        TechnicianTest.query.filter_by(technician_id=record.id).delete()
        UserRole.query.filter_by(user_id=record.id).delete()
        if record.patient_record and not TestRequest.query.filter_by(patient_id=record.patient_record.id).first():
            db.session.delete(record.patient_record)
    elif kind in {"conditions", "allergies", "medications"}:
        record.patients.clear()


@bp.route("/recycle-bin")
def recycle_bin():
    sections = _recycle_sections()
    return render_template(
        "manager/recycle_bin.html",
        sections=sections,
        deleted_by_names=_deleted_actor_names(sections),
    )


@bp.route("/recycle-bin/<kind>/<record_id>/restore", methods=["POST"])
def recycle_restore(kind, record_id):
    models = _recycle_models()
    Model = models.get(kind)
    if not Model:
        abort(404)
    record = db.session.get(Model, record_id)
    if not record or not getattr(record, "deleted_at", None):
        abort(404)
    if not _restore_allowed(kind, record):
        abort(403)

    restore(record)
    if kind == "users" and getattr(record, "patient_record", None):
        restore(record.patient_record)
    log_audit(current_user.id, f"restore_{kind}", Model.__tablename__, record.id)
    db.session.commit()
    flash("Item restored.", "success")
    return redirect(url_for("manager.recycle_bin"))


@bp.route("/recycle-bin/<kind>/<record_id>/delete", methods=["POST"])
def recycle_hard_delete(kind, record_id):
    models = _recycle_models()
    Model = models.get(kind)
    if not Model:
        abort(404)
    record = db.session.get(Model, record_id)
    if not record or not getattr(record, "deleted_at", None):
        abort(404)
    if not _restore_allowed(kind, record):
        abort(403)

    reason = _hard_delete_block_reason(kind, record)
    if reason:
        flash(reason, "error")
        return redirect(url_for("manager.recycle_bin"))

    name = getattr(record, "name", None) or getattr(record, "full_name", None) or getattr(record, "email", None) or record.id
    try:
        _prepare_hard_delete(kind, record)
        log_audit(current_user.id, f"hard_delete_{kind}", Model.__tablename__, record.id, {"name": name})
        db.session.delete(record)
        db.session.commit()
        flash(f"{name} permanently deleted.", "success")
    except IntegrityError:
        db.session.rollback()
        flash("This item is linked to existing records and cannot be permanently deleted.", "error")
    return redirect(url_for("manager.recycle_bin"))


@bp.route("/")
def dashboard():

    statuses = [
        "submitted",
        "samples_received",
        "in_progress",
        "completed",
        "verified",
        "released"
    ]

    by_status = {s: 0 for s in statuses}
    by_status.update(dict(
        db.session.query(TestRequest.status, func.count(TestRequest.id))
        .filter(TestRequest.status.in_(statuses))
        .group_by(TestRequest.status)
        .all()
    ))
    status_chart_labels = [s.replace("_", " ").title() for s in statuses]
    status_chart_values = [by_status.get(s, 0) for s in statuses]

    # Low stock items
    low_stock = (
        Consumable.query
        .filter(Consumable.deleted_at.is_(None))
        .filter(Consumable.current_stock <= Consumable.reorder_level * 1.1)
        .order_by(Consumable.current_stock.asc())
        .all()
    )

    # Recent consumable orders
    recent_orders = (
        ConsumableOrder.query
        .order_by(ConsumableOrder.ordered_at.desc())
        .limit(5)
        .all()
    )
    order_statuses = ["ordered", "partially_complete", "complete", "cancelled"]
    by_order_status = {s: 0 for s in order_statuses}
    by_order_status.update(dict(
        db.session.query(ConsumableOrder.status, func.count(ConsumableOrder.id))
        .filter(ConsumableOrder.status.in_(order_statuses))
        .group_by(ConsumableOrder.status)
        .all()
    ))
    order_chart_labels = [s.replace("_", " ").title() for s in order_statuses]
    order_chart_values = [by_order_status.get(s, 0) for s in order_statuses]

    today = date.today()
    trend_days = [today - timedelta(days=13 - i) for i in range(14)]
    trend_counts = {day.isoformat(): 0 for day in trend_days}
    trend_start = datetime.combine(trend_days[0], datetime.min.time())
    for created_at, in (
        db.session.query(TestRequest.created_at)
        .filter(TestRequest.created_at >= trend_start)
        .all()
    ):
        if created_at:
            key = created_at.date().isoformat()
            if key in trend_counts:
                trend_counts[key] += 1

    stock_chart_rows = sorted(
        low_stock,
        key=lambda item: item.current_stock / max(item.reorder_level or 1, 1),
    )[:8]

    # Technician count
    technician_count = (
        User.query
        .join(UserRole)
        .filter(UserRole.role == "lab_technician", User.deleted_at.is_(None))
        .count()
    )

    # Supplier count
    supplier_count = Supplier.query.filter(Supplier.deleted_at.is_(None)).count()

    # Total orders
    total_orders = ConsumableOrder.query.count()

    # Doctor count
    doctor_count = (
    User.query
    .join(UserRole)
    .filter(UserRole.role == "doctor", User.deleted_at.is_(None))
    .count()
)

    return render_template(
        "manager/dashboard.html",

        by_status=by_status,
        low_stock=low_stock,
        recent_orders=recent_orders,
        status_chart_labels=status_chart_labels,
        status_chart_values=status_chart_values,
        order_chart_labels=order_chart_labels,
        order_chart_values=order_chart_values,
        trend_chart_labels=[day.strftime("%d %b") for day in trend_days],
        trend_chart_values=[trend_counts[day.isoformat()] for day in trend_days],
        stock_chart_labels=[item.name for item in stock_chart_rows],
        stock_chart_current=[item.current_stock for item in stock_chart_rows],
        stock_chart_reorder=[item.reorder_level for item in stock_chart_rows],

        technician_count=technician_count,
        supplier_count=supplier_count,
        total_orders=total_orders,
        doctor_count=doctor_count,
    )

@bp.route("/orders/new")
def new_order():

    consumable_id = request.args.get("consumable_id")

    consumables = _live(Consumable).order_by(
        Consumable.name
    ).all()

    suppliers = _live(Supplier).order_by(
        Supplier.name
    ).all()

    return render_template(
        "manager/new_order.html",
        consumables=consumables,
        suppliers=suppliers,
        selected_consumable=consumable_id,
    )


@bp.route("/catalog", methods=["GET", "POST"])
def catalog():

    consumables = _live(Consumable).order_by(
        Consumable.name
    ).all()

    technicians = (
        User.query
        .join(UserRole)
        .filter(UserRole.role == "lab_technician", User.deleted_at.is_(None))
        .all()
    )

    if request.method == "POST":
        sample_type, sample_type_error = _posted_sample_type()
        if sample_type_error:
            flash(sample_type_error, "error")
            return redirect(url_for("manager.catalog"))

        test = TestCatalog(
            code=request.form.get("code"),
            name=request.form.get("name"),
            category=request.form.get("category"),

            sample_type=sample_type,

            units=request.form.get("units"),

            reference_low=request.form.get("reference_low") or None,

            reference_high=request.form.get("reference_high") or None,

            turnaround_hours=int(
                request.form.get("turnaround_minutes") or 60
            ),

            assigned_technician=request.form.get(
                "assigned_technician"
            ),

            active=True
        )

        db.session.add(test)
        db.session.flush()

        selected_consumables = request.form.getlist(
            "consumables"
        )

        for consumable_id in selected_consumables:

            db.session.add(
                TestConsumable(
                    test_id=test.id,
                    consumable_id=consumable_id,
                    quantity_required=1
                )
            )

        db.session.commit()

        flash(
            "Test added successfully.",
            "success"
        )

        return redirect(
            url_for("manager.catalog")
        )

    tests = (
        _live(TestCatalog)
        .order_by(TestCatalog.name)
        .all()
    )

    return render_template(
        "manager/catalog.html",
        tests=tests,
        consumables=consumables,
        technicians=technicians
    )

@bp.route("/catalog/new", methods=["GET", "POST"])
def add_test():

    consumables = (
        _live(Consumable)
        .order_by(Consumable.name)
        .all()
    )

    technicians = (
        User.query
        .join(UserRole)
        .filter(UserRole.role == "lab_technician", User.deleted_at.is_(None))
        .order_by(User.full_name)
        .all()
    )

    if request.method == "POST":
        code = (request.form.get("code") or "").strip().upper()
        name = (request.form.get("name") or "").strip()
        sample_type, sample_type_error = _posted_sample_type()
        if sample_type_error:
            flash(sample_type_error, "error")
            return redirect(url_for("manager.add_test"))
        if TestCatalog.query.filter(
            (func.lower(TestCatalog.name) == name.lower()) | (TestCatalog.code == code)
        ).first():
            flash("Test code and test name must be unique.", "error")
            return redirect(url_for("manager.add_test"))
        test = TestCatalog(
            code=code,
            name=name,
            category=request.form.get("category"),
            sample_type=sample_type,
            units=request.form.get("units"),
            reference_low=request.form.get("reference_low") or None,
            reference_high=request.form.get("reference_high") or None,
            turnaround_hours=int(
                request.form.get("turnaround_minutes") or 60
            ),
            active=True
        )

        db.session.add(test)
        db.session.flush()

        consumable_ids = request.form.getlist(
            "consumable_id[]"
        )

        for consumable_id in consumable_ids:
            db.session.add(
                TestConsumable(
                    test_id=test.id,
                    consumable_id=consumable_id,
                    quantity_required=int(request.form.get(f"quantity_{consumable_id}") or 1),
                )
            )
        _apply_test_technicians(test, technicians)

        db.session.commit()

        flash(
            "Test added successfully.",
            "success"
        )

        return redirect(
            url_for("manager.catalog")
        )

    return render_template(
    "manager/add_test.html",
    consumables=consumables,
    technicians=technicians,
    categories=_category_names(),
    sample_types=_sample_type_names()
)

@bp.route("/catalog/<test_id>")
def test_detail(test_id):

    test = _live_or_404(TestCatalog, test_id)

    return render_template(
        "manager/test_detail.html",
        test=test
    )
@bp.route("/catalog/<test_id>/view")
def view_test(test_id):

    test = _live_or_404(TestCatalog, test_id)

    return render_template(
        "manager/test_detail.html",
        test=test
    )

@bp.route("/catalog/<test_id>/edit", methods=["GET", "POST"])
def edit_test(test_id):

    test = _live_or_404(TestCatalog, test_id)

    consumables = (
        _live(Consumable)
        .order_by(Consumable.name)
        .all()
    )

    technicians = (
        User.query
        .join(UserRole)
        .filter(UserRole.role == "lab_technician", User.deleted_at.is_(None))
        .order_by(User.full_name)
        .all()
    )

    if request.method == "POST":
        code = (request.form.get("code") or "").strip().upper()
        name = (request.form.get("name") or "").strip()
        sample_type, sample_type_error = _posted_sample_type(extra_allowed=[test.sample_type])
        if sample_type_error:
            flash(sample_type_error, "error")
            return redirect(url_for("manager.edit_test", test_id=test.id))
        duplicate = TestCatalog.query.filter(
            TestCatalog.id != test.id,
            ((func.lower(TestCatalog.name) == name.lower()) | (TestCatalog.code == code)),
        ).first()
        if duplicate:
            flash("Test code and test name must be unique.", "error")
            return redirect(url_for("manager.edit_test", test_id=test.id))
        test.code = code
        test.name = name
        test.category = request.form.get("category")

        test.sample_type = sample_type

        test.units = request.form.get("units")

        test.reference_low = (
            request.form.get("reference_low") or None
        )

        test.reference_high = (
            request.form.get("reference_high") or None
        )

        test.turnaround_hours = int(
            request.form.get("turnaround_minutes") or 60
        )

        # Remove existing consumables
        TestConsumable.query.filter_by(
            test_id=test.id
        ).delete()

        consumable_ids = request.form.getlist(
            "consumable_id[]"
        )

        for cid in consumable_ids:
            if cid:
                db.session.add(
                    TestConsumable(
                        test_id=test.id,
                        consumable_id=cid,
                        quantity_required=int(request.form.get(f"quantity_{cid}") or 1),
                    )
                )
        _apply_test_technicians(test, technicians)

        db.session.commit()

        flash(
            "Test updated successfully.",
            "success"
        )

        return redirect(
            url_for(
                "manager.view_test",
                test_id=test.id
            )
        )

    test_consumables = (
        TestConsumable.query
        .filter_by(test_id=test.id)
        .all()
    )

    assigned_ids = [
        tc.consumable_id
        for tc in test_consumables
    ]
    assigned_technician_ids = [
        assignment.technician_id
        for assignment in TechnicianTest.query.filter_by(test_id=test.id).all()
    ]

    return render_template(
        "manager/edit_test.html",
        test=test,
        consumables=consumables,
        technicians=technicians,
        test_consumables=test_consumables,
        assigned_ids=assigned_ids,
        assigned_technician_ids=assigned_technician_ids,
        categories=_category_names(),
        sample_types=_sample_type_choices(test.sample_type)
    )


@bp.route("/reference-data", methods=["GET", "POST"])
def reference_data():
    _ensure_reference_data()
    if request.method == "POST":
        kind = request.form.get("kind")
        name = (request.form.get("name") or "").strip()
        description = (request.form.get("description") or "").strip() or None
        Model = {"category": TestCategory, "sample_type": SampleType}.get(kind)
        if not Model or not name:
            flash("Select a valid list and enter a name.", "error")
        elif Model.query.filter(func.lower(Model.name) == name.lower()).first():
            flash("That name already exists.", "error")
        else:
            row = Model(name=name, description=description, active=True)
            db.session.add(row)
            log_audit(current_user.id, f"create_{kind}", Model.__tablename__, name)
            db.session.commit()
            flash("Reference item added.", "success")
        return redirect(url_for("manager.reference_data"))
    return render_template(
        "manager/reference_data.html",
        categories=TestCategory.query.order_by(TestCategory.name).all(),
        sample_types=SampleType.query.order_by(SampleType.name).all(),
    )


@bp.route("/reference-data/<kind>/<row_id>/toggle", methods=["POST"])
def reference_toggle(kind, row_id):
    Model = {"category": TestCategory, "sample_type": SampleType}.get(kind)
    if not Model:
        abort(404)
    row = db.session.get(Model, row_id)
    if not row:
        abort(404)
    row.active = not row.active
    log_audit(current_user.id, f"toggle_{kind}", Model.__tablename__, row.id,
              {"active": row.active})
    db.session.commit()
    flash("Reference item updated.", "success")
    return redirect(url_for("manager.reference_data"))

@bp.route(
    "/catalog/<test_id>/delete",
    methods=["POST"]
)
def delete_test(test_id):

    test = _live_or_404(TestCatalog, test_id)
    soft_delete(test, current_user.id)
    log_audit(current_user.id, "soft_delete_test", "test_catalog", test.id)

    db.session.commit()

    flash(
        "Test moved to recycle bin.",
        "success"
    )

    return redirect(
        url_for("manager.catalog")
    )

@bp.route("/inventory")
def inventory():

    items = _live(Consumable).order_by(
        Consumable.name
    ).all()

    return render_template(
        "manager/inventory.html",
        items=items
    )

@bp.route("/inventory/new", methods=["GET", "POST"])
def add_consumable():

    if request.method == "POST":

        consumable = Consumable(
            sku=request.form.get("sku"),
            name=request.form.get("name"),
            category=request.form.get("category"),
            unit=request.form.get("unit"),
            supplier_id=request.form.get("supplier_id") or None,
            current_stock=int(request.form.get("current_stock") or 0),
            reorder_level=int(request.form.get("reorder_level") or 0),
        )

        db.session.add(consumable)
        db.session.commit()

        flash("Consumable added successfully.", "success")

        return redirect(url_for("manager.inventory"))

    return render_template(
        "manager/add_consumable.html",
        suppliers=_live(Supplier).order_by(Supplier.name).all(),
    )

@bp.route("/inventory/<consumable_id>")
def view_consumable(consumable_id):

    consumable = _live_or_404(Consumable, consumable_id)

    return render_template(
        "manager/view_consumable.html",
        consumable=consumable
    )


@bp.route("/inventory/<consumable_id>/adjust", methods=["POST"])
def adjust_consumable(consumable_id):
    consumable = _live_or_404(Consumable, consumable_id)
    operation = request.form.get("operation")
    try:
        quantity = int(request.form.get("quantity") or 0)
    except ValueError:
        quantity = -1
    if operation not in ("increase", "decrease", "set") or quantity < 0:
        flash("Enter a valid non-negative quantity and adjustment type.", "error")
        return redirect(url_for("manager.view_consumable", consumable_id=consumable.id))

    old_value = consumable.current_stock
    if operation == "increase":
        consumable.current_stock += quantity
        movement_type = "in"
        movement_quantity = quantity
    elif operation == "decrease":
        if quantity > consumable.current_stock:
            flash("Stock cannot be decreased below zero.", "error")
            return redirect(url_for("manager.view_consumable", consumable_id=consumable.id))
        consumable.current_stock -= quantity
        movement_type = "out"
        movement_quantity = quantity
    else:
        consumable.current_stock = quantity
        movement_type = "adjustment"
        movement_quantity = quantity

    db.session.add(StockMovement(
        consumable_id=consumable.id,
        movement_type=movement_type,
        quantity=movement_quantity,
        notes=f"{operation.title()} adjustment: {old_value} to {consumable.current_stock}",
        created_by=current_user.id,
    ))
    log_audit(
        current_user.id,
        "adjust_stock",
        "consumable",
        consumable.id,
        {"operation": operation, "old": old_value, "new": consumable.current_stock},
    )
    db.session.commit()
    flash("Stock level adjusted.", "success")
    return redirect(url_for("manager.view_consumable", consumable_id=consumable.id))


@bp.route(
    "/inventory/<consumable_id>/edit",
    methods=["GET", "POST"]
)
def edit_consumable(consumable_id):

    consumable = _live_or_404(Consumable, consumable_id)

    if request.method == "POST":

        consumable.sku = request.form.get("sku")
        consumable.name = request.form.get("name")
        consumable.category = request.form.get("category")
        consumable.unit = request.form.get("unit")
        consumable.supplier_id = request.form.get("supplier_id") or None
        consumable.current_stock = int(
            request.form.get("current_stock") or 0
        )
        consumable.reorder_level = int(
            request.form.get("reorder_level") or 0
        )

        db.session.commit()

        flash(
            "Consumable updated successfully.",
            "success"
        )

        return redirect(
            url_for("manager.inventory")
        )

    return render_template(
        "manager/edit_consumable.html",
        consumable=consumable,
        suppliers=_live(Supplier).order_by(Supplier.name).all(),
    )

@bp.route(
    "/inventory/<consumable_id>/delete",
    methods=["POST"]
)
def delete_consumable(consumable_id):

    consumable = _live_or_404(Consumable, consumable_id)
    soft_delete(consumable, current_user.id)
    log_audit(current_user.id, "soft_delete_consumable", "consumable", consumable.id)

    db.session.commit()

    flash(
        "Consumable moved to recycle bin.",
        "success"
    )

    return redirect(
        url_for("manager.inventory")
    )

@bp.route("/suppliers", methods=["GET", "POST"])
def suppliers():
    if request.method == "POST":
        s = Supplier(
            name=request.form.get("name", "").strip(),
            contact_name=request.form.get("contact_name"),
            email=request.form.get("email"),
            phone=request.form.get("phone"),
            address=request.form.get("address"),
        )
        db.session.add(s)
        log_audit(current_user.id, "add_supplier", "supplier", s.id)
        db.session.commit()
        flash("Supplier added.", "success")
        return redirect(url_for("manager.suppliers"))
    rows = _live(Supplier).order_by(Supplier.name).all()
    return render_template("manager/suppliers.html", rows=rows)

@bp.route("/suppliers/new", methods=["GET", "POST"])
def add_supplier():

    if request.method == "POST":

        supplier = Supplier(
            name=request.form.get("name"),
            contact_name=request.form.get("contact_name"),
            email=request.form.get("email"),
            phone=request.form.get("phone"),
            address=request.form.get("address"),
        )

        db.session.add(supplier)
        db.session.commit()

        flash("Supplier added successfully.", "success")

        return redirect(url_for("manager.suppliers"))

    return render_template(
        "manager/add_supplier.html"
    )


@bp.route("/suppliers/<supplier_id>")
def supplier_detail(supplier_id):

    supplier = _live_or_404(Supplier, supplier_id)

    return render_template(
        "manager/supplier_detail.html",
        supplier=supplier
    )


@bp.route(
    "/suppliers/<supplier_id>/edit",
    methods=["GET", "POST"]
)
def edit_supplier(supplier_id):

    supplier = _live_or_404(Supplier, supplier_id)

    if request.method == "POST":

        supplier.name = request.form.get("name")
        supplier.contact_name = request.form.get("contact_name")
        supplier.email = request.form.get("email")
        supplier.phone = request.form.get("phone")
        supplier.address = request.form.get("address")

        db.session.commit()

        flash(
            "Supplier updated successfully.",
            "success"
        )

        return redirect(
            url_for("manager.suppliers")
        )

    return render_template(
        "manager/edit_supplier.html",
        supplier=supplier
    )


@bp.route(
    "/suppliers/<supplier_id>/delete",
    methods=["POST"]
)
def delete_supplier(supplier_id):

    supplier = _live_or_404(Supplier, supplier_id)
    soft_delete(supplier, current_user.id)
    log_audit(current_user.id, "soft_delete_supplier", "supplier", supplier.id)
    db.session.commit()

    flash(
        "Supplier moved to recycle bin.",
        "success"
    )

    return redirect(
        url_for("manager.suppliers")
    )



@bp.route("/technicians")
def technicians():

    techs = (
        User.query
        .join(UserRole)
        .filter(UserRole.role == "lab_technician", User.deleted_at.is_(None))
        .all()
    )

    assignment_counts = {}

    for tech in techs:
        assignment_counts[tech.id] = (
            TechnicianTest.query
            .filter_by(technician_id=tech.id)
            .count()
        )

    return render_template(
        "manager/technicians.html",
        technicians=techs,
        assignment_counts=assignment_counts
    )

@bp.route(
    "/technicians/<tech_id>",
    methods=["GET", "POST"]
)
def technician_detail(tech_id):

    technician = _live_or_404(User, tech_id)

    tests = _live(TestCatalog).filter_by(active=True).order_by(
        TestCatalog.name
    ).all()

    if request.method == "POST":

        selected_tests = request.form.getlist(
            "tests"
        )
        if not selected_tests:
            flash("Assign at least one test type to the technician.", "error")
            return redirect(url_for("manager.technician_detail", tech_id=tech_id))

        # Remove old technician assignments
        TechnicianTest.query.filter_by(
            technician_id=tech_id
        ).delete()

        # Clear existing catalog assignments
        _live(TestCatalog).filter_by(
            assigned_technician=technician.full_name
        ).update(
            {"assigned_technician": None}
        )

        # Add new assignments
        for test_id in selected_tests:

            db.session.add(
                TechnicianTest(
                    technician_id=tech_id,
                    test_id=test_id
                )
            )

            test = _live(TestCatalog).filter_by(id=test_id).first()

            if test:
                test.assigned_technician = (
                    technician.full_name
                )

        db.session.commit()

        flash(
            "Assignments updated.",
            "success"
        )

        return redirect(
            url_for(
                "manager.technician_detail",
                tech_id=tech_id
            )
        )

    assigned = TechnicianTest.query.filter_by(
        technician_id=tech_id
    ).all()

    assigned_ids = [
        a.test_id
        for a in assigned
    ]

    return render_template(
        "manager/technician_detail.html",
        technician=technician,
        tests=tests,
        assigned_ids=assigned_ids
    )

@bp.route("/requests")
def requests_list():
    rows = TestRequest.query.order_by(
        TestRequest.created_at.desc()
    ).all()

    return render_template(
        "manager/requests.html",
        rows=rows
    )

@bp.route("/quick-order", methods=["POST"])
def quick_order():
    consumable_id = request.form.get("consumable_id")
    try:
        quantity = int(request.form.get("quantity") or 0)
    except ValueError:
        quantity = 0
    consumable = db.session.get(Consumable, consumable_id)
    if consumable and consumable.deleted_at:
        consumable = None
    if not consumable or quantity <= 0:
        flash("Select a valid consumable and quantity.", "error")
        return redirect(url_for("manager.dashboard"))
    supplier = consumable.supplier
    if not supplier or supplier.deleted_at:
        flash("Link this consumable to a supplier before ordering.", "error")
        return redirect(url_for("manager.dashboard"))
    order = ConsumableOrder(
        order_number=f"ORD-{datetime.now().strftime('%Y%m%d%H%M%S%f')}",
        supplier_id=supplier.id,
        consumable_id=consumable.id,
        quantity=quantity,
        status="ordered",
    )
    db.session.add(order)
    db.session.flush()
    db.session.add(ConsumableOrderItem(
        order_id=order.id,
        consumable_id=consumable.id,
        quantity=quantity,
        status="ordered",
    ))
    log_audit(
        current_user.id,
        "quick_order",
        "consumable_order",
        order.id,
    )
    db.session.commit()
    sent = send_email(
        [supplier.email],
        f"MediLab Connect consumable order {order.order_number}",
        (
            f"Hello {supplier.contact_name or supplier.name},\n\n"
            f"Please process the following consumable order for MediLab Connect.\n\n"
            f"Order number: {order.order_number}\n"
            f"Item: {consumable.name}\n"
            f"Quantity: {quantity}\n\n"
            "Please contact the laboratory if you need any clarification before dispatch.\n\n"
            "- MediLab Connect"
        ),
    )
    flash(
        f"Order placed for {consumable.name} and e-mailed to the supplier."
        if sent else
        f"Order placed for {consumable.name}; supplier e-mail could not be sent.",
        "success" if sent else "error",
    )
    return redirect(url_for("manager.dashboard"))

@bp.route("/doctors/new", methods=["GET", "POST"])
def add_doctor():

    if request.method == "POST":
        full_name = (request.form.get("full_name") or "").strip()
        surname = (request.form.get("surname") or "").strip()
        email = (request.form.get("email") or "").lower().strip()
        phone = (request.form.get("phone") or "").strip()
        gender = (request.form.get("gender") or "").strip() or None
        employee_number = (request.form.get("employee_number") or "").strip()
        sa_id_number = _normalise_sa_id(request.form.get("sa_id_number"))
        hpcsa_number = (request.form.get("hpcsa_number") or "").strip()
        dob, dob_error = _staff_dob_from_form()
        if not full_name or not surname:
            flash("First name and surname are required.", "error")
            return redirect(url_for("manager.add_doctor"))
        if not email:
            flash("Email is required.", "error")
            return redirect(url_for("manager.add_doctor"))
        if dob_error:
            flash(dob_error, "error")
            return redirect(url_for("manager.add_doctor"))
        if gender and gender not in GENDER_OPTIONS:
            flash("Please select a valid gender.", "error")
            return redirect(url_for("manager.add_doctor"))
        if not phone:
            flash("Phone number is required for the WhatsApp welcome message.", "error")
            return redirect(url_for("manager.add_doctor"))
        if not employee_number:
            flash("Employee number is required.", "error")
            return redirect(url_for("manager.add_doctor"))
        if not hpcsa_number:
            flash("HPCSA number is required.", "error")
            return redirect(url_for("manager.add_doctor"))
        if User.query.filter_by(email=email).first():
            flash("Doctor already exists.", "error")
            return redirect(url_for("manager.add_doctor"))
        if User.query.filter_by(employee_number=employee_number).first():
            flash("Employee number already exists.", "error")
            return redirect(url_for("manager.add_doctor"))
        if _sa_id_already_registered(sa_id_number):
            flash("That SA ID number is already registered.", "error")
            return redirect(url_for("manager.add_doctor"))
        if User.query.filter_by(hpcsa_number=hpcsa_number).first():
            flash("HPCSA number already exists.", "error")
            return redirect(url_for("manager.add_doctor"))

        doctor = User(
            full_name=full_name,
            surname=surname,
            email=email,
            phone=phone,
            gender=gender,
            date_of_birth=dob,
            employee_number=employee_number,
            sa_id_number=sa_id_number,
            hpcsa_number=hpcsa_number,
            must_change_password=True,
        )
        temporary_password = secrets.token_urlsafe(10) + "A1!"
        doctor.set_password(temporary_password)
        doctor.temp_password = temporary_password

        db.session.add(doctor)
        db.session.flush()
        db.session.add(UserRole(user_id=doctor.id, role="doctor"))
        log_audit(current_user.id, "create_doctor", "user", doctor.id)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash("An account with that email, employee number, SA ID number, or HPCSA number already exists.", "error")
            return redirect(url_for("manager.add_doctor"))
        sent = send_email(
            [doctor.email],
            "Your MediLab Connect doctor account",
            (
                f"Hello Dr. {doctor.full_name or doctor.email},\n\n"
                "Your MediLab Connect doctor account has been created.\n\n"
                f"Temporary password: {temporary_password}\n\n"
                "For security, you will be asked to choose a new password the first time you sign in.\n\n"
                "- MediLab Connect"
            ),
        )
        whatsapp_sent = send_account_welcome_whatsapp(
            doctor,
            role="doctor",
            temporary_password=temporary_password,
        )
        flash_message = (
            "Doctor created and temporary password e-mailed."
            if sent else
            f"Doctor created. SMTP is unavailable; temporary password: {temporary_password}"
        )
        if whatsapp_sent:
            flash_message += " WhatsApp welcome sent."
        flash(
            flash_message,
            "success" if sent else "error",
        )
        return redirect(url_for("manager.doctors"))

    return render_template(
        "manager/add_doctor.html",
        gender_options=GENDER_OPTIONS,
    )

@bp.route("/doctors")
def doctors():

    doctors = (
        User.query
        .join(UserRole)
        .filter(UserRole.role == "doctor", User.deleted_at.is_(None))
        .all()
    )

    return render_template(
        "manager/doctors.html",
        doctors=doctors
    )

@bp.route("/doctors/<doctor_id>")
def doctor_detail(doctor_id):

    doctor = _live_or_404(User, doctor_id)

    return render_template(
        "manager/doctor_detail.html",
        doctor=doctor
    )

@bp.route(
    "/doctors/<doctor_id>/edit",
    methods=["GET", "POST"]
)
def edit_doctor(doctor_id):

    doctor = _live_or_404(User, doctor_id)

    if request.method == "POST":
        dob, dob_error = _staff_dob_from_form()
        gender = (request.form.get("gender") or "").strip() or None
        if dob_error:
            flash(dob_error, "error")
            return redirect(url_for("manager.edit_doctor", doctor_id=doctor_id))
        if gender and gender not in GENDER_OPTIONS:
            flash("Please select a valid gender.", "error")
            return redirect(url_for("manager.edit_doctor", doctor_id=doctor_id))

        doctor.full_name = request.form.get("full_name")
        doctor.surname = request.form.get("surname")
        doctor.email = request.form.get("email")
        doctor.phone = request.form.get("phone")
        doctor.gender = gender
        doctor.date_of_birth = dob
        doctor.employee_number = request.form.get("employee_number")
        doctor.sa_id_number = request.form.get("sa_id_number")
        doctor.hpcsa_number = request.form.get("hpcsa_number")

        db.session.commit()

        flash(
            "Doctor updated successfully.",
            "success"
        )

        return redirect(
            url_for("manager.doctors")
        )

    return render_template(
        "manager/edit_doctor.html",
        doctor=doctor,
        gender_options=GENDER_OPTIONS,
    )

@bp.route(
    "/doctors/<doctor_id>/delete",
    methods=["POST"]
)
def delete_doctor(doctor_id):

    doctor = _live_or_404(User, doctor_id)
    soft_delete(doctor, current_user.id)
    log_audit(current_user.id, "soft_delete_doctor", "user", doctor.id)

    db.session.commit()

    flash(
        "Doctor moved to recycle bin.",
        "success"
    )

    return redirect(
        url_for("manager.doctors")
    )


@bp.route("/technicians/new", methods=["GET", "POST"])
def add_technician():
    tests = _live(TestCatalog).filter_by(active=True).order_by(TestCatalog.name).all()
    if request.method == "POST":
        full_name = (request.form.get("full_name") or "").strip()
        surname = (request.form.get("surname") or "").strip()
        email = (request.form.get("email") or "").lower().strip()
        phone = (request.form.get("phone") or "").strip()
        title = (request.form.get("title") or "").strip() or None
        gender = (request.form.get("gender") or "").strip() or None
        employee_number = (request.form.get("employee_number") or "").strip()
        sa_id_number = _normalise_sa_id(request.form.get("sa_id_number"))
        selected_tests = request.form.getlist("tests")
        dob, dob_error = _staff_dob_from_form()
        if not selected_tests:
            flash("Assign at least one test type to the technician.", "error")
            return redirect(url_for("manager.add_technician"))
        if not full_name or not surname:
            flash("First name and surname are required.", "error")
            return redirect(url_for("manager.add_technician"))
        if not email:
            flash("Email is required.", "error")
            return redirect(url_for("manager.add_technician"))
        if dob_error:
            flash(dob_error, "error")
            return redirect(url_for("manager.add_technician"))
        if title and title not in TITLE_OPTIONS:
            flash("Please select a valid title.", "error")
            return redirect(url_for("manager.add_technician"))
        if gender and gender not in GENDER_OPTIONS:
            flash("Please select a valid gender.", "error")
            return redirect(url_for("manager.add_technician"))
        if not phone:
            flash("Phone number is required for the WhatsApp welcome message.", "error")
            return redirect(url_for("manager.add_technician"))
        if not employee_number:
            flash("Employee number is required.", "error")
            return redirect(url_for("manager.add_technician"))
        if User.query.filter_by(email=email).first():
            flash("User already exists.", "error")
            return redirect(url_for("manager.add_technician"))
        if User.query.filter_by(employee_number=employee_number).first():
            flash("Employee number already exists.", "error")
            return redirect(url_for("manager.add_technician"))
        if _sa_id_already_registered(sa_id_number):
            flash("That SA ID number is already registered.", "error")
            return redirect(url_for("manager.add_technician"))

        technician = User(
            title=title,
            full_name=full_name,
            surname=surname,
            email=email,
            phone=phone,
            gender=gender,
            date_of_birth=dob,
            employee_number=employee_number,
            sa_id_number=sa_id_number,
            must_change_password=True,
        )
        temporary_password = secrets.token_urlsafe(10) + "A1!"
        technician.set_password(temporary_password)
        technician.temp_password = temporary_password
        db.session.add(technician)
        db.session.flush()
        db.session.add(UserRole(user_id=technician.id, role="lab_technician"))
        for test_id in selected_tests:
            if db.session.get(TestCatalog, test_id):
                db.session.add(TechnicianTest(
                    technician_id=technician.id,
                    test_id=test_id,
                ))
        log_audit(current_user.id, "create_technician", "user", technician.id)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash("An account with that email, employee number, or SA ID number already exists.", "error")
            return redirect(url_for("manager.add_technician"))
        sent = send_email(
            [technician.email],
            "Your MediLab Connect technician account",
            (
                f"Hello {technician.full_name or technician.email},\n\n"
                "Your MediLab Connect laboratory technician account has been created.\n\n"
                f"Temporary password: {temporary_password}\n\n"
                "For security, you will be asked to choose a new password the first time you sign in.\n\n"
                "- MediLab Connect"
            ),
        )
        whatsapp_sent = send_account_welcome_whatsapp(
            technician,
            role="lab_technician",
            temporary_password=temporary_password,
        )
        flash_message = (
            "Technician created and temporary password e-mailed."
            if sent else
            f"Technician created. SMTP is unavailable; temporary password: {temporary_password}"
        )
        if whatsapp_sent:
            flash_message += " WhatsApp welcome sent."
        flash(
            flash_message,
            "success" if sent else "error",
        )
        return redirect(url_for("manager.technicians"))

    return render_template(
        "manager/add_technician.html",
        tests=tests,
        title_options=TITLE_OPTIONS,
        gender_options=GENDER_OPTIONS,
    )

@bp.route(
    "/technicians/<tech_id>/edit",
    methods=["GET", "POST"]
)
def edit_technician(tech_id):

    technician = _live_or_404(User, tech_id)

    tests = _live(TestCatalog).filter_by(active=True).order_by(
        TestCatalog.name
    ).all()

    if request.method == "POST":
        selected_tests = request.form.getlist("tests")
        title = (request.form.get("title") or "").strip() or None
        gender = (request.form.get("gender") or "").strip() or None
        dob, dob_error = _staff_dob_from_form()
        if not selected_tests:
            flash("Assign at least one test type to the technician.", "error")
            return redirect(url_for("manager.edit_technician", tech_id=tech_id))
        if dob_error:
            flash(dob_error, "error")
            return redirect(url_for("manager.edit_technician", tech_id=tech_id))
        if title and title not in TITLE_OPTIONS:
            flash("Please select a valid title.", "error")
            return redirect(url_for("manager.edit_technician", tech_id=tech_id))
        if gender and gender not in GENDER_OPTIONS:
            flash("Please select a valid gender.", "error")
            return redirect(url_for("manager.edit_technician", tech_id=tech_id))

        # Update technician details
        technician.title = title

        technician.full_name = request.form.get(
            "full_name"
        )

        technician.surname = request.form.get(
            "surname"
        )

        technician.email = request.form.get(
            "email"
        )

        technician.phone = request.form.get(
            "phone"
        )

        technician.gender = gender

        technician.date_of_birth = dob

        technician.employee_number = request.form.get(
            "employee_number"
        )

        technician.sa_id_number = request.form.get(
            "sa_id_number"
        )

        technician.hpcsa_number = request.form.get(
            "hpcsa_number"
        )

        # Remove old assignments
        TechnicianTest.query.filter_by(
            technician_id=tech_id
        ).delete()

        # Remove technician from all tests currently assigned
        assigned_tests = TestCatalog.query.filter_by(
            assigned_technician=technician.full_name
        ).all()

        for test in assigned_tests:
            test.assigned_technician = None

        # Add new assignments
        for test_id in selected_tests:

            db.session.add(
                TechnicianTest(
                    technician_id=tech_id,
                    test_id=test_id
                )
            )

            test = _live(TestCatalog).filter_by(id=test_id).first()

            if test:
                test.assigned_technician = (
                    technician.full_name
                )

        db.session.commit()

        flash(
            "Technician updated successfully.",
            "success"
        )

        return redirect(
            url_for(
                "manager.technicians"
            )
        )

    assigned_ids = [
        assignment.test_id
        for assignment in TechnicianTest.query.filter_by(
            technician_id=tech_id
        ).all()
    ]

    return render_template(
        "manager/edit_technician.html",
        technician=technician,
        tests=tests,
        assigned_ids=assigned_ids,
        title_options=TITLE_OPTIONS,
        gender_options=GENDER_OPTIONS,
    )

@bp.route("/technicians/<tech_id>/delete",
          methods=["POST"])
def delete_technician(tech_id):

    technician = _live_or_404(User, tech_id)

    tests = TestCatalog.query.filter_by(
        assigned_technician=technician.full_name
    ).all()

    for test in tests:
        test.assigned_technician = None

    soft_delete(technician, current_user.id)
    log_audit(current_user.id, "soft_delete_technician", "user", technician.id)

    db.session.commit()

    flash(
        "Technician moved to recycle bin.",
        "success"
    )

    return redirect(
        url_for("manager.technicians")
    )

@bp.route("/orders", methods=["GET", "POST"])
def orders():
    if request.method == "POST":
        supplier = db.session.get(Supplier, request.form.get("supplier_id"))
        if not supplier or supplier.deleted_at:
            flash("Select a valid supplier.", "error")
            return redirect(url_for("manager.orders"))
        order_number = (request.form.get("order_number") or "").strip()
        if not order_number:
            order_number = "ORD-" + datetime.now().strftime("%Y%m%d%H%M%S%f")
        if ConsumableOrder.query.filter_by(order_number=order_number).first():
            flash("Order number already exists.", "error")
            return redirect(url_for("manager.orders"))

        selected = []
        for consumable_id, quantity_text in zip(
            request.form.getlist("consumable_id"),
            request.form.getlist("quantity"),
        ):
            try:
                quantity = int(quantity_text or 0)
            except ValueError:
                quantity = 0
            consumable = db.session.get(Consumable, consumable_id)
            if (
                quantity > 0
                and consumable
                and not consumable.deleted_at
                and consumable.supplier_id == supplier.id
            ):
                selected.append((consumable, quantity))
        if not selected:
            flash("Add at least one consumable supplied by the selected supplier.", "error")
            return redirect(url_for("manager.orders"))

        order = ConsumableOrder(
            order_number=order_number,
            supplier_id=supplier.id,
            consumable_id=selected[0][0].id,
            quantity=sum(quantity for _, quantity in selected),
            status="ordered",
        )
        db.session.add(order)
        db.session.flush()
        for consumable, quantity in selected:
            db.session.add(ConsumableOrderItem(
                order_id=order.id,
                consumable_id=consumable.id,
                quantity=quantity,
                status="ordered",
            ))
        log_audit(
            current_user.id,
            "create_order",
            "consumable_order",
            order.id,
            {"items": [{"consumable_id": c.id, "quantity": q} for c, q in selected]},
        )
        db.session.commit()
        lines = "\n".join(f"- {c.name}: {q}" for c, q in selected)
        sent = send_email(
            [supplier.email],
            f"MediLab Connect consumable order {order.order_number}",
            (
                f"Hello {supplier.contact_name or supplier.name},\n\n"
                "Please process the following consumable order for MediLab Connect.\n\n"
                f"Order number: {order.order_number}\n"
                f"Items:\n{lines}\n\n"
                "Please contact the laboratory if you need any clarification before dispatch.\n\n"
                "- MediLab Connect"
            ),
        )
        if sent:
            order.supplier_notified_at = datetime.now()
            db.session.commit()
        flash(
            "Order created and e-mailed to the supplier."
            if sent else
            "Order created. Supplier e-mail could not be sent; check SMTP settings.",
            "success" if sent else "error",
        )
        return redirect(url_for("manager.orders"))

    order_rows = ConsumableOrder.query.order_by(
        ConsumableOrder.ordered_at.desc()
    ).all()
    return render_template(
        "manager/orders.html",
        orders=order_rows,
        suppliers=_live(Supplier).order_by(Supplier.name).all(),
        consumables=_live(Consumable).order_by(Consumable.name).all(),
    )


@bp.route("/orders/items/<item_id>/receive", methods=["POST"])
def receive_order_item(item_id):
    item = db.session.get(ConsumableOrderItem, item_id)
    if not item:
        abort(404)
    if item.status != "ordered":
        flash("Only ordered items can be received.", "error")
        return redirect(url_for("manager.orders"))
    item.received_quantity = item.quantity
    item.received_at = datetime.now()
    item.status = "received"
    item.consumable.current_stock += item.quantity
    db.session.add(StockMovement(
        consumable_id=item.consumable_id,
        movement_type="in",
        quantity=item.quantity,
        notes=f"Received on order {item.order.order_number}",
        created_by=current_user.id,
    ))
    item.order.refresh_status()
    if item.order.status == "complete":
        item.order.received_at = item.order.completed_at
    log_audit(current_user.id, "receive_order_item", "consumable_order_item", item.id)
    db.session.commit()
    flash("Order item received and stock updated.", "success")
    return redirect(url_for("manager.orders"))


@bp.route("/orders/items/<item_id>/cancel", methods=["POST"])
def cancel_order_item(item_id):
    item = db.session.get(ConsumableOrderItem, item_id)
    if not item:
        abort(404)
    reason = (request.form.get("reason") or "").strip()
    if item.status != "ordered" or not reason:
        flash("An ordered item and cancellation reason are required.", "error")
        return redirect(url_for("manager.orders"))
    item.status = "cancelled"
    item.cancel_reason = reason
    item.cancelled_at = datetime.now()
    item.order.refresh_status()
    db.session.commit()
    supplier = item.order.supplier
    sent = send_email(
        [supplier.email if supplier else None],
        f"MediLab Connect order item cancelled: {item.order.order_number}",
        (
            f"Hello {supplier.contact_name or supplier.name if supplier else 'Supplier'},\n\n"
            f"One item on order {item.order.order_number} has been cancelled.\n\n"
            f"Item: {item.consumable.name}\n"
            f"Reason: {reason}\n\n"
            "No further action is required for this item unless the laboratory contacts you directly.\n\n"
            "- MediLab Connect"
        ),
    )
    log_audit(current_user.id, "cancel_order_item", "consumable_order_item", item.id,
              {"reason": reason, "supplier_notified": sent})
    db.session.commit()
    flash("Order item cancelled and supplier notified." if sent else "Order item cancelled.", "success")
    return redirect(url_for("manager.orders"))


@bp.route("/orders/<order_id>/cancel", methods=["POST"])
def cancel_order(order_id):
    order = db.session.get(ConsumableOrder, order_id)
    if not order:
        abort(404)
    reason = (request.form.get("reason") or "").strip()
    if not reason:
        flash("Cancellation reason is required.", "error")
        return redirect(url_for("manager.orders"))
    for item in order.items:
        if item.status == "ordered":
            item.status = "cancelled"
            item.cancel_reason = reason
            item.cancelled_at = datetime.now()
    order.status = "cancelled"
    order.cancel_reason = reason
    order.cancelled_at = datetime.now()
    db.session.commit()
    sent = send_email(
        [order.supplier.email if order.supplier else None],
        f"MediLab Connect order cancelled: {order.order_number}",
        (
            f"Hello {order.supplier.contact_name or order.supplier.name if order.supplier else 'Supplier'},\n\n"
            f"Order {order.order_number} has been cancelled.\n\n"
            f"Reason: {reason}\n\n"
            "No further action is required for this order unless the laboratory contacts you directly.\n\n"
            "- MediLab Connect"
        ),
    )
    if sent:
        order.supplier_notified_at = datetime.now()
    log_audit(current_user.id, "cancel_order", "consumable_order", order.id,
              {"reason": reason, "supplier_notified": sent})
    db.session.commit()
    flash("Order cancelled and supplier notified." if sent else "Order cancelled.", "success")
    return redirect(url_for("manager.orders"))


@bp.route("/orders/create", methods=["POST"])
def create_order():
    """Compatibility endpoint used by the quick single-item order page."""
    supplier_id = request.form.get("supplier_id")
    consumable_id = request.form.get("consumable_id")
    quantity = request.form.get("quantity") or "1"
    supplier = db.session.get(Supplier, supplier_id)
    consumable = db.session.get(Consumable, consumable_id)
    if (
        not supplier
        or supplier.deleted_at
        or not consumable
        or consumable.deleted_at
        or consumable.supplier_id != supplier.id
    ):
        flash("Consumable must be linked to the selected supplier.", "error")
        return redirect(url_for("manager.new_order"))
    order = ConsumableOrder(
        order_number="ORD-" + datetime.now().strftime("%Y%m%d%H%M%S%f"),
        supplier_id=supplier.id,
        consumable_id=consumable.id,
        quantity=int(quantity),
        status="ordered",
    )
    db.session.add(order)
    db.session.flush()
    db.session.add(ConsumableOrderItem(
        order_id=order.id,
        consumable_id=consumable.id,
        quantity=int(quantity),
        status="ordered",
    ))
    db.session.commit()
    send_email(
        [supplier.email],
        f"MediLab Connect consumable order {order.order_number}",
        (
            f"Hello {supplier.contact_name or supplier.name},\n\n"
            f"Please process the following consumable order for MediLab Connect.\n\n"
            f"Order number: {order.order_number}\n"
            f"Item: {consumable.name}\n"
            f"Quantity: {quantity}\n\n"
            "Please contact the laboratory if you need any clarification before dispatch.\n\n"
            "- MediLab Connect"
        ),
    )
    flash("Order created.", "success")
    return redirect(url_for("manager.orders"))

@bp.route("/requests/<request_id>")
def request_detail(request_id):

    req = TestRequest.query.get_or_404(request_id)

    technician_names = {}

    technicians = (
        User.query
        .join(UserRole, User.id == UserRole.user_id)
        .filter(UserRole.role == "lab_technician")
        .all()
    )

    for item in req.items:

        tech_name = "Not Assigned"

        if item.captured_by:
            tech = User.query.get(item.captured_by)

            if tech:
                tech_name = tech.full_name or tech.email

        technician_names[item.id] = tech_name

    return render_template(
        "manager/request_detail.html",
        req=req,
        technician_names=technician_names,
    )


@bp.route("/reports")
def reports():
    frm, to, start, end = parse_range(request.args)
    qs = TestRequest.query.filter(TestRequest.created_at.between(start, end))
    request_total = qs.count()
    total = TestRequestItem.query.filter(
        TestRequestItem.status.in_(("completed", "verified")),
        TestRequestItem.completed_at.between(start, end),
    ).count()
    released = qs.filter(TestRequest.status == "released").count()
    by_category = (db.session.query(TestCatalog.category, func.count(TestRequestItem.id))
                   .join(TestRequestItem, TestRequestItem.test_id == TestCatalog.id)
                   .filter(TestRequestItem.status.in_(("completed", "verified")),
                           TestRequestItem.completed_at.between(start, end))
                   .group_by(TestCatalog.category)
                   .order_by(func.count(TestRequestItem.id).desc()).all())
    low_stock = (Consumable.query
                 .filter(Consumable.deleted_at.is_(None))
                 .filter(Consumable.current_stock <= Consumable.reorder_level * 1.1)
                 .order_by(Consumable.current_stock).all())
    stock_chart_rows = sorted(
        low_stock,
        key=lambda item: item.current_stock / max(item.reorder_level or 1, 1),
    )[:8]
    if request.args.get("format") == "pdf":
        sections = [{
            "heading": "Tests performed by category",
            "headers": ["Category", "Tests performed"],
            "rows": [[c or "-", n] for c, n in by_category] or [["No data", 0]],
        }, {
            "heading": "Low / reorder-level stock",
            "headers": ["SKU", "Item", "On hand", "Reorder level"],
            "rows": [[c.sku, c.name, c.current_stock, c.reorder_level] for c in low_stock]
                    or [["-", "All consumables above reorder level", "", ""]],
        }]
        buf = build_report_pdf(
            "Laboratory Manager Report",
            subtitle=f"Range: {frm:%Y-%m-%d} \u2192 {to:%Y-%m-%d}",
            summary=[f"Tests performed in range: <b>{total}</b>",
                     f"Released in range: <b>{released}</b>",
                     f"Distinct categories worked: <b>{len(by_category)}</b>"],
            sections=sections,
        )
        return send_file(buf, mimetype="application/pdf", as_attachment=True,
                         download_name=f"manager-report-{frm}_{to}.pdf")
    return render_template("manager/reports.html", total=total, released=released,
                           by_category=by_category, low_stock=low_stock,
                           frm=frm, to=to,
                           category_chart_labels=[c or "-" for c, _n in by_category],
                           category_chart_values=[n for _c, n in by_category],
                           release_chart_labels=["Released", "Not released"],
                           release_chart_values=[released, max(request_total - released, 0)],
                           stock_chart_labels=[item.name for item in stock_chart_rows],
                           stock_chart_current=[item.current_stock for item in stock_chart_rows],
                           stock_chart_reorder=[item.reorder_level for item in stock_chart_rows])
