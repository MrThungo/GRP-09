import secrets  # <-- Added secrets import here
import os
import uuid
from datetime import date, datetime, timedelta
from flask import Blueprint, render_template, redirect, url_for, request, flash, abort, send_file, jsonify, current_app, Response
from flask_login import login_required, current_user
from sqlalchemy import func, or_
from sqlalchemy.orm import joinedload, selectinload
from werkzeug.utils import secure_filename

from ..consultation_recordings import delete_recording_file
from ..extensions import db
from ..auth_utils import role_required
from ..models import (
    Patient,
    Sample,
    TestRequest,
    TestRequestItem,
    TestCatalog,
    PRIORITIES,
    User,
    UserRole,
    ROLE_LABELS,
    ConsentGrant,
    AccessRequest,
    Notification,
    Condition,
    Allergy,
    Medication,
    GENDER_OPTIONS,
    OnlineConsultation,
    ConsultationSignal,
    DoctorAvailabilitySlot,
)
from ..notification_pages import clear_user_notifications, mark_user_notifications_read, render_user_notifications
from ..reports import build_request_results_pdf
from ..sa_id import validate_sa_id
from ..services import doctor_return_item_for_review, release_request, log_audit, notify, send_email
from ..url_utils import external_url_for
from ..whatsapp import send_account_welcome_whatsapp

bp = Blueprint("doctor", __name__, template_folder="../templates/doctor")

ALLOWED_AVATAR_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


def _ready_to_release_query(query):
    return query.filter(
        TestRequest.status == "completed",
        TestRequest.items.any(TestRequestItem.status != "cancelled"),
        ~TestRequest.items.any(TestRequestItem.status.notin_(("verified", "cancelled"))),
    )


def _unique_sample_barcode(request_number, sample_index, sample_type):
    for _ in range(12):
        barcode = Sample.generate_barcode(request_number, sample_index, sample_type)
        if not Sample.query.filter_by(barcode=barcode).first():
            return barcode
    while True:
        barcode = Sample.generate_simple_barcode()
        if not Sample.query.filter_by(barcode=barcode).first():
            return barcode


def _reference_label(test):
    if test.reference_low is not None and test.reference_high is not None:
        return f"{test.reference_low} - {test.reference_high} {test.units or ''}".strip()
    return test.reference_text or "-"


@bp.before_request
@login_required
@role_required("doctor")
def _gate():
    pass


@bp.route("/notifications")
def notifications():
    return render_user_notifications("doctor.mark_all_read", "doctor.clear_all_notifications")


@bp.route("/notifications/mark-all-read", methods=["POST"])
def mark_all_read():
    return mark_user_notifications_read("doctor.notifications")


@bp.route("/notifications/clear-all", methods=["POST"])
def clear_all_notifications():
    return clear_user_notifications("doctor.notifications")


@bp.route("/")
def dashboard():
    base_query = TestRequest.query.filter_by(doctor_id=current_user.id)

    status_counts = {
        status: 0
        for status in ["submitted", "samples_received", "in_progress", "completed", "verified", "released", "cancelled"]
    }
    status_counts.update(dict(
        db.session.query(TestRequest.status, func.count(TestRequest.id))
        .filter(TestRequest.doctor_id == current_user.id)
        .group_by(TestRequest.status)
        .all()
    ))

    ready_count = _ready_to_release_query(base_query).count()
    
    stats = {
        "open": sum(status_counts.get(status, 0) for status in ["submitted", "samples_received", "in_progress"]),
        "verified": ready_count,
        "released": status_counts.get("released", 0),
        "total": sum(status_counts.values()),
    }
    
    recent = (
        base_query
        .options(selectinload(TestRequest.patient))
        .order_by(TestRequest.created_at.desc())
        .limit(8)
        .all()
    )
    
    today = date.today()
    week_start = today - timedelta(days=6)
    weekly_counts = dict(
        db.session.query(func.date(TestRequest.created_at), func.count(TestRequest.id))
        .filter(
            TestRequest.doctor_id == current_user.id,
            TestRequest.created_at >= datetime.combine(week_start, datetime.min.time()),
        )
        .group_by(func.date(TestRequest.created_at))
        .all()
    )
    weekly_days = [today - timedelta(days=i) for i in range(6, -1, -1)]
    weekly_labels = [day.strftime('%a') for day in weekly_days]
    weekly_data = [weekly_counts.get(day.isoformat(), 0) for day in weekly_days]
    
    # Get abnormal count for last 7 days
    week_ago = today - timedelta(days=7)
    abnormal_count = TestRequestItem.query.join(TestRequest).filter(
        TestRequest.doctor_id == current_user.id,
        TestRequestItem.abnormal_flag.isnot(None),
        TestRequestItem.captured_at >= week_ago
    ).count()
    
    # Get priority counts
    priority_counts = {"routine": 0, "urgent": 0, "stat": 0}
    priority_counts.update(dict(
        db.session.query(TestRequest.priority, func.count(TestRequest.id))
        .filter(TestRequest.doctor_id == current_user.id)
        .group_by(TestRequest.priority)
        .all()
    ))
    
    today_date = today.isoformat()
    current_time = datetime.now().strftime('%H:%M:%S')
    
    return render_template("doctor/dashboard.html", 
                         stats=stats,
                         status_counts=status_counts,
                         recent=recent,
                         today_requests=[],
                         today_date=today_date,
                         current_time=current_time,
                         weekly_labels=weekly_labels,
                         weekly_data=weekly_data,
                         priority_counts=priority_counts,
                         abnormal_count=abnormal_count)


@bp.route("/patients")
def patients():
    q = (request.args.get("q") or "").strip()
    query = (
        Patient.query
        .outerjoin(User, Patient.profile_id == User.id)
        .filter(Patient.deleted_at.is_(None))
    )
    if q:
        like = f"%{q}%"
        digits = "".join(ch for ch in q if ch.isdigit())
        id_like = f"%{digits}%" if digits else like
        query = query.filter(or_(
            Patient.full_name.ilike(like),
            Patient.surname.ilike(like),
            Patient.mrn.ilike(like),
            Patient.email.ilike(like),
            Patient.phone.ilike(like),
            Patient.id_number.ilike(id_like),
            User.sa_id_number.ilike(id_like),
            User.email.ilike(like),
        ))
    rows = query.order_by(Patient.full_name).all()
    return render_template("doctor/patients.html", patients=rows, q=q)


