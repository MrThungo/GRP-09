import csv
import os
import uuid
from datetime import date, datetime, time, timedelta
from io import StringIO
from flask import (
    Blueprint, render_template, abort, send_file, request,
    redirect, url_for, flash, current_app, Response,
)
from flask_login import login_required, current_user
from sqlalchemy import func, or_
from sqlalchemy.orm import selectinload
from werkzeug.utils import secure_filename

from ..extensions import db
from ..auth_utils import role_required
from ..models import (
    Patient, TestRequest, TestRequestItem, TestCatalog, Notification, PRIORITIES,
    REQUEST_STATUSES, Condition, Allergy, Medication, ConsentGrant, User,
    UserRole, AccessRequest, TITLE_OPTIONS, GENDER_OPTIONS, OnlineConsultation,
)
from ..sa_id import validate_sa_id
from ..email import send_email
from ..services import log_audit, notify

bp = Blueprint("patient", __name__, template_folder="../templates/patient")

ALLOWED_AVATAR_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
BLOOD_TYPES = ("A+", "A-", "B+", "B-", "AB+", "AB-", "O+", "O-")


@bp.before_request
@login_required
@role_required("patient")
def _gate():
    pass


def _my_patient():
    return Patient.query.filter_by(profile_id=current_user.id, deleted_at=None).first()