@bp.get("/patients/search")
def patient_search():
    q = (request.args.get("q") or "").strip()
    digits = "".join(ch for ch in q if ch.isdigit())
    if len(q) < 2 and len(digits) < 2:
        return jsonify({"patients": []})

    like = f"%{q}%"
    id_like = f"%{digits}%" if digits else like
    rows = (
        db.session.query(Patient, User.sa_id_number.label("profile_sa_id"))
        .outerjoin(User, Patient.profile_id == User.id)
        .filter(Patient.deleted_at.is_(None))
        .filter(or_(
            Patient.full_name.ilike(like),
            Patient.surname.ilike(like),
            Patient.mrn.ilike(like),
            Patient.email.ilike(like),
            Patient.phone.ilike(like),
            Patient.id_number.ilike(id_like),
            User.sa_id_number.ilike(id_like),
            User.email.ilike(like),
        ))
        .order_by(Patient.full_name)
        .limit(15)
        .all()
    )

    return jsonify({
        "patients": [
            {
                "id": patient.id,
                "full_name": patient.full_name,
                "mrn": patient.mrn,
                "id_number": patient.id_number or profile_sa_id or "",
                "email": patient.email or "",
                "phone": patient.phone or "",
            }
            for patient, profile_sa_id in rows
        ]
    })


@bp.get("/patients/<patient_id>/history")
def patient_history(patient_id):
    patient = (
        Patient.query
        .options(
            selectinload(Patient.conditions),
            selectinload(Patient.allergy_list),
            selectinload(Patient.medications),
        )
        .filter(Patient.id == patient_id, Patient.deleted_at.is_(None))
        .first()
    )
    if not patient:
        abort(404)

    grants = (
        ConsentGrant.query
        .options(
            selectinload(ConsentGrant.requests),
            selectinload(ConsentGrant.request_items),
        )
        .filter_by(
            doctor_id=current_user.id,
            patient_id=patient.id,
            revoked_at=None,
        )
        .all()
    )
    shared_request_ids = {
        shared_request.id
        for grant in grants
        for shared_request in grant.requests
    }
    shared_request_ids.update(
        item.request_id
        for grant in grants
        for item in grant.request_items
    )

    requests = (
        TestRequest.query
        .options(
            selectinload(TestRequest.items).joinedload(TestRequestItem.test),
            selectinload(TestRequest.samples),
            joinedload(TestRequest.doctor),
        )
        .filter(
            TestRequest.patient_id == patient.id,
            or_(
                TestRequest.doctor_id == current_user.id,
                TestRequest.id.in_(shared_request_ids),
            ),
        )
        .order_by(TestRequest.created_at.desc())
        .limit(10)
        .all()
    )
    visible_requests = []
    for req in requests:
        if req.doctor_id == current_user.id:
            visible_items = list(req.items)
        else:
            request_grants = [
                grant for grant in grants
                if (
                    any(shared.id == req.id for shared in grant.requests)
                    or any(item.request_id == req.id for item in grant.request_items)
                )
            ]
            visible_items = _shared_items_for_request(req, request_grants)
        if visible_items:
            visible_requests.append((req, visible_items))

    return jsonify({
        "patient": {
            "full_name": patient.full_name,
            "mrn": patient.mrn,
            "id_number": patient.id_number or "",
            "date_of_birth": patient.date_of_birth.isoformat() if patient.date_of_birth else "",
            "blood_type": patient.blood_type or "",
            "phone": patient.phone or "",
            "email": patient.email or "",
            "chronic_conditions": patient.chronic_conditions or "",
            "conditions": [condition.name for condition in patient.conditions],
            "allergies": [allergy.name for allergy in patient.allergy_list],
            "allergy_notes": patient.allergies or "",
            "medications": [medication.name for medication in patient.medications],
            "medication_notes": patient.current_medication or "",
        },
        "requests": [
            {
                "request_number": req.request_number,
                "status": req.status,
                "priority": req.priority,
                "created_at": req.created_at.strftime("%Y-%m-%d %H:%M"),
                "released_at": req.released_at.strftime("%Y-%m-%d %H:%M") if req.released_at else "",
                "doctor": req.doctor.full_name if req.doctor else "",
                "samples": [
                    {
                        "barcode": sample.barcode,
                        "sample_type": sample.sample_type,
                        "status": sample.status,
                    }
                    for sample in req.samples
                ],
                "items": [
                    {
                        "code": item.test.code if item.test else "",
                        "name": item.test.name if item.test else "Unknown test",
                        "status": item.status,
                        "result": str(item.result_value) if item.result_value is not None else (item.result_text or ""),
                        "units": item.test.units if item.test else "",
                        "flag": item.abnormal_flag or "",
                        "reference": _reference_label(item.test) if item.test else "-",
                    }
                    for item in visible_items
                ],
            }
            for req, visible_items in visible_requests
        ],
    })


@bp.route("/requests")
def requests_list():
    query = TestRequest.query.filter_by(doctor_id=current_user.id)
    
    status = request.args.get('status')
    if status:
        statuses = status.split(',')
        query = query.filter(TestRequest.status.in_(statuses))
    
    priority = request.args.get('priority')
    if priority:
        query = query.filter_by(priority=priority)
    
    ready_filter = request.args.get('verified') == '1'
    if ready_filter:
        query = _ready_to_release_query(query)
    
    rows = query.order_by(TestRequest.created_at.desc()).all()
    today_date = date.today().isoformat()
    
    return render_template(
        "doctor/requests.html",
        requests=rows,
        today_date=today_date,
        ready_filter=ready_filter,
    )


@bp.route("/requests/new", methods=["GET", "POST"])
def request_new():
    if request.method == "POST":
        patient_id = request.form.get("patient_id")
        priority = (request.form.get("priority") or "routine")
        clinical_notes = (request.form.get("clinical_notes") or "")
        test_ids = list(dict.fromkeys(request.form.getlist("test_ids")))
        barcodes = request.form.getlist("barcode")
        sample_types = request.form.getlist("sample_type")
        samples = []
        for index, sample_type in enumerate(sample_types):
            sample_type = (sample_type or "").strip()
            barcode = (barcodes[index] if index < len(barcodes) else "").strip()
            if sample_type:
                samples.append({"barcode": barcode, "sample_type": sample_type})

        if not patient_id:
            flash("Patient is required.", "error")
            return redirect(url_for("doctor.request_new"))
        if priority not in PRIORITIES:
            flash("Invalid priority selected.", "error")
            return redirect(url_for("doctor.request_new"))
        if not test_ids:
            flash("At least one test type is required.", "error")
            return redirect(url_for("doctor.request_new"))
        selected_tests = (
            TestCatalog.query
            .filter(TestCatalog.id.in_(test_ids), TestCatalog.active.is_(True), TestCatalog.deleted_at.is_(None))
            .all()
        )
        if len(selected_tests) != len(test_ids):
            flash("One or more selected tests are no longer available.", "error")
            return redirect(url_for("doctor.request_new"))
        required_sample_types = []
        for test in selected_tests:
            sample_type = (test.sample_type or "").strip()
            if sample_type and sample_type.casefold() not in {value.casefold() for value in required_sample_types}:
                required_sample_types.append(sample_type)
        posted_types = {(sample["sample_type"] or "").casefold() for sample in samples}
        for sample_type in required_sample_types:
            if sample_type.casefold() not in posted_types:
                samples.append({"barcode": "", "sample_type": sample_type})
        if not samples:
            flash("At least one sample is required.", "error")
            return redirect(url_for("doctor.request_new"))
        unique_samples = []
        seen_types = set()
        for sample in samples:
            key = sample["sample_type"].casefold()
            if key in seen_types:
                continue
            seen_types.add(key)
            unique_samples.append(sample)
        samples = unique_samples

        req = TestRequest(
            request_number=TestRequest.generate_number(),
            patient_id=patient_id,
            doctor_id=current_user.id,
            priority=priority,
            clinical_notes=clinical_notes,
            status="submitted"
        )
        db.session.add(req)
        db.session.flush()

        for test in selected_tests:
            db.session.add(TestRequestItem(request_id=req.id, test_id=test.id, status="submitted"))

        seen_barcodes = set()
        for index, sample in enumerate(samples, start=1):
            barcode = sample["barcode"] or _unique_sample_barcode(req.request_number, index, sample["sample_type"])
            if barcode.casefold() in seen_barcodes:
                db.session.rollback()
                flash(f"Barcode {barcode} is duplicated in this request.", "error")
                return redirect(url_for("doctor.request_new"))
            seen_barcodes.add(barcode.casefold())
            existing_barcode = Sample.query.filter_by(barcode=barcode).first()
            if existing_barcode:
                db.session.rollback()
                flash(f"Barcode {barcode} already exists.", "error")
                return redirect(url_for("doctor.request_new"))
            db.session.add(Sample(
                request_id=req.id,
                barcode=barcode,
                sample_type=sample["sample_type"],
                status="collected",
            ))

        patient = db.session.get(Patient, patient_id)
        if patient and patient.profile_id:
            notify(patient.profile_id, "New Test Request Submitted",(f"Your test request {req.request_number} has been submitted."), "/patient/requests")
        if patient and patient.email:
            send_email(
                [patient.email],
                f"MediLab Connect test request submitted: {req.request_number}",
                (
                    f"Hello {patient.full_name},\n\n"
                    f"Dr. {current_user.full_name or current_user.email} has submitted laboratory request {req.request_number} on your behalf.\n\n"
                    "You can sign in to the MediLab Connect portal to follow the request status and view updates when they are available.\n\n"
                    "- MediLab Connect"
                ),
            )

        log_audit(current_user.id, "create_request", "test_request", req.id)
        db.session.commit()
        flash(f"Request {req.request_number} created successfully.", "success")
        return redirect(url_for("doctor.request_detail", request_id=req.id))

    tests = (
        TestCatalog.query
        .filter(TestCatalog.active.is_(True), TestCatalog.deleted_at.is_(None))
        .order_by(TestCatalog.category, TestCatalog.code)
        .all()
    )
    return render_template("doctor/request_new.html", tests=tests, priorities=PRIORITIES)


def _shared_grants_for_request(req):
    return (
        ConsentGrant.query
        .filter_by(doctor_id=current_user.id, patient_id=req.patient_id, revoked_at=None)
        .filter(or_(
            ConsentGrant.requests.any(TestRequest.id == req.id),
            ConsentGrant.request_items.any(TestRequestItem.request_id == req.id),
        ))
        .order_by(ConsentGrant.granted_at.desc())
        .all()
    )


def _shared_items_for_request(req, grants):
    selected_ids = set()
    full_request_shared = False
    for grant in grants:
        grant_item_ids = {
            item.id for item in grant.request_items
            if item.request_id == req.id
        }
        if grant_item_ids:
            selected_ids.update(grant_item_ids)
        elif any(shared.id == req.id for shared in grant.requests):
            full_request_shared = True
    if full_request_shared:
        return list(req.items)
    return [item for item in req.items if item.id in selected_ids]


def _active_consultation_for_request(req):
    return (OnlineConsultation.query
            .filter(
                OnlineConsultation.request_id == req.id,
                OnlineConsultation.doctor_id == current_user.id,
                OnlineConsultation.status.in_((
                    "offered", "online_requested", "in_person_requested",
                    "in_person_booked", "invited", "accepted", "started",
                )),
            )
            .order_by(OnlineConsultation.created_at.desc())
            .first())


def _latest_consultation_for_request(req):
    return (OnlineConsultation.query
            .filter_by(request_id=req.id, doctor_id=current_user.id)
            .order_by(OnlineConsultation.created_at.desc())
            .first())


def _portal_url(endpoint, **values):
    return external_url_for(endpoint, **values)


def _patient_email_for_consultation(consultation):
    patient = consultation.patient
    if not patient:
        return ""
    profile_email = patient.profile.email if patient.profile else ""
    return patient.email or profile_email or ""


def _email_patient_consultation_notice(consultation, subject, body, endpoint, **values):
    email = _patient_email_for_consultation(consultation)
    if not email:
        return False
    link = _portal_url(endpoint, **values)
    patient_name = consultation.patient.full_name if consultation.patient else email
    return send_email(
        [email],
        subject,
        (
            f"Hello {patient_name},\n\n"
            f"{body}\n\n"
            f"Open consultation: {link}\n\n"
            "- MediLab Connect"
        ),
    )


def _ensure_consultation_offer(req, message=None):
    existing = _active_consultation_for_request(req)
    if existing:
        return existing, False
    if not (req.patient and req.patient.profile_id):
        raise ValueError("This patient does not have a portal account for consultation invites.")
    consultation = OnlineConsultation(
        request_id=req.id,
        patient_id=req.patient_id,
        doctor_id=current_user.id,
        requested_by_id=current_user.id,
        status="offered",
        invite_message=message,
    )
    db.session.add(consultation)
    db.session.flush()
    notify(
        req.patient.profile_id,
        "Choose how to discuss your results",
        (
            f"Your results for {req.request_number} are ready. "
            "Please choose an in-person discussion or an invite-only online consultation."
        ),
        url_for("patient.consultation_detail", consultation_id=consultation.id),
    )
    _email_patient_consultation_notice(
        consultation,
        f"MediLab Connect consultation choice: {req.request_number}",
        (
            f"Your results for {req.request_number} are ready. "
            "Please choose whether you prefer an in-person discussion or an invite-only online consultation."
        ),
        "patient.consultation_detail",
        consultation_id=consultation.id,
    )
    log_audit(current_user.id, "offer_online_consultation", "online_consultation", consultation.id)
    return consultation, True