def _date_filter_value(name):
    raw = (request.args.get(name) or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _patient_consultation_or_404(consultation_id, room_token=None):
    p = _my_patient()
    consultation = (OnlineConsultation.query
                    .options(
                        selectinload(OnlineConsultation.request),
                        selectinload(OnlineConsultation.doctor),
                    )
                    .filter_by(id=consultation_id)
                    .first())
    if not (p and consultation and consultation.patient_id == p.id):
        abort(404)
    if room_token is not None and consultation.room_token != room_token:
        abort(404)
    return consultation


def _consultation_rows_for_patient(patient_id):
    return (OnlineConsultation.query
            .options(
                selectinload(OnlineConsultation.request),
                selectinload(OnlineConsultation.doctor),
            )
            .filter_by(patient_id=patient_id)
            .order_by(
                OnlineConsultation.scheduled_at.desc().nullslast(),
                OnlineConsultation.created_at.desc(),
            ))


def _latest_consultations_by_request(patient_id, request_ids):
    if not request_ids:
        return {}
    rows = (_consultation_rows_for_patient(patient_id)
            .filter(OnlineConsultation.request_id.in_(request_ids))
            .all())
    by_request = {}
    for row in rows:
        by_request.setdefault(row.request_id, row)
    return by_request


@bp.route("/")
def dashboard():
    p = _my_patient()
    released, pending_requests, recent_notifs = [], [], []
    counts = {"total": 0, "in_progress": 0, "released": 0}
    if p:
        status_counts = dict(
            db.session.query(TestRequest.status, func.count(TestRequest.id))
            .filter(TestRequest.patient_id == p.id)
            .group_by(TestRequest.status)
            .all()
        )
        counts["total"] = sum(status_counts.values())
        counts["released"] = status_counts.get("released", 0)
        counts["in_progress"] = sum(
            count for status, count in status_counts.items()
            if status not in ("released", "cancelled")
        )
        released = (TestRequest.query
                    .filter_by(patient_id=p.id, status="released")
                    .order_by(TestRequest.released_at.desc()).limit(5).all())
        pending_requests = (TestRequest.query
                            .filter(TestRequest.patient_id == p.id,
                                    TestRequest.status.notin_(("released", "cancelled")))
                            .order_by(TestRequest.created_at.desc()).limit(5).all())
    recent_notifs = (Notification.query.filter_by(user_id=current_user.id)
                     .order_by(Notification.created_at.desc()).limit(5).all())
    return render_template("patient/dashboard.html",
                           patient=p, recent=released, pending=pending_requests,
                           notifs=recent_notifs, counts=counts)


@bp.route("/results")
def results():
    p = _my_patient()
    rows = []
    display_items = {}
    consultations_by_request = {}
    filters = {
        "q": (request.args.get("q") or "").strip(),
        "flag": (request.args.get("flag") or "").strip().lower(),
        "released_from": (request.args.get("released_from") or "").strip(),
        "released_to": (request.args.get("released_to") or "").strip(),
    }
    released_from = _date_filter_value("released_from")
    released_to = _date_filter_value("released_to")
    if p:
        query = (TestRequest.query
                 .filter(TestRequest.patient_id == p.id,
                         TestRequest.status == "released"))
        if filters["q"]:
            like = f"%{filters['q']}%"
            query = query.filter(or_(
                TestRequest.request_number.ilike(like),
                TestRequest.items.any(TestRequestItem.test.has(or_(
                    TestCatalog.code.ilike(like),
                    TestCatalog.name.ilike(like),
                ))),
            ))
        if filters["flag"] in ("low", "high"):
            query = query.filter(TestRequest.items.any(
                TestRequestItem.abnormal_flag == filters["flag"]
            ))
        elif filters["flag"] == "abnormal":
            query = query.filter(TestRequest.items.any(
                TestRequestItem.abnormal_flag.isnot(None)
            ))
        elif filters["flag"] == "normal":
            query = query.filter(~TestRequest.items.any(
                TestRequestItem.abnormal_flag.isnot(None)
            ))
        if released_from:
            query = query.filter(TestRequest.released_at >= datetime.combine(released_from, time.min))
        if released_to:
            query = query.filter(TestRequest.released_at <= datetime.combine(released_to, time.max))
        rows = (query
                .options(selectinload(TestRequest.items).joinedload(TestRequestItem.test))
                .order_by(TestRequest.released_at.desc().nullslast(),
                          TestRequest.updated_at.desc()).all())

        q_lower = filters["q"].lower()
        for result_request in rows:
            items = list(result_request.items)
            request_number_matches = q_lower and q_lower in (result_request.request_number or "").lower()
            if q_lower and not request_number_matches:
                items = [
                    item for item in items
                    if q_lower in (item.test.code or "").lower()
                    or q_lower in (item.test.name or "").lower()
                ]
            if filters["flag"] in ("low", "high"):
                items = [item for item in items if item.abnormal_flag == filters["flag"]]
            elif filters["flag"] == "abnormal":
                items = [item for item in items if item.abnormal_flag]
            display_items[result_request.id] = items
        consultations_by_request = _latest_consultations_by_request(p.id, [r.id for r in rows])
    return render_template(
        "patient/results.html",
        patient=p,
        requests=rows,
        filters=filters,
        display_items=display_items,
        consultations_by_request=consultations_by_request,
    )


@bp.route("/consultations")
def consultations():
    p = _my_patient()
    rows = _consultation_rows_for_patient(p.id).all() if p else []
    return render_template("patient/consultations.html", consultations=rows)


@bp.route("/consultations/<consultation_id>")
def consultation_detail(consultation_id):
    consultation = _patient_consultation_or_404(consultation_id)
    return render_template("patient/consultation_detail.html", consultation=consultation)


@bp.route("/consultations/<consultation_id>/online", methods=["POST"])
def request_online_consultation(consultation_id):
    consultation = _patient_consultation_or_404(consultation_id)
    if consultation.status not in ("offered", "in_person_requested", "declined"):
        flash("This consultation choice has already moved forward.", "error")
        return redirect(url_for("patient.consultation_detail", consultation_id=consultation.id))
    consultation.status = "online_requested"
    consultation.patient_preference = "online"
    consultation.patient_response = None
    consultation.patient_responded_at = datetime.now()
    consultation.decline_reason = None
    notify(
        consultation.doctor_id,
        "Patient requested online consultation",
        f"{consultation.patient.full_name} requested an online consultation for {consultation.request.request_number}.",
        url_for("doctor.consultations"),
    )
    log_audit(current_user.id, "request_online_consultation", "online_consultation", consultation.id)
    db.session.commit()
    flash("Your online consultation request was sent to your doctor.", "success")
    return redirect(url_for("patient.consultation_detail", consultation_id=consultation.id))


@bp.route("/consultations/<consultation_id>/in-person", methods=["POST"])
def request_in_person_consultation(consultation_id):
    consultation = _patient_consultation_or_404(consultation_id)
    if consultation.status not in ("offered", "online_requested", "declined"):
        flash("This consultation choice has already moved forward.", "error")
        return redirect(url_for("patient.consultation_detail", consultation_id=consultation.id))
    consultation.status = "in_person_requested"
    consultation.patient_preference = "in_person"
    consultation.patient_response = None
    consultation.patient_responded_at = datetime.now()
    consultation.decline_reason = None
    notify(
        consultation.doctor_id,
        "Patient prefers an in-person consultation",
        f"{consultation.patient.full_name} prefers to discuss {consultation.request.request_number} in person.",
        url_for("doctor.consultations"),
    )
    log_audit(current_user.id, "request_in_person_consultation", "online_consultation", consultation.id)
    db.session.commit()
    flash("Your doctor has been told that you prefer an in-person discussion.", "success")
    return redirect(url_for("patient.consultation_detail", consultation_id=consultation.id))


@bp.route("/consultations/<consultation_id>/respond", methods=["POST"])
def respond_to_consultation(consultation_id):
    consultation = _patient_consultation_or_404(consultation_id)
    if consultation.status != "invited":
        flash("There is no active meeting time to respond to.", "error")
        return redirect(url_for("patient.consultation_detail", consultation_id=consultation.id))
    response = (request.form.get("response") or "").strip().lower()
    consultation.patient_responded_at = datetime.now()
    if response == "accepted":
        consultation.status = "accepted"
        consultation.patient_response = "accepted"
        consultation.decline_reason = None
        notify(
            consultation.doctor_id,
            "Online consultation accepted",
            f"{consultation.patient.full_name} accepted the online consultation time.",
            url_for("doctor.consultations"),
        )
        flash("Meeting accepted. You can enter the waiting room at the scheduled time.", "success")
    elif response == "declined":
        consultation.status = "declined"
        consultation.patient_response = "declined"
        consultation.decline_reason = (request.form.get("decline_reason") or "").strip() or None
        notify(
            consultation.doctor_id,
            "Online consultation declined",
            f"{consultation.patient.full_name} declined the online consultation time.",
            url_for("doctor.consultations"),
        )
        flash("Meeting time declined. Your doctor can send another invite.", "success")
    else:
        flash("Please accept or decline the meeting time.", "error")
        return redirect(url_for("patient.consultation_detail", consultation_id=consultation.id))
    log_audit(current_user.id, "respond_online_consultation", "online_consultation", consultation.id, {"response": response})
    db.session.commit()
    return redirect(url_for("patient.consultation_detail", consultation_id=consultation.id))


@bp.route("/consultations/<consultation_id>/waiting/<room_token>")
def consultation_waiting(consultation_id, room_token):
    consultation = _patient_consultation_or_404(consultation_id, room_token=room_token)
    if consultation.status == "started":
        return redirect(url_for("patient.consultation_room", consultation_id=consultation.id, room_token=consultation.room_token))
    if consultation.status != "accepted":
        flash("Accept the invite before entering the waiting room.", "error")
        return redirect(url_for("patient.consultation_detail", consultation_id=consultation.id))
    return render_template("consultations/waiting.html", consultation=consultation)


@bp.route("/consultations/<consultation_id>/room/<room_token>")
def consultation_room(consultation_id, room_token):
    consultation = _patient_consultation_or_404(consultation_id, room_token=room_token)
    if consultation.status == "accepted":
        return redirect(url_for("patient.consultation_waiting", consultation_id=consultation.id, room_token=consultation.room_token))
    if consultation.status != "started":
        flash("The online session is not open yet.", "error")
        return redirect(url_for("patient.consultation_detail", consultation_id=consultation.id))
    return render_template("consultations/room.html", consultation=consultation, role="patient")


@bp.route("/consultations/<consultation_id>/record")
def consultation_record(consultation_id):
    consultation = _patient_consultation_or_404(consultation_id)
    if not consultation.session_record_body:
        flash("No consultation record is available yet.", "error")
        return redirect(url_for("patient.consultation_detail", consultation_id=consultation.id))
    return Response(
        consultation.session_record_body,
        mimetype=consultation.session_record_mime or "text/plain",
        headers={
            "Content-Disposition": f"attachment; filename={consultation.session_record_filename or 'consultation-record.txt'}",
            "Content-Length": str(consultation.session_record_size or len(consultation.session_record_body.encode("utf-8"))),
        },
    )


@bp.route("/requests")
def requests_list():
    p = _my_patient()
    rows = []
    filters = {
        "q": (request.args.get("q") or "").strip(),
        "status": (request.args.get("status") or "").strip(),
        "priority": (request.args.get("priority") or "").strip(),
        "created_from": (request.args.get("created_from") or "").strip(),
        "created_to": (request.args.get("created_to") or "").strip(),
    }
    created_from = _date_filter_value("created_from")
    created_to = _date_filter_value("created_to")
    if p:
        query = TestRequest.query.filter_by(patient_id=p.id)
        if filters["q"]:
            query = query.filter(TestRequest.request_number.ilike(f"%{filters['q']}%"))
        if filters["status"] in REQUEST_STATUSES:
            query = query.filter(TestRequest.status == filters["status"])
        else:
            filters["status"] = ""
        if filters["priority"] in PRIORITIES:
            query = query.filter(TestRequest.priority == filters["priority"])
        else:
            filters["priority"] = ""
        if created_from:
            query = query.filter(TestRequest.created_at >= datetime.combine(created_from, time.min))
        if created_to:
            query = query.filter(TestRequest.created_at <= datetime.combine(created_to, time.max))
        rows = query.order_by(TestRequest.created_at.desc()).all()
    return render_template(
        "patient/requests.html",
        requests=rows,
        filters=filters,
        statuses=REQUEST_STATUSES,
        priorities=PRIORITIES,
    )


@bp.route("/requests/<request_id>/cancel", methods=["POST"])
def cancel_request(request_id):
    p = _my_patient()
    req = db.session.get(TestRequest, request_id)
    if not (p and req and req.patient_id == p.id):
        abort(404)
    flash("Patients cannot cancel submitted requests. Please contact your doctor or the laboratory.", "error")
    return redirect(url_for("patient.requests_list"))


@bp.route("/history.csv")
def history_csv():
    p = _my_patient()
    if not p:
        abort(404)
    out = StringIO()
    w = csv.writer(out)
    w.writerow(["request_number", "status", "priority", "created_at", "released_at",
                "test_code", "test_name", "result_value", "result_text", "units", "flag"])
    rows = (TestRequest.query
            .options(selectinload(TestRequest.items).joinedload(TestRequestItem.test))
            .filter_by(patient_id=p.id)
            .order_by(TestRequest.created_at.desc()).all())
    for r in rows:
        if not r.items:
            w.writerow([r.request_number, r.status, r.priority,
                        r.created_at.isoformat(),
                        r.released_at.isoformat() if r.released_at else "",
                        "", "", "", "", "", ""])
        for it in r.items:
            can_view_results = r.status == "released"
            w.writerow([
                r.request_number, r.status, r.priority,
                r.created_at.isoformat(),
                r.released_at.isoformat() if r.released_at else "",
                it.test.code, it.test.name,
                it.result_value if can_view_results and it.result_value is not None else "",
                it.result_text if can_view_results else "",
                it.test.units or "",
                it.abnormal_flag if can_view_results else "",
            ])
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment;filename=my-lab-history.csv"})


@bp.route("/notifications")
def notifications():
    rows = (Notification.query.filter_by(user_id=current_user.id)
            .order_by(Notification.created_at.desc()).limit(100).all())
    return render_template("patient/notifications.html", rows=rows)


@bp.route("/notifications/mark-all-read", methods=["POST"])
def mark_all_read():
    Notification.query.filter_by(user_id=current_user.id, read=False).update({"read": True})
    db.session.commit()
    flash("All notifications marked as read.", "success")
    return redirect(url_for("patient.notifications"))


@bp.route("/book", methods=["GET", "POST"])
def book():
    flash("Laboratory test requests must be created by a doctor who records the collected samples.", "error")
    return redirect(url_for("patient.dashboard"))


@bp.route("/profile", methods=["GET", "POST"])
def profile():
    p = _my_patient()
    if not p:
        abort(404)
    if request.method == "POST":
        title = (request.form.get("title") or "").strip() or None
        gender = (request.form.get("gender") or "").strip() or None
        id_number = "".join(ch for ch in (request.form.get("id_number") or "").strip() if ch.isdigit()) or None
        dob_text = (request.form.get("date_of_birth") or "").strip()
        dob_value = None
        if dob_text:
            try:
                dob_value = date.fromisoformat(dob_text)
            except ValueError:
                flash("Date of birth is invalid.", "error")
                return redirect(url_for("patient.profile"))
        if id_number:
            valid_id, id_error, dob_from_id = validate_sa_id(id_number)
            if not valid_id:
                flash(id_error or "Invalid South African ID number.", "error")
                return redirect(url_for("patient.profile"))
            duplicate_patient = (
                Patient.query
                .filter(Patient.id_number == id_number, Patient.id != p.id)
                .first()
            )
            duplicate_user = (
                User.query
                .filter(User.sa_id_number == id_number, User.id != current_user.id)
                .first()
            )
            if duplicate_patient or duplicate_user:
                flash("That ID number is already linked to another profile.", "error")
                return redirect(url_for("patient.profile"))
            if dob_value and dob_value != dob_from_id:
                flash("Date of birth does not match the ID number.", "error")
                return redirect(url_for("patient.profile"))
            dob_value = dob_from_id
        if title and title not in TITLE_OPTIONS:
            flash("Please select a valid title.", "error")
            return redirect(url_for("patient.profile"))
        if gender and gender not in GENDER_OPTIONS:
            flash("Please select a valid gender.", "error")
            return redirect(url_for("patient.profile"))

        current_user.title = title
        current_user.gender = gender
        p.id_number = id_number
        current_user.sa_id_number = id_number
        p.phone = request.form.get("phone") or None
        p.address = request.form.get("address") or None
        p.gender = gender
        blood_type = (request.form.get("blood_type") or "").strip().upper() or None
        if blood_type and blood_type not in BLOOD_TYPES:
            flash("Please select a valid blood type.", "error")
            return redirect(url_for("patient.profile"))
        p.blood_type = blood_type
        # M:N selection (admin-managed catalogues)
        cond_ids = set(request.form.getlist("condition_ids"))
        all_ids  = set(request.form.getlist("allergy_ids"))
        med_ids  = set(request.form.getlist("medication_ids"))
        p.conditions   = Condition.query.filter(Condition.id.in_(cond_ids)).all() if cond_ids else []
        p.allergy_list = Allergy.query.filter(Allergy.id.in_(all_ids)).all() if all_ids else []
        p.medications  = Medication.query.filter(Medication.id.in_(med_ids)).all() if med_ids else []
        # Preserve older free-text notes when the dropdown-only profile form is submitted.
        if "chronic_conditions" in request.form:
            p.chronic_conditions = (request.form.get("chronic_conditions") or "").strip() or None
        if "allergies" in request.form:
            p.allergies = (request.form.get("allergies") or "").strip() or None
        if "current_medication" in request.form:
            p.current_medication = (request.form.get("current_medication") or "").strip() or None
        p.date_of_birth = dob_value
        # Avatar upload (optional)
        f = request.files.get("avatar")
        if f and f.filename:
            ext = os.path.splitext(f.filename)[1].lower()
            if ext not in ALLOWED_AVATAR_EXT:
                flash("Avatar must be PNG/JPG/GIF/WEBP.", "error")
            else:
                fname = f"{current_user.id}{ext}"
                f.save(os.path.join(current_app.config["AVATAR_UPLOAD_DIR"],
                                    secure_filename(fname)))
                current_user.avatar_url = url_for("static", filename=f"avatars/{fname}") + f"?v={uuid.uuid4().hex[:6]}"
        db.session.commit()
        flash("Profile updated.", "success")
        return redirect(url_for("patient.profile"))
    return render_template(
        "patient/profile.html",
        patient=p,
        all_conditions=Condition.query.filter(Condition.active.is_(True), Condition.deleted_at.is_(None)).order_by(Condition.category, Condition.name).all(),
        all_allergies=Allergy.query.filter(Allergy.active.is_(True), Allergy.deleted_at.is_(None)).order_by(Allergy.category, Allergy.name).all(),
        all_medications=Medication.query.filter(Medication.active.is_(True), Medication.deleted_at.is_(None)).order_by(Medication.category, Medication.name).all(),
        blood_types=BLOOD_TYPES,
        title_options=TITLE_OPTIONS,
        gender_options=GENDER_OPTIONS,
        my_condition_ids={c.id for c in p.conditions},
        my_allergy_ids={a.id for a in p.allergy_list},
        my_medication_ids={m.id for m in p.medications},
    )


@bp.route("/deactivate", methods=["POST"])
def deactivate():
    """Soft-delete: user marks their own account inactive. An admin must reactivate it."""
    from datetime import datetime
    from flask_login import logout_user
    confirm = (request.form.get("confirm") or "").strip().lower()
    if confirm != "deactivate":
        flash("Type 'deactivate' to confirm.", "error")
        return redirect(url_for("patient.profile"))
    current_user.is_deactivated = True
    current_user.deactivated_at = datetime.now()
    log_audit(current_user.id, "self_deactivate", "user", current_user.id)
    db.session.commit()
    response = redirect(url_for("public.landing"))
    logout_user()
    flash("Your account has been deactivated. Contact an administrator to reactivate it.", "success")
    return response


def _dt(value):
    return value.strftime("%Y-%m-%d %H:%M") if value else "-"


def _patient_detail_rows(patient):
    return [
        ["Full name", patient.full_name or "-"],
        ["MRN", patient.mrn or "-"],
        ["ID number", patient.id_number or current_user.sa_id_number or "-"],
        ["Date of birth", patient.date_of_birth.strftime("%Y-%m-%d") if patient.date_of_birth else "-"],
        ["Blood type", patient.blood_type or "-"],
        ["Contact", " / ".join(part for part in [patient.email, patient.phone] if part) or "-"],
    ]


def _render_request_pdf(req, include_results=True):
    from reportlab.lib.units import mm
    from ..reports import build_report_pdf, format_reference, format_result_value

    title = (
        f"Lab Results - {req.request_number}"
        if include_results
        else f"Lab Request - {req.request_number}"
    )
    summary = [
        f"Request number: <b>{req.request_number}</b>",
        f"Status: <b>{req.status.replace('_', ' ').title()}</b>",
        f"Priority: <b>{req.priority.title()}</b>",
        f"Created: <b>{_dt(req.created_at)}</b>",
    ]
    if req.released_at:
        summary.append(f"Released: <b>{_dt(req.released_at)}</b>")
    if req.doctor:
        summary.append(f"Requesting doctor: <b>{req.doctor.full_name or req.doctor.email}</b>")

    rows = []
    for item in req.items:
        if include_results:
            rows.append([
                f"{item.test.code} {item.test.name}",
                format_result_value(item),
                item.test.units or "-",
                format_reference(item.test),
                (item.abnormal_flag or "Normal").upper(),
                item.status.replace("_", " ").title(),
            ])
        else:
            rows.append([
                f"{item.test.code} {item.test.name}",
                item.test.sample_type or "-",
                item.status.replace("_", " ").title(),
            ])

    sections = [
        {
            "heading": "Patient details",
            "headers": ["Field", "Details"],
            "rows": _patient_detail_rows(req.patient),
            "col_widths": [42 * mm, 138 * mm],
        },
        {
            "heading": "Test results" if include_results else "Requested tests",
            "headers": ["Test", "Result", "Units", "Reference", "Flag", "Status"] if include_results else ["Test", "Sample type", "Status"],
            "rows": rows or [["No tests", "", "", "", "", ""]],
            "col_widths": [45 * mm, 28 * mm, 20 * mm, 38 * mm, 18 * mm, 31 * mm] if include_results else [90 * mm, 50 * mm, 40 * mm],
        },
    ]
    if req.clinical_notes:
        sections.append({
            "heading": "Clinical notes",
            "headers": ["Notes"],
            "rows": [[req.clinical_notes]],
            "col_widths": [180 * mm],
        })
    if req.release_note and include_results:
        sections.append({
            "heading": "Doctor note",
            "headers": ["Note"],
            "rows": [[req.release_note]],
            "col_widths": [180 * mm],
        })

    return build_report_pdf(
        title,
        subtitle="Nelson Mandela Bay Haematology Diagnostic Laboratories",
        summary=summary,
        sections=sections,
    )


@bp.route("/results/<request_id>/pdf")
def result_pdf(request_id):
    p = _my_patient()
    req = (
        TestRequest.query
        .options(
            selectinload(TestRequest.patient),
            selectinload(TestRequest.doctor),
            selectinload(TestRequest.items).joinedload(TestRequestItem.test),
        )
        .filter_by(id=request_id)
        .first()
    )
    if not (p and req and req.patient_id == p.id and req.status == "released"):
        abort(404)
    buf = _render_request_pdf(req, include_results=True)
    return send_file(buf, mimetype="application/pdf",
                     as_attachment=True, download_name=f"{req.request_number}-results.pdf")


@bp.route("/requests/<request_id>/pdf")
def request_pdf(request_id):
    """Export any of the patient's own requests (booking confirmation or results) as PDF."""
    p = _my_patient()
    req = (
        TestRequest.query
        .options(
            selectinload(TestRequest.patient),
            selectinload(TestRequest.doctor),
            selectinload(TestRequest.items).joinedload(TestRequestItem.test),
        )
        .filter_by(id=request_id)
        .first()
    )
    if not (p and req and req.patient_id == p.id):
        abort(404)
    buf = _render_request_pdf(req, include_results=req.status == "released")
    return send_file(buf, mimetype="application/pdf",
                     as_attachment=True, download_name=f"{req.request_number}.pdf")



# ---------- Patient reports + chart data ----------
@bp.route("/reports")
def reports():
    from flask import send_file
    from sqlalchemy import func
    from reportlab.lib.units import mm
    from ..reports import build_report_pdf, format_reference, format_result_value, parse_range
    p = _my_patient()
    frm, to, start, end = parse_range(request.args)
    rows = []
    by_category = []
    report_results = []
    total_tests = 0
    abnormal = 0
    normal = 0
    latest_release = None
    low = 0
    high = 0
    release_counts = {}
    if p:
        report_date = func.coalesce(TestRequest.released_at, TestRequest.updated_at, TestRequest.created_at)
        result_rows = (
            db.session.query(TestRequestItem, TestRequest, TestCatalog)
            .join(TestRequest, TestRequest.id == TestRequestItem.request_id)
            .join(Patient, Patient.id == TestRequest.patient_id)
            .join(TestCatalog, TestCatalog.id == TestRequestItem.test_id)
            .filter(
                Patient.id == p.id,
                Patient.profile_id == current_user.id,
                Patient.deleted_at.is_(None),
                TestRequest.status == "released",
                report_date.between(start, end),
            )
            .order_by(
                TestCatalog.category.asc(),
                TestCatalog.name.asc(),
                report_date.desc(),
                TestRequest.request_number.asc(),
            )
            .all()
        )

        category_counts = {}
        requests_by_id = {}
        for item, req, test in result_rows:
            released_on = req.released_at or req.updated_at or req.created_at
            release_key = released_on.date().isoformat()
            release_counts[release_key] = release_counts.get(release_key, 0) + 1
            requests_by_id.setdefault(req.id, req)
            category = test.category or "Uncategorised"
            category_counts[category] = category_counts.get(category, 0) + 1
            flag = (item.abnormal_flag or "").lower()
            if flag == "low":
                low += 1
            elif flag == "high":
                high += 1
            report_results.append({
                "category": category,
                "request_id": req.id,
                "request_number": req.request_number,
                "released": released_on,
                "test": f"{test.code} {test.name}",
                "result": format_result_value(item),
                "units": test.units or "-",
                "reference": format_reference(test),
                "flag": (item.abnormal_flag or "Normal").upper(),
                "abnormal": bool(item.abnormal_flag),
            })

        by_category = list(category_counts.items())
        rows = sorted(
            requests_by_id.values(),
            key=lambda r: r.released_at or r.updated_at or r.created_at,
            reverse=True,
        )
        total_tests = len(result_rows)
        abnormal = sum(1 for item, _req, _test in result_rows if item.abnormal_flag)
        normal = max(total_tests - abnormal, 0)
        latest_release = max(
            (r.released_at or r.updated_at or r.created_at for r in rows),
            default=None,
        )

    trend_days = []
    trend_counts = {}
    for offset in range((to - frm).days + 1):
        day = frm + timedelta(days=offset)
        trend_days.append(day)
        trend_counts[day.isoformat()] = release_counts.get(day.isoformat(), 0)

    if request.args.get("format") == "pdf":
        patient_name = p.full_name if p else (current_user.full_name or current_user.email)
        result_table_rows = [[
            result["category"],
            _dt(result["released"]),
            result["request_number"],
            result["test"],
            result["result"],
            result["units"],
            result["reference"],
            result["flag"] if result["abnormal"] else "-",
        ] for result in report_results] or [["No released results in this date range", "", "", "", "", "", "", ""]]

        sections = [{
            "heading": "All test results grouped by category",
            "headers": ["Category", "Released", "Request #", "Test", "Result", "Units", "Reference", "Flag"],
            "rows": result_table_rows,
            "col_widths": [24 * mm, 22 * mm, 32 * mm, 42 * mm, 17 * mm, 12 * mm, 19 * mm, 12 * mm],
        }]
        buf = build_report_pdf(
            f"Patient Lab History - {patient_name}",
            subtitle=f"Released range: {frm:%Y-%m-%d} to {to:%Y-%m-%d}",
            summary=[f"Patient: <b>{patient_name}</b>",
                     f"MRN: <b>{p.mrn if p else '-'}</b>",
                     f"Released requests: <b>{len(rows)}</b>",
                     f"Total tests: <b>{total_tests}</b>",
                     f"Test categories: <b>{len(by_category)}</b>",
                     f"Abnormal results: <b>{abnormal}</b>",
                     f"Normal results: <b>{normal}</b>"],
            sections=sections,
        )
        return send_file(buf, mimetype="application/pdf", as_attachment=True,
                         download_name=f"my-lab-report-{frm}_{to}.pdf")
    return render_template("patient/reports.html",
                           frm=frm, to=to, rows=rows,
                           by_category=by_category, abnormal=abnormal,
                           normal=normal, total_tests=total_tests,
                           latest_release=latest_release,
                           report_results=report_results,
                           category_chart_labels=[category for category, _count in by_category],
                           category_chart_values=[count for _category, count in by_category],
                           flag_chart_labels=["Normal", "Low", "High"],
                           flag_chart_values=[normal, low, high],
                           release_chart_labels=[day.strftime("%d %b") for day in trend_days],
                           release_chart_values=[trend_counts[day.isoformat()] for day in trend_days],
                           request_chart_labels=["Released requests", "Tests reported"],
                           request_chart_values=[len(rows), total_tests])


@bp.route("/dashboard-charts.json")
def dashboard_charts():
    """Aggregated chart data for the patient dashboard (Chart.js)."""
    from collections import defaultdict
    from flask import jsonify
    p = _my_patient()
    if not p:
        return jsonify({"trends": [], "abnormal": {}, "timeline": [], "by_category": {}})

    items = (db.session.query(TestRequestItem, TestRequest, TestCatalog)
             .join(TestRequest, TestRequest.id == TestRequestItem.request_id)
             .join(TestCatalog, TestCatalog.id == TestRequestItem.test_id)
             .filter(TestRequest.patient_id == p.id,
                     TestRequest.status == "released")
             .order_by(TestRequest.created_at.asc()).all())

    # Trends per test code (only numeric results)
    trends_map = defaultdict(lambda: {"label": "", "units": "", "low": None, "high": None,
                                      "points": []})
    abnormal_breakdown = {"normal": 0, "low": 0, "high": 0}
    by_category = defaultdict(int)

    for it, req, t in items:
        by_category[t.category or "Other"] += 1
        flag = (it.abnormal_flag or "").lower()
        if flag in ("low", "high"):
            abnormal_breakdown[flag] += 1
        elif it.result_value is not None or it.result_text:
            abnormal_breakdown["normal"] += 1
        if it.result_value is not None:
            d = trends_map[t.code]
            d["label"] = f"{t.code} - {t.name}"
            d["units"] = t.units or ""
            d["low"] = float(t.reference_low) if t.reference_low is not None else None
            d["high"] = float(t.reference_high) if t.reference_high is not None else None
            d["points"].append({
                "x": (it.captured_at or req.created_at).strftime("%Y-%m-%d"),
                "y": float(it.result_value),
                "flag": flag or None,
            })

    # Status timeline: count of requests created per month for the last 12 months
    from datetime import date
    today = date.today()
    months = []
    for i in range(11, -1, -1):
        y = today.year
        m = today.month - i
        while m <= 0:
            m += 12; y -= 1
        months.append(f"{y:04d}-{m:02d}")
    status_set = ["submitted", "samples_received", "in_progress",
                  "completed", "verified", "released", "cancelled"]
    timeline = {s: {m: 0 for m in months} for s in status_set}
    reqs = (
        TestRequest.query
        .with_entities(TestRequest.status, TestRequest.created_at)
        .filter(TestRequest.patient_id == p.id)
        .all()
    )
    for r in reqs:
        key = r.created_at.strftime("%Y-%m")
        if key in timeline.get(r.status, {}):
            timeline[r.status][key] += 1

    return {
        "trends": [{"code": code, **d} for code, d in trends_map.items() if len(d["points"]) >= 1],
        "abnormal": abnormal_breakdown,
        "by_category": dict(by_category),
        "timeline": {
            "months": months,
            "series": [{"status": s, "data": [timeline[s][m] for m in months]} for s in status_set],
        },
    }


# ---------------------------------------------------------------------------
# Consent management - patient grants/revokes doctor access to selected requests
# ---------------------------------------------------------------------------

def _doctor_ids():
    return [uid for (uid,) in db.session.query(UserRole.user_id).filter_by(role="doctor").all()]


def _requests_from_items(items):
    requests_by_id = {}
    for item in items:
        if item.request and item.request_id not in requests_by_id:
            requests_by_id[item.request_id] = item.request
    return list(requests_by_id.values())


def _selected_patient_items(patient_id, item_ids):
    item_ids = [item_id for item_id in item_ids if item_id]
    if not item_ids:
        return []
    return (
        TestRequestItem.query
        .join(TestRequest, TestRequest.id == TestRequestItem.request_id)
        .filter(
            TestRequest.patient_id == patient_id,
            TestRequestItem.id.in_(item_ids),
        )
        .order_by(TestRequest.created_at.desc(), TestRequestItem.created_at.asc())
        .all()
    )


def _send_consent_email(doctor: User, patient: Patient, requests, note: str, items=None):
    """Best-effort email; logs and continues if SMTP is not configured."""
    items_by_request = {}
    for item in items or []:
        items_by_request.setdefault(item.request_id, []).append(item)
    lines = [
        f"Hello Dr. {doctor.full_name or doctor.email},",
        "",
        f"{patient.full_name} (MRN {patient.mrn}) has granted you access to selected test results in the NMB-HLab portal.",
    ]
    if requests:
        lines += ["", "Requests included:"]
        for r in requests:
            lines.append(f"  - {r.request_number} ({r.created_at:%Y-%m-%d}, status: {r.status})")
            selected_items = items_by_request.get(r.id)
            if selected_items:
                for item in selected_items:
                    lines.append(f"      * {item.test.code} {item.test.name}")
            elif r.items:
                lines.append("      * All tests in this request")
    else:
        lines.append("\nNo test requests were included.")
    if note:
        lines += ["", f"Note from patient: {note}"]
    lines += [
        "",
        "Please sign in to review the shared records. Access is limited to the results selected by the patient.",
        "",
        "- NMB-HLab",
    ]
    sent = send_email(
        [doctor.email],
        f"NMB-HLab - {patient.full_name} granted you access",
        "\n".join(lines),
    )
    if not sent:
        current_app.logger.warning("Consent email could not be sent to %s", doctor.email)


@bp.route("/consent", methods=["GET"])
def consent():
    p = _my_patient()
    if not p:
        abort(404)
    grants = (ConsentGrant.query
              .filter_by(patient_id=p.id)
              .order_by(ConsentGrant.granted_at.desc()).all())
    doctors = (User.query
               .join(UserRole, UserRole.user_id == User.id)
               .filter(UserRole.role == "doctor")
               .order_by(User.full_name).all())
    requests_ = (TestRequest.query
                 .filter_by(patient_id=p.id)
                 .order_by(TestRequest.created_at.desc()).all())
    return render_template("patient/consent.html",
                           grants=grants, doctors=doctors, requests=requests_)


@bp.route("/consent/grant", methods=["POST"])
def consent_grant():
    p = _my_patient()
    if not p:
        abort(404)
    doctor_id = request.form.get("doctor_id")
    note = (request.form.get("note") or "").strip() or None
    item_ids = request.form.getlist("item_ids")
    doctor = db.session.get(User, doctor_id) if doctor_id else None
    if not doctor or not any(r.role == "doctor" for r in doctor.user_roles):
        flash("Please select a valid doctor.", "error")
        return redirect(url_for("patient.consent"))
    selected_items = _selected_patient_items(p.id, item_ids)
    if not selected_items:
        flash("Select at least one test result to share, or use Select all for a request.", "error")
        return redirect(url_for("patient.consent"))
    selected = _requests_from_items(selected_items)
    grant = ConsentGrant(patient_id=p.id, doctor_id=doctor.id, note=note)
    grant.requests = selected
    grant.request_items = selected_items
    db.session.add(grant)
    db.session.flush()
    log_audit(current_user.id, "consent_grant", "consent_grant", grant.id,
              {
                  "doctor_id": doctor.id,
                  "request_ids": [r.id for r in selected],
                  "item_ids": [item.id for item in selected_items],
              })
    # In-app notification + email
    db.session.add(Notification(
        user_id=doctor.id,
        title=f"{p.full_name} granted you access to test results",
        body=f"{len(selected_items)} test result(s) across {len(selected)} request(s) shared.",
        link=url_for("doctor.dashboard"),
    ))
    db.session.commit()
    _send_consent_email(doctor, p, selected, note or "", selected_items)
    flash(f"Access granted to Dr. {doctor.full_name or doctor.email}.", "success")
    return redirect(url_for("patient.consent"))


@bp.route("/consent/<grant_id>/revoke", methods=["POST"])
def consent_revoke(grant_id):
    from datetime import datetime
    p = _my_patient()
    g = db.session.get(ConsentGrant, grant_id)
    if not (p and g and g.patient_id == p.id):
        abort(404)
    if g.revoked_at:
        flash("Consent already revoked.", "error")
    else:
        g.revoked_at = datetime.now()
        log_audit(current_user.id, "consent_revoke", "consent_grant", g.id)
        db.session.commit()
        flash("Access revoked.", "success")
    return redirect(url_for("patient.consent"))


# ---------------------------------------------------------------------------
# Doctor-initiated access requests - patient inbox
# ---------------------------------------------------------------------------
@bp.route("/access-requests")
def access_requests():
    from datetime import datetime  # noqa
    p = _my_patient()
    if not p:
        abort(404)
    pending = (AccessRequest.query
               .filter_by(patient_id=p.id, status="pending")
               .order_by(AccessRequest.created_at.desc()).all())
    history = (AccessRequest.query
               .filter(AccessRequest.patient_id == p.id,
                       AccessRequest.status != "pending")
               .order_by(AccessRequest.created_at.desc()).all())
    requests_ = (TestRequest.query
                 .filter_by(patient_id=p.id)
                 .order_by(TestRequest.created_at.desc()).all())
    return render_template("patient/access_requests.html",
                           pending=pending, history=history, requests=requests_)


@bp.route("/access-requests/<req_id>/accept", methods=["POST"])
def access_request_accept(req_id):
    from datetime import datetime
    p = _my_patient()
    ar = db.session.get(AccessRequest, req_id)
    if not (p and ar and ar.patient_id == p.id):
        abort(404)
    if ar.status != "pending":
        flash("This request has already been answered.", "error")
        return redirect(url_for("patient.access_requests"))
    selected_items = _selected_patient_items(p.id, request.form.getlist("item_ids"))
    if not selected_items:
        flash("Select at least one test result to share, or use Select all for a request.", "error")
        return redirect(url_for("patient.access_requests"))
    selected = _requests_from_items(selected_items)
    grant = ConsentGrant(patient_id=p.id, doctor_id=ar.doctor_id,
                         note=f"Auto-granted via access request {ar.id}")
    grant.requests = selected
    grant.request_items = selected_items
    db.session.add(grant)
    db.session.flush()
    ar.status = "accepted"
    ar.responded_at = datetime.now()
    ar.grant_id = grant.id
    db.session.add(Notification(
        user_id=ar.doctor_id,
        title=f"{p.full_name} accepted your access request",
        body=f"You can now view {len(selected_items)} test result(s) across {len(selected)} request(s).",
        link=url_for("doctor.shared_requests"),
    ))
    log_audit(
        current_user.id,
        "access_request_accept",
        "access_request",
        ar.id,
        {"request_ids": [r.id for r in selected], "item_ids": [item.id for item in selected_items]},
    )
    db.session.commit()
    flash("Access granted to the doctor.", "success")
    return redirect(url_for("patient.access_requests"))


@bp.route("/access-requests/<req_id>/decline", methods=["POST"])
def access_request_decline(req_id):
    from datetime import datetime
    p = _my_patient()
    ar = db.session.get(AccessRequest, req_id)
    if not (p and ar and ar.patient_id == p.id):
        abort(404)
    if ar.status != "pending":
        flash("This request has already been answered.", "error")
        return redirect(url_for("patient.access_requests"))
    ar.status = "declined"
    ar.responded_at = datetime.now()
    db.session.add(Notification(
        user_id=ar.doctor_id,
        title=f"{p.full_name} declined your access request",
        body="The patient declined to share their test record.",
        link=url_for("doctor.access_requests"),
    ))
    log_audit(current_user.id, "access_request_decline", "access_request", ar.id)
    db.session.commit()
    flash("Request declined.", "success")
    return redirect(url_for("patient.access_requests"))