def _doctor_consultation_or_404(consultation_id, room_token=None):
    consultation = (OnlineConsultation.query
                    .options(
                        selectinload(OnlineConsultation.patient),
                        selectinload(OnlineConsultation.request),
                    )
                    .filter_by(id=consultation_id, doctor_id=current_user.id)
                    .first())
    if not consultation:
        abort(404)
    if room_token is not None and consultation.room_token != room_token:
        abort(404)
    return consultation


def _doctor_availability_slot_or_404(slot_id):
    slot = (DoctorAvailabilitySlot.query
            .options(
                selectinload(DoctorAvailabilitySlot.booked_consultation)
                .selectinload(OnlineConsultation.patient),
                selectinload(DoctorAvailabilitySlot.booked_consultation)
                .selectinload(OnlineConsultation.request),
            )
            .filter_by(id=slot_id, doctor_id=current_user.id)
            .first())
    if not slot:
        abort(404)
    return slot


def _parse_datetime_local(value):
    value = (value or "").strip()
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _upcoming_availability_slots():
    return (DoctorAvailabilitySlot.query
            .options(
                selectinload(DoctorAvailabilitySlot.booked_consultation)
                .selectinload(OnlineConsultation.patient),
                selectinload(DoctorAvailabilitySlot.booked_consultation)
                .selectinload(OnlineConsultation.request),
            )
            .filter(
                DoctorAvailabilitySlot.doctor_id == current_user.id,
                DoctorAvailabilitySlot.ends_at >= datetime.now(),
                DoctorAvailabilitySlot.status != "cancelled",
            )
            .order_by(DoctorAvailabilitySlot.starts_at.asc())
            .limit(40)
            .all())


@bp.route("/requests/<request_id>")
def request_detail(request_id):
    req = db.session.get(TestRequest, request_id)
    if not req:
        abort(404)
    shared_via = None
    visible_items = list(req.items)
    if req.doctor_id != current_user.id:
        shared_grants = _shared_grants_for_request(req)
        if not shared_grants:
            abort(404)
        shared_via = shared_grants[0]
        visible_items = _shared_items_for_request(req, shared_grants)
        if not visible_items:
            abort(404)
    consultation = None if shared_via else _latest_consultation_for_request(req)
    return render_template(
        "doctor/request_detail.html",
        req=req,
        items=visible_items,
        shared_via=shared_via,
        consultation=consultation,
    )


@bp.route("/requests/<request_id>/release", methods=["POST"])
def request_release(request_id):
    req = db.session.get(TestRequest, request_id)
    if not req or req.doctor_id != current_user.id:
        abort(404)
    if req.status == "released":
        flash("Results have already been released.", "success")
        return redirect(url_for("doctor.request_detail", request_id=req.id))
    if not req.all_verified:
        flash("Only fully verified requests can be released.", "error")
        return redirect(url_for("doctor.request_detail", request_id=req.id))
    note = (request.form.get("release_note") or "").strip() or None
    release_request(req, current_user.id, note=note)
    try:
        _ensure_consultation_offer(req)
    except ValueError:
        pass
    db.session.commit()
    flash("Results released to patient.", "success")
    return redirect(url_for("doctor.request_detail", request_id=req.id))


@bp.route("/requests/<request_id>/consultation-offer", methods=["POST"])
def offer_consultation(request_id):
    req = db.session.get(TestRequest, request_id)
    if not req or req.doctor_id != current_user.id:
        abort(404)
    if req.status != "released":
        flash("Release the patient's results before offering a consultation.", "error")
        return redirect(url_for("doctor.request_detail", request_id=req.id))
    message = (request.form.get("message") or "").strip() or None
    try:
        _, created = _ensure_consultation_offer(req, message=message)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("doctor.request_detail", request_id=req.id))
    db.session.commit()
    flash(
        "Consultation choice sent to the patient." if created else "The patient already has an active consultation choice.",
        "success",
    )
    return redirect(url_for("doctor.request_detail", request_id=req.id))


@bp.route("/consultations")
def consultations():
    rows = (OnlineConsultation.query
            .options(
                selectinload(OnlineConsultation.patient),
                selectinload(OnlineConsultation.request),
            )
            .filter_by(doctor_id=current_user.id)
            .order_by(
                OnlineConsultation.scheduled_at.desc().nullslast(),
                OnlineConsultation.created_at.desc(),
            )
            .all())
    return render_template(
        "doctor/consultations.html",
        consultations=rows,
        availability_slots=_upcoming_availability_slots(),
    )


@bp.route("/consultations/availability", methods=["POST"])
def create_availability_slot():
    starts_at = _parse_datetime_local(request.form.get("starts_at"))
    if not starts_at:
        flash("Choose a valid available date and time.", "error")
        return redirect(url_for("doctor.consultations"))
    try:
        duration = int(request.form.get("duration_minutes") or 30)
    except ValueError:
        duration = 30
    duration = min(240, max(10, duration))
    ends_at = starts_at + timedelta(minutes=duration)
    if starts_at < datetime.now() - timedelta(minutes=5):
        flash("Availability cannot be created in the past.", "error")
        return redirect(url_for("doctor.consultations"))

    overlap = (DoctorAvailabilitySlot.query
               .filter(
                   DoctorAvailabilitySlot.doctor_id == current_user.id,
                   DoctorAvailabilitySlot.status.in_(("open", "booked")),
                   DoctorAvailabilitySlot.starts_at < ends_at,
                   DoctorAvailabilitySlot.ends_at > starts_at,
               )
               .first())
    if overlap:
        flash("That availability overlaps with another open slot.", "error")
        return redirect(url_for("doctor.consultations"))

    slot = DoctorAvailabilitySlot(
        doctor_id=current_user.id,
        starts_at=starts_at,
        ends_at=ends_at,
        location=(request.form.get("location") or "").strip() or None,
        note=(request.form.get("note") or "").strip() or None,
        status="open",
    )
    db.session.add(slot)
    db.session.flush()
    log_audit(current_user.id, "create_availability_slot", "doctor_availability_slot", slot.id)
    db.session.commit()
    flash("In-person availability added.", "success")
    return redirect(url_for("doctor.consultations"))


@bp.route("/consultations/availability/<slot_id>/cancel", methods=["POST"])
def cancel_availability_slot(slot_id):
    slot = _doctor_availability_slot_or_404(slot_id)
    if slot.status == "booked" or slot.booked_consultation_id:
        flash("Booked appointment slots cannot be cancelled here. Contact the patient first.", "error")
        return redirect(url_for("doctor.consultations"))
    slot.status = "cancelled"
    log_audit(current_user.id, "cancel_availability_slot", "doctor_availability_slot", slot.id)
    db.session.commit()
    flash("Availability slot cancelled.", "success")
    return redirect(url_for("doctor.consultations"))


@bp.route("/consultations/<consultation_id>/invite", methods=["POST"])
def invite_consultation(consultation_id):
    consultation = _doctor_consultation_or_404(consultation_id)
    if consultation.status not in ("online_requested", "invited", "declined"):
        flash("The patient must request an online consultation before you can send a meeting invite.", "error")
        return redirect(url_for("doctor.consultations"))
    scheduled_at = _parse_datetime_local(request.form.get("scheduled_at"))
    if not scheduled_at:
        flash("Please choose a valid meeting date and time.", "error")
        return redirect(url_for("doctor.consultations"))
    try:
        duration = int(request.form.get("duration_minutes") or 30)
    except ValueError:
        duration = 30
    duration = min(120, max(10, duration))
    consultation.status = "invited"
    consultation.scheduled_at = scheduled_at
    consultation.scheduled_end_at = scheduled_at + timedelta(minutes=duration)
    consultation.patient_response = None
    consultation.patient_responded_at = None
    consultation.doctor_started_at = None
    consultation.ended_at = None
    consultation.invite_message = (request.form.get("message") or "").strip() or consultation.invite_message
    if consultation.patient_user_id:
        notify(
            consultation.patient_user_id,
            "Online consultation invite",
            (
                f"Your doctor invited you to discuss {consultation.request.request_number} "
                f"on {scheduled_at.strftime('%Y-%m-%d at %H:%M')}."
            ),
            url_for("patient.consultation_detail", consultation_id=consultation.id),
        )
    _email_patient_consultation_notice(
        consultation,
        f"MediLab Connect online consultation invite: {consultation.request.request_number}",
        (
            f"Your doctor invited you to discuss {consultation.request.request_number} "
            f"on {scheduled_at.strftime('%Y-%m-%d at %H:%M')}."
        ),
        "patient.consultation_detail",
        consultation_id=consultation.id,
    )
    log_audit(current_user.id, "invite_online_consultation", "online_consultation", consultation.id)
    db.session.commit()
    flash("Online consultation invite sent.", "success")
    return redirect(url_for("doctor.consultations"))


@bp.route("/consultations/<consultation_id>/start", methods=["POST"])
def start_consultation(consultation_id):
    consultation = _doctor_consultation_or_404(consultation_id)
    if consultation.status not in ("accepted", "started"):
        flash("The patient must accept the meeting time before the session can start.", "error")
        return redirect(url_for("doctor.consultations"))
    was_started = consultation.status == "started"
    consultation.status = "started"
    consultation.doctor_started_at = consultation.doctor_started_at or datetime.now()
    if not was_started:
        ConsultationSignal.query.filter_by(consultation_id=consultation.id).delete()
        if consultation.patient_user_id:
            notify(
                consultation.patient_user_id,
                "Online consultation started",
                "Your doctor has opened the secure consultation room.",
                url_for("patient.consultation_waiting", consultation_id=consultation.id, room_token=consultation.room_token),
            )
        _email_patient_consultation_notice(
            consultation,
            f"MediLab Connect online consultation started: {consultation.request.request_number}",
            "Your doctor has opened the secure consultation room.",
            "patient.consultation_waiting",
            consultation_id=consultation.id,
            room_token=consultation.room_token,
        )
    log_audit(current_user.id, "start_online_consultation", "online_consultation", consultation.id)
    db.session.commit()
    return redirect(url_for("doctor.consultation_room", consultation_id=consultation.id, room_token=consultation.room_token))


@bp.route("/consultations/<consultation_id>/end", methods=["POST"])
def end_consultation(consultation_id):
    consultation = _doctor_consultation_or_404(consultation_id)
    if consultation.status not in ("accepted", "started"):
        flash("Only accepted or active sessions can be ended.", "error")
        return redirect(url_for("doctor.consultations"))
    consultation.status = "completed"
    consultation.ended_at = datetime.now()
    try:
        delete_recording_file(consultation)
    except Exception:
        current_app.logger.warning(
            "Consultation recording cleanup failed for %s",
            consultation.id,
            exc_info=True,
        )
    log_audit(current_user.id, "complete_online_consultation", "online_consultation", consultation.id)
    db.session.commit()
    flash("Online consultation completed.", "success")
    return redirect(url_for("doctor.consultations"))


@bp.route("/consultations/<consultation_id>/record")
def consultation_record(consultation_id):
    _doctor_consultation_or_404(consultation_id)
    flash("Saved consultation videos have been disabled.", "info")
    return redirect(url_for("doctor.consultations"))


@bp.route("/consultations/<consultation_id>/record/video")
def consultation_record_video(consultation_id):
    _doctor_consultation_or_404(consultation_id)
    abort(404)


@bp.route("/consultations/<consultation_id>/record/extend", methods=["POST"])
def extend_consultation_record(consultation_id):
    _doctor_consultation_or_404(consultation_id)
    flash("Saved consultation videos have been disabled.", "info")
    return redirect(url_for("doctor.consultations"))


@bp.route("/consultations/<consultation_id>/room/<room_token>")
def consultation_room(consultation_id, room_token):
    consultation = _doctor_consultation_or_404(consultation_id, room_token=room_token)
    if consultation.status not in ("accepted", "started"):
        flash("The patient must accept the invite before the meeting room opens.", "error")
        return redirect(url_for("doctor.consultations"))
    return render_template("consultations/room.html", consultation=consultation, role="doctor")


@bp.route("/requests/<request_id>/items/<item_id>/send-back", methods=["POST"])
def send_result_back(request_id, item_id):
    req = db.session.get(TestRequest, request_id)
    item = db.session.get(TestRequestItem, item_id)
    if not req or not item or item.request_id != req.id or req.doctor_id != current_user.id:
        abort(404)
    note = (request.form.get("note") or "").strip()
    try:
        doctor_return_item_for_review(item, current_user.id, note)
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "error")
        return redirect(url_for("doctor.request_detail", request_id=req.id))
    db.session.commit()
    flash("Result sent back to the technician for review.", "success")
    return redirect(url_for("doctor.request_detail", request_id=req.id))


@bp.route("/requests/<request_id>/items/<item_id>/cancel", methods=["POST"])
def cancel_item(request_id, item_id):
    """Cancels a single test item when multiple tests exist in the request."""
    req = db.session.get(TestRequest, request_id)
    item = db.session.get(TestRequestItem, item_id)
    
    if not req or not item or item.request_id != req.id or req.doctor_id != current_user.id:
        abort(404)
        
    if req.status not in ['submitted', 'samples_received']:
        flash("This request's current timeline status blocks item cancellation.", "error")
        return redirect(url_for("doctor.request_detail", request_id=req.id))
        
    reason = request.form.get("reason", "").strip()
    if not reason:
        flash("A cancellation reason must be specified.", "error")
        return redirect(url_for("doctor.request_detail", request_id=req.id))
        
    item.status = "cancelled"
    item.review_notes = f"Item cancelled by Doctor. Reason: {reason}"
    
    active_items = [i for i in req.items if i.status != "cancelled"]
    if not active_items:
        req.status = "cancelled"
        
    log_audit(current_user.id, "cancel_item", "test_request_item", item.id, {"reason": reason})
    db.session.commit()
    
    flash(f"Test item {item.test.name} has been cancelled.", "success")
    return redirect(url_for("doctor.request_detail", request_id=req.id))


@bp.route("/requests/<request_id>/cancel", methods=["POST"])
def cancel_request(request_id):
    req = db.session.get(TestRequest, request_id)
    if not req:
        abort(404)
    if req.doctor_id != current_user.id:
        abort(403)
        
    if req.status not in ['submitted', 'samples_received']:
        flash("Only submitted or received requests may be cancelled.", "error")
        return redirect(url_for("doctor.request_detail", request_id=req.id))
        
    reason = (request.form.get("reason") or "").strip()
    if not reason:
        flash("Cancellation reason required.", "error")
        return redirect(url_for("doctor.request_detail", request_id=req.id))
        
    req.status = "cancelled"
    req.cancel_reason = reason
    req.cancelled_by = current_user.id
    req.cancelled_at = datetime.now()
    
    for item in req.items:
        item.status = "cancelled"
        item.review_notes = f"Request cancelled by Doctor. Reason: {reason}"
        
    patient = req.patient
    if patient and patient.profile_id:
        notify(patient.profile_id, "Test Request Cancelled", f"Request {req.request_number} has been cancelled.", "")
    if patient and patient.email:
        send_email(
            [patient.email],
            f"MediLab Connect request cancelled: {req.request_number}",
            (
                f"Hello {patient.full_name or patient.email},\n\n"
                f"Laboratory request {req.request_number} has been cancelled by the requesting doctor.\n\n"
                f"Reason: {reason}\n\n"
                "If you have questions, please contact your doctor or the laboratory.\n\n"
                "- MediLab Connect"
            ),
        )
        
    log_audit(current_user.id, "cancel_request", "test_request", req.id, {"reason": reason})
    db.session.commit()
    
    flash("Request cancelled.", "success")
    return redirect(url_for("doctor.request_detail", request_id=req.id))


@bp.route("/shared")
def shared_requests():
    grants = (ConsentGrant.query
              .filter_by(doctor_id=current_user.id, revoked_at=None)
              .order_by(ConsentGrant.granted_at.desc()).all())
    return render_template("doctor/shared.html", grants=grants)


@bp.route("/users/new", methods=["GET", "POST"])
def user_new():
    selected_condition_ids = set()
    selected_allergy_ids = set()
    selected_medication_ids = set()

    if request.method == "POST":
        full_name = (request.form.get("first_name") or "").strip()
        email = (request.form.get("email") or "").lower().strip()
        raw_id_no = (request.form.get("id_number") or "").strip()
        id_no = "".join(ch for ch in raw_id_no if ch.isdigit()) if raw_id_no else ""
        dob_text = (request.form.get("date_of_birth") or "").strip()
        cellphone = (request.form.get("cellphone") or "").strip()
        selected_condition_ids = set(request.form.getlist("condition_ids"))
        selected_allergy_ids = set(request.form.getlist("allergy_ids"))
        selected_medication_ids = set(request.form.getlist("medication_ids"))
        valid_id, id_error, dob_from_id = validate_sa_id(raw_id_no)
        try:
            supplied_dob = date.fromisoformat(dob_text) if dob_text else dob_from_id
        except ValueError:
            supplied_dob = None

        role = "patient"
        if not full_name or not email or not cellphone:
            flash("Full name, e-mail and cellphone are required.", "error")
        elif not valid_id:
            flash(id_error or "Invalid South African ID number.", "error")
        elif supplied_dob and dob_from_id and supplied_dob != dob_from_id:
            flash("Date of birth does not match the SA ID number.", "error")
        elif User.query.filter_by(email=email).first():
            flash("That email is already registered.", "error")
        elif User.query.filter_by(sa_id_number=id_no).first():
            flash("That SA ID number is already registered.", "error")
        elif Patient.query.filter_by(id_number=id_no).first():
            flash("That SA ID number is already registered.", "error")
        else:
            generated = secrets.token_urlsafe(10) + "A1!"
            surname = full_name.rsplit(" ", 1)[1] if " " in full_name else ""
            u = User(
                email=email,
                full_name=full_name,
                surname=surname or None,
                must_change_password=True,
                phone=cellphone,
                sa_id_number=id_no,
            )
            u.set_password(generated)
            u.temp_password = generated
            db.session.add(u)
            db.session.flush()
            db.session.add(UserRole(user_id=u.id, role=role))
            patient = Patient(
                profile_id=u.id, mrn="MRN-" + u.id[:8],
                full_name=full_name, surname=surname or None, id_number=id_no,
                date_of_birth=supplied_dob, phone=cellphone, email=email,
                created_by=current_user.id,
            )
            patient.conditions = (
                Condition.query.filter(Condition.id.in_(selected_condition_ids)).all()
                if selected_condition_ids else []
            )
            patient.allergy_list = (
                Allergy.query.filter(Allergy.id.in_(selected_allergy_ids)).all()
                if selected_allergy_ids else []
            )
            patient.medications = (
                Medication.query.filter(Medication.id.in_(selected_medication_ids)).all()
                if selected_medication_ids else []
            )
            db.session.add(patient)
            notify(u.id, "Your MediLab Connect account is ready",
                   f"You have been added as a {ROLE_LABELS[role]}. Sign in and change your password.", "/app")
            log_audit(current_user.id, "doctor_create_patient", "user", u.id, {"role": role})
            db.session.commit()
            sent = send_email(
                [email],
                "Your MediLab Connect patient account",
                (
                    f"Hello {full_name},\n\n"
                    "Your MediLab Connect patient account has been created.\n\n"
                    f"Temporary password: {generated}\n\n"
                    "For security, you will be asked to choose a new password the first time you sign in.\n\n"
                    "- MediLab Connect"
                ),
            )
            whatsapp_sent = send_account_welcome_whatsapp(
                u,
                role=role,
                temporary_password=generated,
            )
            flash_message = (
                "Patient account created and temporary password e-mailed."
                if sent else
                f"Patient account created. SMTP is unavailable; temporary password: {generated}"
            )
            if whatsapp_sent:
                flash_message += " WhatsApp welcome sent."
            flash(
                flash_message,
                "success" if sent else "error",
            )
            return redirect(url_for("doctor.user_new"))
    return render_template(
        "doctor/user_new.html",
        all_conditions=Condition.query.filter(Condition.active.is_(True), Condition.deleted_at.is_(None)).order_by(Condition.category, Condition.name).all(),
        all_allergies=Allergy.query.filter(Allergy.active.is_(True), Allergy.deleted_at.is_(None)).order_by(Allergy.category, Allergy.name).all(),
        all_medications=Medication.query.filter(Medication.active.is_(True), Medication.deleted_at.is_(None)).order_by(Medication.category, Medication.name).all(),
        selected_condition_ids=selected_condition_ids,
        selected_allergy_ids=selected_allergy_ids,
        selected_medication_ids=selected_medication_ids,
    )


@bp.route("/reports")
def reports():
    from ..reports import build_report_pdf, parse_range
    from ..models import TestCatalog
    frm, to, start, end = parse_range(request.args)

    qs = TestRequest.query.filter(TestRequest.doctor_id == current_user.id,
                                  TestRequest.created_at.between(start, end))
    total = qs.count()
    by_status = (db.session.query(TestRequest.status, db.func.count(TestRequest.id))
                 .filter(TestRequest.doctor_id == current_user.id,
                         TestRequest.created_at.between(start, end))
                 .group_by(TestRequest.status).all())
    by_priority = (db.session.query(TestRequest.priority, db.func.count(TestRequest.id))
                   .filter(TestRequest.doctor_id == current_user.id,
                           TestRequest.created_at.between(start, end))
                   .group_by(TestRequest.priority).all())
    rows = qs.order_by(TestRequest.created_at.desc()).all()
    abnormal_count = sum(1 for r in rows for it in r.items if it.abnormal_flag)
    total_items = sum(len(r.items) for r in rows)
    normal_count = max(total_items - abnormal_count, 0)
    by_category = (
        db.session.query(TestCatalog.category, db.func.count(TestRequestItem.id))
        .join(TestRequestItem, TestRequestItem.test_id == TestCatalog.id)
        .join(TestRequest, TestRequest.id == TestRequestItem.request_id)
        .filter(
            TestRequest.doctor_id == current_user.id,
            TestRequest.created_at.between(start, end),
        )
        .group_by(TestCatalog.category)
        .order_by(db.func.count(TestRequestItem.id).desc())
        .all()
    )
    trend_days = []
    trend_counts = {}
    for offset in range((to - frm).days + 1):
        day = frm + timedelta(days=offset)
        trend_days.append(day)
        trend_counts[day.isoformat()] = 0
    for created_at, in (
        db.session.query(TestRequest.created_at)
        .filter(
            TestRequest.doctor_id == current_user.id,
            TestRequest.created_at.between(start, end),
        )
        .all()
    ):
        if created_at:
            key = created_at.date().isoformat()
            if key in trend_counts:
                trend_counts[key] += 1

    if request.args.get("format") == "pdf":
        detail = [[r.request_number,
                   r.patient.full_name if r.patient else "-",
                   r.priority, r.status,
                   r.created_at.strftime("%Y-%m-%d"),
                   len(r.items)] for r in rows] or [["No requests", "", "", "", "", ""]]
        sections = [
            {"heading": "Requests by status",
             "headers": ["Status", "Count"],
             "rows": [[s, n] for s, n in by_status] or [["No data", 0]]},
            {"heading": "Requests by priority",
             "headers": ["Priority", "Count"],
             "rows": [[p, n] for p, n in by_priority] or [["No data", 0]]},
            {"heading": "All requests in range",
             "headers": ["Request #", "Patient", "Priority", "Status", "Created", "Tests"],
             "rows": detail},
        ]
        buf = build_report_pdf(
            f"Doctor Report - {current_user.full_name or current_user.email}",
            subtitle=f"Range: {frm:%Y-%m-%d} \u2192 {to:%Y-%m-%d}",
            summary=[f"Total requests submitted: <b>{total}</b>",
                     f"Abnormal results in range: <b>{abnormal_count}</b>"],
            sections=sections,
        )
        return send_file(buf, mimetype="application/pdf", as_attachment=True,
                         download_name=f"doctor-report-{frm}_{to}.pdf")
    return render_template("doctor/reports.html",
                           frm=frm, to=to, total=total, rows=rows,
                           by_status=by_status, by_priority=by_priority,
                           abnormal_count=abnormal_count,
                           status_chart_labels=[(status or "-").replace("_", " ").title() for status, _count in by_status],
                           status_chart_values=[count for _status, count in by_status],
                           priority_chart_labels=[(priority or "-").title() for priority, _count in by_priority],
                           priority_chart_values=[count for _priority, count in by_priority],
                           trend_chart_labels=[day.strftime("%d %b") for day in trend_days],
                           trend_chart_values=[trend_counts[day.isoformat()] for day in trend_days],
                           abnormal_chart_labels=["Normal", "Abnormal"],
                           abnormal_chart_values=[normal_count, abnormal_count],
                           category_chart_labels=[category or "-" for category, _count in by_category],
                           category_chart_values=[count for _category, count in by_category])


@bp.route("/alerts")
def alerts():
    days = request.args.get("days", type=int)
    today = date.today()
    if days:
        start_d = today - timedelta(days=max(days - 1, 0))
        end_d = today
    else:
        try:
            start_d = date.fromisoformat(request.args.get("start_date", ""))
        except ValueError:
            start_d = today - timedelta(days=4)
        try:
            end_d = date.fromisoformat(request.args.get("end_date", ""))
        except ValueError:
            end_d = today
    if start_d > end_d:
        start_d, end_d = end_d, start_d

    start_dt = datetime.combine(start_d, datetime.min.time())
    end_dt = datetime.combine(end_d, datetime.max.time())
    alerts_ = []
    rows = (TestRequest.query
            .filter_by(doctor_id=current_user.id)
            .order_by(TestRequest.created_at.desc()).all())
    for req in rows:
        for item in req.items:
            if item.abnormal_flag not in ("low", "high"):
                continue
            when = item.captured_at or item.completed_at or req.updated_at
            if not (start_dt <= when <= end_dt):
                continue
            reference = (
                f"{item.test.reference_low} - {item.test.reference_high} {item.test.units or ''}".strip()
                if item.test.reference_low is not None and item.test.reference_high is not None
                else (item.test.reference_text or "-")
            )
            alerts_.append({
                "id": item.id,
                "request_id": req.id,
                "request_number": req.request_number,
                "patient_name": req.patient.full_name if req.patient else "-",
                "test_name": item.test.name,
                "result_value": item.result_value,
                "result_text": item.result_text,
                "reference_range": reference,
                "abnormal_flag": item.abnormal_flag,
                "priority": req.priority,
                "date": when.strftime("%Y-%m-%d"),
            })

    alerts_by_date = {}
    for alert in alerts_:
        alerts_by_date.setdefault(alert["date"], []).append(alert)

    return render_template(
        "doctor/alerts.html",
        alerts=alerts_,
        alerts_by_date=alerts_by_date,
        start_date=start_d.isoformat(),
        end_date=end_d.isoformat(),
    )


@bp.route("/requests/<request_id>/pdf")
def generate_result_pdf(request_id):
    req = db.session.get(TestRequest, request_id)
    if not req:
        abort(404)
    visible_items = None
    if req.doctor_id != current_user.id:
        shared_grants = _shared_grants_for_request(req)
        if not shared_grants:
            abort(404)
        visible_items = _shared_items_for_request(req, shared_grants)
        if not visible_items:
            abort(404)
    buf = build_request_results_pdf(req, items=visible_items)
    return send_file(buf, mimetype="application/pdf", as_attachment=True,
                     download_name=f"{req.request_number}-results.pdf")


@bp.route("/access-requests", methods=["GET", "POST"])
def access_requests():
    if request.method == "POST":
        patient_id = (request.form.get("patient_id") or "").strip()
        note = (request.form.get("note") or "").strip() or None
        p = db.session.get(Patient, patient_id) if patient_id else None
        if not p:
            flash("Please select a patient.", "error")
            return redirect(url_for("doctor.access_requests"))
        existing = (AccessRequest.query
                    .filter_by(doctor_id=current_user.id,
                               patient_id=p.id, status="pending")
                    .first())
        if existing:
            flash("You already have a pending request with this patient.", "error")
            return redirect(url_for("doctor.access_requests"))
        ar = AccessRequest(doctor_id=current_user.id, patient_id=p.id, note=note)
        db.session.add(ar)
        db.session.add(Notification(
            user_id=p.profile_id,
            title=f"Dr. {current_user.full_name or current_user.email} requested access to your test record",
            body=note or "Open the access requests page to accept or decline.",
            link=url_for("patient.access_requests"),
        ))
        log_audit(current_user.id, "access_request_send", "access_request", ar.id,
                  {"patient_id": p.id})
        db.session.commit()
        flash(f"Access request sent to {p.full_name}.", "success")
        return redirect(url_for("doctor.access_requests"))

    sent = (AccessRequest.query
            .filter_by(doctor_id=current_user.id)
            .order_by(AccessRequest.created_at.desc()).all())
    patients = Patient.query.filter(Patient.deleted_at.is_(None)).order_by(Patient.full_name).all()
    return render_template("doctor/access_requests.html",
                           sent=sent, patients=patients)


@bp.route("/profile", methods=["GET", "POST"])
def profile():
    doctor = current_user

    if request.method == "POST":
        doctor.full_name = (request.form.get("full_name") or "").strip()
        doctor.surname = (request.form.get("surname") or "").strip() or None

        gender = (request.form.get("gender") or "").strip() or None
        if gender and gender not in GENDER_OPTIONS:
            flash("Please select a valid gender.", "error")
            return redirect(url_for("doctor.profile"))
        doctor.gender = gender

        doctor.phone = (request.form.get("phone") or "").strip() or None
        sa_id = (request.form.get("sa_id_number") or "").strip() or None
        dob_text = (request.form.get("date_of_birth") or "").strip()

        dob_value = None
        if dob_text:
            try:
                dob_value = date.fromisoformat(dob_text)
            except ValueError:
                flash("Date of birth must be a valid date.", "error")
                return redirect(url_for("doctor.profile"))
                
        if sa_id and len(sa_id) == 13:
            valid_id, id_error, dob_from_id = validate_sa_id(sa_id)
            if not valid_id:
                flash(id_error or "Invalid South African ID number.", "error")
                return redirect(url_for("doctor.profile"))
            if dob_value and dob_from_id and dob_value != dob_from_id:
                flash("Date of birth does not match the SA ID number.", "error")
                return redirect(url_for("doctor.profile"))
            dob_value = dob_value or dob_from_id

        if sa_id and User.query.filter(User.sa_id_number == sa_id, User.id != doctor.id).first():
            flash("That SA ID number is already used by another account.", "error")
            return redirect(url_for("doctor.profile"))

        doctor.sa_id_number = sa_id
        doctor.date_of_birth = dob_value
        doctor.hpcsa_number = (request.form.get("hpcsa_number") or "").strip() or None

        email = (request.form.get("email") or "").lower().strip()
        existing = User.query.filter(User.email == email, User.id != doctor.id).first()
        if existing:
            flash("Email already exists.", "error")
            return redirect(url_for("doctor.profile"))
        doctor.email = email

        f = request.files.get("avatar")
        if f and f.filename:
            ext = os.path.splitext(f.filename)[1].lower()
            if ext not in ALLOWED_AVATAR_EXT:
                flash("Avatar must be PNG/JPG/GIF/WEBP.", "error")
                return redirect(url_for("doctor.profile"))

            filename = f"{doctor.id}{ext}"
            save_path = os.path.join(
                current_app.config["AVATAR_UPLOAD_DIR"],
                secure_filename(filename)
            )
            f.save(save_path)
            doctor.avatar_url = (
                url_for("static", filename=f"avatars/{filename}")
                + f"?v={uuid.uuid4().hex[:6]}"
            )

        log_audit(current_user.id, "update_profile", "user", current_user.id)
        db.session.commit()

        flash("Profile updated successfully.", "success")
        return redirect(url_for("doctor.profile"))

    return render_template(
        "doctor/profile.html",
        doctor=doctor,
        gender_options=GENDER_OPTIONS
    )
