from datetime import date, datetime, timedelta

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from sqlalchemy import func, or_
from sqlalchemy.orm import joinedload, selectinload

from ..auth_utils import role_required
from ..extensions import db
from ..models import (
    StockMovement,
    PRIORITIES,
    Sample,
    TechnicianTest,
    TestCatalog,
    TestRequest,
    TestRequestItem,
    TestResultReview,
    TITLE_OPTIONS,
    GENDER_OPTIONS,
    User,
)
from ..notification_pages import clear_user_notifications, mark_user_notifications_read, render_user_notifications
from ..sa_id import validate_sa_id
from ..services import log_audit, notify, return_item_for_review, send_email, verify_item

bp = Blueprint("technician", __name__, template_folder="../templates/technician")


@bp.before_request
@login_required
@role_required("lab_technician")
def _gate():
    pass


@bp.route("/notifications")
def notifications():
    return render_user_notifications("technician.mark_all_read", "technician.clear_all_notifications")


@bp.route("/notifications/mark-all-read", methods=["POST"])
def mark_all_read():
    return mark_user_notifications_read("technician.notifications")


@bp.route("/notifications/clear-all", methods=["POST"])
def clear_all_notifications():
    return clear_user_notifications("technician.notifications")


def _staff_dob_from_form():
    raw = (request.form.get("date_of_birth") or "").strip()
    sa_id = (request.form.get("sa_id_number") or "").strip()
    dob = None
    if raw:
        try:
            dob = date.fromisoformat(raw)
        except ValueError:
            return None, "Date of birth must be a valid date."
    if sa_id and len(sa_id) == 13:
        valid, id_error, dob_from_id = validate_sa_id(sa_id)
        if not valid:
            return None, id_error or "Invalid South African ID number."
        if dob and dob_from_id and dob != dob_from_id:
            return None, "Date of birth does not match the SA ID number."
        dob = dob or dob_from_id
    return dob, None


@bp.route("/profile", methods=["GET", "POST"])
def profile():
    technician = current_user
    if request.method == "POST":
        email = (request.form.get("email") or "").lower().strip()
        title = (request.form.get("title") or "").strip() or None
        gender = (request.form.get("gender") or "").strip() or None
        sa_id = (request.form.get("sa_id_number") or "").strip() or None
        dob, dob_error = _staff_dob_from_form()
        if dob_error:
            flash(dob_error, "error")
            return redirect(url_for("technician.profile"))
        if not email:
            flash("Email is required.", "error")
            return redirect(url_for("technician.profile"))
        if title and title not in TITLE_OPTIONS:
            flash("Please select a valid title.", "error")
            return redirect(url_for("technician.profile"))
        if gender and gender not in GENDER_OPTIONS:
            flash("Please select a valid gender.", "error")
            return redirect(url_for("technician.profile"))
        if User.query.filter(User.email == email, User.id != technician.id).first():
            flash("That email is already used by another account.", "error")
            return redirect(url_for("technician.profile"))
        if sa_id and User.query.filter(User.sa_id_number == sa_id, User.id != technician.id).first():
            flash("That SA ID number is already used by another account.", "error")
            return redirect(url_for("technician.profile"))

        technician.title = title
        technician.full_name = (request.form.get("full_name") or "").strip()
        technician.surname = (request.form.get("surname") or "").strip() or None
        technician.gender = gender
        technician.email = email
        technician.phone = (request.form.get("phone") or "").strip() or None
        technician.sa_id_number = sa_id
        technician.date_of_birth = dob
        log_audit(current_user.id, "update_technician_profile", "user", technician.id)
        db.session.commit()
        flash("Profile updated successfully.", "success")
        return redirect(url_for("technician.profile"))

    return render_template(
        "technician/profile.html",
        technician=technician,
        title_options=TITLE_OPTIONS,
        gender_options=GENDER_OPTIONS,
    )


def _assigned_test_ids():
    return {
        test_id for (test_id,) in (
            db.session.query(TechnicianTest.test_id)
            .join(TestCatalog, TestCatalog.id == TechnicianTest.test_id)
            .filter(
                TechnicianTest.technician_id == current_user.id,
                TestCatalog.active.is_(True),
                TestCatalog.deleted_at.is_(None),
            )
            .all()
        )
    }

def _all_samples_received(req):
    active_samples = [sample for sample in req.samples if sample.status != "rejected"]
    return bool(active_samples) and all(sample.status == "received" for sample in active_samples)

def _can_perform(test_id):
    return test_id in _assigned_test_ids()


def _due_at(item):
    start = item.started_at or item.request.created_at
    return start + timedelta(minutes=max(item.test.turnaround_minutes, 1))

def _required_sample_received(item):
    required_type = (item.test.sample_type or "").strip().casefold()
    if not item.request.samples:
        return False
    if not required_type:
        return _all_samples_received(item.request)
    matching_samples = [
        sample for sample in item.request.samples
        if (sample.sample_type or "").strip().casefold() == required_type
    ]
    return bool(matching_samples) and any(
        sample.status == "received" for sample in matching_samples
    )

def _queue_for_item(item, technician_id):
    if item.status == "to_be_reviewed" and item.assigned_to == technician_id:
        return "review"
    if item.status == "in_progress" and item.assigned_to == technician_id:
        return "selected"
    if item.status == "completed":
        if item.captured_by == technician_id:
            return "selected"
        return "verification"
    if (
        item.status == "submitted"
        and not item.assigned_to
        and item.request.status == "submitted"
    ):
        return "receipt"
    if (
        item.status == "submitted"
        and not item.assigned_to
        and item.request.status in ("samples_received", "in_progress")
        and _required_sample_received(item)
    ):
        return "waiting"
    return None


def _technician_can_access_request(req):
    return bool(req and any(_can_perform(item.test_id) for item in req.items))


def _refresh_request_after_sample_receipt(req):
    if req.status == "submitted" and _all_samples_received(req):
        req.status = "samples_received"


def _active_request_items(req):
    return [item for item in req.items if item.status != "cancelled"]


def _items_affected_by_sample(sample):
    sample_type = (sample.sample_type or "").strip().casefold()
    if not sample_type:
        return []
    return [
        item for item in sample.request.items
        if item.status not in ("cancelled", "completed", "verified")
        and (item.test.sample_type or "").strip().casefold() == sample_type
    ]


def _reject_request_item(item, reason):
    item.status = "cancelled"
    item.result_value = None
    item.result_text = None
    item.result_notes = None
    item.abnormal_flag = None
    item.near_limit_reminded_at = None
    item.review_notes = f"Rejected by laboratory: {reason}"
    db.session.add(TestResultReview(
        item_id=item.id,
        reviewer_id=current_user.id,
        action="rejected",
        note=reason,
    ))


def _refresh_request_after_item_change(req, cancel_reason=None):
    active_items = _active_request_items(req)
    if not active_items:
        req.status = "cancelled"
        req.cancel_reason = cancel_reason or "All tests on this request were rejected by the laboratory."
        req.cancelled_by = current_user.id
        req.cancelled_at = datetime.now()
        return True
    if req.status in ("cancelled", "released"):
        return req.status == "cancelled"
    if all(item.status in ("completed", "verified") for item in active_items):
        req.status = "completed"
    elif any(item.status in ("in_progress", "completed", "verified", "to_be_reviewed") for item in active_items):
        req.status = "in_progress"
    elif all(_required_sample_received(item) for item in active_items):
        req.status = "samples_received"
    else:
        req.status = "submitted"
    return False


def _notify_doctor_of_sample_issue(req, reason, affected_items=None, cancelled_request=False):
    if not req.doctor_id:
        return
    affected_items = affected_items or []
    test_names = ", ".join(item.test.name for item in affected_items if item.test)
    affected_text = test_names or "the affected sample"
    if cancelled_request:
        notice_body = (
            f"Request {req.request_number} was cancelled because all active tests "
            f"were rejected by the laboratory: {reason}"
        )
        email_body = (
            f"Hello Dr. {req.doctor.full_name or req.doctor.email},\n\n"
            f"Laboratory request {req.request_number} has been cancelled because all active tests were rejected.\n\n"
            f"Affected: {affected_text}\n"
            f"Reason: {reason}\n\n"
            "Please review the request and arrange recollection if required.\n\n"
            "- NMB-HLab"
        )
    else:
        notice_body = (
            f"{affected_text} on request {req.request_number} was rejected by the laboratory: {reason}. "
            "Other tests on the request remain active."
        )
        email_body = (
            f"Hello Dr. {req.doctor.full_name or req.doctor.email},\n\n"
            f"A laboratory issue was recorded on request {req.request_number}.\n\n"
            f"Affected: {affected_text}\n"
            f"Reason: {reason}\n\n"
            "Only the affected test or sample was rejected. Any other active tests on this request remain in progress.\n\n"
            "- NMB-HLab"
        )
    notify(
        req.doctor_id,
        "Laboratory rejection recorded",
        notice_body,
        f"/doctor/requests/{req.id}",
    )
    if req.doctor and req.doctor.email:
        send_email(
            [req.doctor.email],
            f"NMB-HLab laboratory rejection: {req.request_number}",
            email_body,
        )

@bp.route("/")
def dashboard():
    assigned_ids = _assigned_test_ids()
    assigned_tests = (
        TestCatalog.query
        .filter(
            TestCatalog.id.in_(assigned_ids),
            TestCatalog.active.is_(True),
            TestCatalog.deleted_at.is_(None),
        )
        .order_by(TestCatalog.category, TestCatalog.name)
        .all()
        if assigned_ids else []
    )
    priority = (request.args.get("priority") or "").strip()
    request_no = (request.args.get("request") or "").strip()
    category = (request.args.get("category") or "").strip()
    queue_filter = (request.args.get("queue") or "").strip()
    due_filter = (request.args.get("due") or "").strip()
    now = datetime.now()
    eligible_items = (
        TestRequestItem.query
        .options(
            joinedload(TestRequestItem.test),
            selectinload(TestRequestItem.request).selectinload(TestRequest.patient),
            selectinload(TestRequestItem.request).selectinload(TestRequest.samples),
        )
        .join(TestRequest, TestRequest.id == TestRequestItem.request_id)
        .filter(TestRequestItem.test_id.in_(assigned_ids))
        .filter(
            TestRequest.status.in_(
                ("submitted", "samples_received", "in_progress", "completed")
            )
        )
        .all()
        if assigned_ids else []
    )

    work_items = []
    work_items1 = []
    receipt_requests = set()
    for item in eligible_items:
        item.queue = _queue_for_item(item, current_user.id)
        if not item.queue:
            continue
        if item.queue == "receipt":
            if item.request_id in receipt_requests:
                continue
            receipt_requests.add(item.request_id)
        item.due_at = _due_at(item)
        tracks_turnaround = item.status in ("submitted", "in_progress", "to_be_reviewed")
        item.is_overdue = tracks_turnaround and item.due_at < now
        item.is_nearing = (
            tracks_turnaround
            and not item.is_overdue
            and item.due_at <= now + timedelta(minutes=30)
        )
        work_items.append(item)
        work_items1.append(item)

    assigned_count = sum(
        1 for item in work_items
        if item.assigned_to == current_user.id
        and item.status in ("in_progress", "completed", "to_be_reviewed")
    )
    waiting_count = sum(
        1 for item in work_items if item.queue == "waiting"
    )
    receipt_count = sum(
        1 for item in work_items if item.queue == "receipt"
    )
    verification_count = sum(
        1 for item in work_items if item.queue == "verification"
    )
    review_count = sum(
        1 for item in work_items if item.queue == "review"
    )

    categories = sorted({
        item.test.category for item in eligible_items if item.test and item.test.category
    })
    workload_counts = {
        test_id: count
        for test_id, count in (
            db.session.query(
                TestRequestItem.test_id,
                func.count(TestRequestItem.id),
            )
            .join(TestRequest, TestRequest.id == TestRequestItem.request_id)
            .filter(
                TestRequestItem.test_id.in_(assigned_ids),
                TestRequest.status.in_(
                    ("submitted", "samples_received", "in_progress", "completed")
                ),
            )
            .group_by(TestRequestItem.test_id)
            .all()
            if assigned_ids else []
        )
    }
    stat_count = sum(1 for item in work_items if item.request.priority == "stat")
    urgent_count = sum(1 for item in work_items if item.request.priority == "urgent")
    routine_count = sum(1 for item in work_items if item.request.priority == "routine")
    overdue_count = sum(1 for item in work_items if item.is_overdue)
    nearing_count = sum(1 for item in work_items if item.is_nearing)
    workload_chart_rows = sorted(
        assigned_tests,
        key=lambda test: workload_counts.get(test.id, 0),
        reverse=True,
    )[:10]
    rows = []
    for item in work_items:
        if priority and item.request.priority != priority:
            continue
        if request_no and request_no.lower() not in item.request.request_number.lower():
            continue
        if category and item.test.category != category:
            continue
        if queue_filter and item.queue != queue_filter:
            continue
        if due_filter == "overdue" and not item.is_overdue:
            continue
        if due_filter == "nearing" and not item.is_nearing:
            continue
        if due_filter == "today" and item.due_at.date() != now.date():
            continue
        rows.append(item)

    priority_order = {"stat": 0, "urgent": 1, "routine": 2}
    rows.sort(key=lambda item: (
        0 if item.is_overdue else 1,
        priority_order.get(item.request.priority, 3),
        item.due_at,
    ))
    return render_template(
        "technician/dashboard.html",
        assigned_tests=assigned_tests,
        workload_counts=workload_counts,
        work_items=rows,
        assigned_count=assigned_count,
        receipt_count=receipt_count,
        waiting_count=waiting_count,
        verification_count=verification_count,
        review_count=review_count,
        stat_count=stat_count,
        urgent_count=urgent_count,
        overdue_count=overdue_count,
        nearing_count=nearing_count,
        categories=categories,
        queue_chart_labels=[
            "Sample receipt",
            "Selected",
            "Waiting",
            "Verification",
            "Review",
        ],
        queue_chart_values=[
            receipt_count,
            assigned_count,
            waiting_count,
            verification_count,
            review_count,
        ],
        priority_chart_labels=["Routine", "Urgent", "STAT"],
        priority_chart_values=[routine_count, urgent_count, stat_count],
        turnaround_chart_labels=["On time", "Near limit", "Overdue"],
        turnaround_chart_values=[
            max(len(work_items) - nearing_count - overdue_count, 0),
            nearing_count,
            overdue_count,
        ],
        workload_chart_labels=[
            f"{test.code} - {test.name}" for test in workload_chart_rows
        ],
        workload_chart_values=[
            workload_counts.get(test.id, 0) for test in workload_chart_rows
        ],
    )
@bp.route("/worklist", defaults={"filter_type": "all"})
@bp.route("/worklist/<filter_type>")
def worklist(filter_type):
    filter_type = (filter_type or "all").strip().lower()
    if filter_type == "assigned":
        filter_type = "selected"
    valid_filters = {
        "all",
        "receipt",
        "selected",
        "waiting",
        "verification",
        "review",
        "urgent",
        "stat",
        "overdue",
        "nearing",
    }
    if filter_type not in valid_filters:
        filter_type = "all"
    assigned_ids = _assigned_test_ids()
    now = datetime.now()
    eligible_items = (
        TestRequestItem.query
        .options(
            joinedload(TestRequestItem.test),
            selectinload(TestRequestItem.request).selectinload(TestRequest.patient),
            selectinload(TestRequestItem.request).selectinload(TestRequest.samples),
        )
        .join(TestRequest, TestRequest.id == TestRequestItem.request_id)
        .filter(TestRequestItem.test_id.in_(assigned_ids))
        .filter(
            TestRequest.status.in_(
                ("submitted", "samples_received", "in_progress", "completed")
            )
        )
        .all()
        if assigned_ids else []
    )

    work_items = []
    receipt_requests = set()
    for item in eligible_items:
        item.queue = _queue_for_item(item, current_user.id)
        if not item.queue:
            continue
        if item.queue == "receipt":
            if item.request_id in receipt_requests:
                continue
            receipt_requests.add(item.request_id)
        item.due_at = _due_at(item)
        tracks_turnaround = item.status in ("submitted", "in_progress", "to_be_reviewed")
        item.is_overdue = tracks_turnaround and item.due_at < now
        item.is_nearing = (
            tracks_turnaround
            and not item.is_overdue
            and item.due_at <= now + timedelta(minutes=30)
        )
        work_items.append(item)

    if filter_type == "urgent":
        rows = [item for item in work_items if item.request.priority == "urgent"]
    elif filter_type == "stat":
        rows = [item for item in work_items if item.request.priority == "stat"]
    elif filter_type == "overdue":
        rows = [item for item in work_items if item.is_overdue]
    elif filter_type == "nearing":
        rows = [item for item in work_items if item.is_nearing]
    elif filter_type in ("receipt", "selected", "waiting", "verification", "review"):
        rows = [item for item in work_items if item.queue == filter_type]
    else:
        rows = list(work_items)

    priority_order = {"stat": 0, "urgent": 1, "routine": 2}
    rows.sort(key=lambda item: (
        0 if item.is_overdue else 1,
        priority_order.get(item.request.priority, 3),
        item.due_at,
        item.request.created_at,
    ))
    titles = {
        "all": "Technician Worklist",
        "receipt": "Sample Receipt",
        "selected": "Selected Tests",
        "waiting": "Waiting Selection",
        "verification": "Verification Queue",
        "review": "Tests Needing Review",
        "urgent": "Urgent Tests",
        "stat": "STAT Tests",
        "overdue": "Overdue Tests",
        "nearing": "Near Deadline Tests",
    }
    filter_options = [
        {"key": "all", "label": "All"},
        {"key": "receipt", "label": "Sample receipt"},
        {"key": "selected", "label": "Selected"},
        {"key": "waiting", "label": "Waiting"},
        {"key": "verification", "label": "Verification"},
        {"key": "review", "label": "Review"},
        {"key": "urgent", "label": "Urgent"},
        {"key": "stat", "label": "STAT"},
        {"key": "overdue", "label": "Overdue"},
        {"key": "nearing", "label": "Near limit"},
    ]

    return render_template(
        "technician/worklist.html",
        work_items=rows,
        title=titles.get(filter_type, "Technician Worklist"),
        filter_type=filter_type,
        filter_options=filter_options,
    )

@bp.route("/requests/<request_id>/receive", methods=["POST"])
def receive_samples(request_id):
    req = db.session.get(TestRequest, request_id)
    if not req:
        abort(404)
    if not _technician_can_access_request(req):
        abort(403)
    if req.status in ("cancelled", "released"):
        flash("Samples cannot be updated for a closed request.", "error")
        return redirect(url_for("technician.capture", request_id=req.id))
    if not req.samples:
        flash("This request has no recorded samples to receive.", "error")
        return redirect(url_for("technician.capture", request_id=req.id))

    now = datetime.now()
    updated = 0
    for sample in req.samples:
        if sample.status == "rejected":
            continue
        sample.status = "received"
        sample.received_by = current_user.id
        sample.received_at = now
        updated += 1
    if not updated:
        flash("There are no active samples to receive.", "error")
        return redirect(url_for("technician.capture", request_id=req.id))
    _refresh_request_after_sample_receipt(req)
    log_audit(current_user.id, "receive_samples", "test_request", req.id)
    db.session.commit()
    flash("Samples marked as received.", "success")
    return redirect(url_for("technician.capture", request_id=req.id))


@bp.route("/samples/<sample_id>/receive", methods=["POST"])
def receive_sample(sample_id):
    sample = db.session.get(Sample, sample_id)
    if not sample or not sample.request:
        abort(404)
    req = sample.request
    if not _technician_can_access_request(req):
        abort(403)
    if req.status in ("cancelled", "released"):
        flash("Samples cannot be updated for a closed request.", "error")
        return redirect(url_for("technician.capture", request_id=req.id))
    if sample.status == "rejected":
        flash("Rejected samples cannot be received.", "error")
        return redirect(url_for("technician.capture", request_id=req.id))
    sample.status = "received"
    sample.received_by = current_user.id
    sample.received_at = datetime.now()
    _refresh_request_after_sample_receipt(req)
    log_audit(current_user.id, "receive_sample", "sample", sample.id)
    db.session.commit()
    flash(f"Sample {sample.barcode} marked as received.", "success")
    return redirect(url_for("technician.capture", request_id=req.id))


@bp.route("/samples/<sample_id>/reject", methods=["POST"])
def reject_sample(sample_id):
    sample = db.session.get(Sample, sample_id)
    if not sample or not sample.request:
        abort(404)
    req = sample.request
    if not _technician_can_access_request(req):
        abort(403)
    if req.status in ("cancelled", "released"):
        flash("Samples cannot be rejected for a closed request.", "error")
        return redirect(url_for("technician.capture", request_id=req.id))
    reason = (request.form.get("reason") or "").strip()
    if not reason:
        flash("A rejection reason is required.", "error")
        return redirect(url_for("technician.capture", request_id=req.id))
    now = datetime.now()
    sample.status = "rejected"
    sample.rejected_by = current_user.id
    sample.rejected_at = now
    sample.rejection_reason = reason
    affected_items = _items_affected_by_sample(sample)
    item_reason = f"Sample {sample.barcode} rejected: {reason}"
    for item in affected_items:
        _reject_request_item(item, item_reason)
    cancelled_request = _refresh_request_after_item_change(req, item_reason)
    _notify_doctor_of_sample_issue(req, reason, affected_items, cancelled_request)
    log_audit(current_user.id, "reject_sample", "sample", sample.id,
              {
                  "reason": reason,
                  "request_id": req.id,
                  "affected_item_ids": [item.id for item in affected_items],
                  "cancelled_request": cancelled_request,
              })
    db.session.commit()
    if cancelled_request:
        flash("Sample rejected. All active tests were affected, so the request was cancelled.", "success")
        return redirect(url_for("technician.dashboard"))
    if affected_items:
        flash("Sample rejected. Only the affected test(s) were stopped; the rest of the request remains active.", "success")
    else:
        flash("Sample rejected and the doctor was notified. No active test matched this sample type.", "success")
    return redirect(url_for("technician.capture", request_id=req.id))


@bp.route("/requests/<request_id>/cancel", methods=["POST"])
def cancel_request(request_id):
    req = db.session.get(TestRequest, request_id)
    if not req:
        abort(404)
    if not any(_can_perform(item.test_id) for item in req.items):
        abort(403)
    if req.status not in ("submitted", "samples_received"):
        flash("Only submitted or sample-received requests can be cancelled.", "error")
        return redirect(url_for("technician.capture", request_id=req.id))
    reason = (request.form.get("reason") or "").strip()
    if not reason:
        flash("Cancellation reason is required.", "error")
        return redirect(url_for("technician.capture", request_id=req.id))
    req.status = "cancelled"
    req.cancel_reason = reason
    req.cancelled_by = current_user.id
    req.cancelled_at = datetime.now()
    if req.doctor_id:
        notify(
            req.doctor_id,
            "Test request cancelled by laboratory",
            f"Request {req.request_number} was cancelled: {reason}",
            f"/doctor/requests/{req.id}",
        )
        if req.doctor and req.doctor.email:
            send_email(
                [req.doctor.email],
                f"NMB-HLab request cancelled: {req.request_number}",
                (
                    f"Hello Dr. {req.doctor.full_name or req.doctor.email},\n\n"
                    f"Laboratory request {req.request_number} has been cancelled by the laboratory.\n\n"
                    f"Reason: {reason}\n\n"
                    "Please review the request in the portal for any follow-up action.\n\n"
                    "- NMB-HLab"
                ),
            )
    log_audit(current_user.id, "technician_cancel_request", "test_request", req.id,
              {"reason": reason})
    db.session.commit()
    flash("Request cancelled and the doctor was notified.", "success")
    return redirect(url_for("technician.dashboard"))


@bp.route("/items/<item_id>/select", methods=["POST"])
def select_item(item_id):
    item = db.session.get(TestRequestItem, item_id)
    if not item:
        abort(404)
    if not _can_perform(item.test_id):
        abort(403)
    if item.request.status not in ("samples_received", "in_progress"):
        flash("Samples must be received before selecting a test.", "error")
        return redirect(url_for("technician.capture", request_id=item.request_id))
    if not _required_sample_received(item):
        flash("The required sample for this test must be received before selecting it.", "error")
        return redirect(url_for("technician.capture", request_id=item.request_id))
    if item.assigned_to and item.assigned_to != current_user.id:
        flash("This test has already been selected by another technician.", "error")
        return redirect(url_for("technician.capture", request_id=item.request_id))
    if item.status != "submitted":
        flash("Only submitted tests can be selected.", "error")
        return redirect(url_for("technician.capture", request_id=item.request_id))

    shortages = [
        link.consumable.name
        for link in item.test.test_consumables
        if link.consumable.current_stock < link.quantity_required
    ]
    if shortages:
        flash("Insufficient stock for: " + ", ".join(shortages), "error")
        return redirect(url_for("technician.capture", request_id=item.request_id))

    for link in item.test.test_consumables:
        link.consumable.current_stock -= link.quantity_required
        db.session.add(StockMovement(
            consumable_id=link.consumable_id,
            movement_type="out",
            quantity=link.quantity_required,
            notes=f"Used for {item.request.request_number} - {item.test.name}",
            created_by=current_user.id,
        ))

    item.assigned_to = current_user.id
    item.started_at = datetime.now()
    item.status = "in_progress"
    item.request.status = "in_progress"
    log_audit(current_user.id, "select_test", "test_request_item", item.id)
    db.session.commit()
    flash(f"{item.test.name} selected. Consumables were deducted from stock.", "success")
    return redirect(url_for("technician.capture", request_id=item.request_id))


@bp.route("/items/<item_id>/reject", methods=["POST"])
def reject_item(item_id):
    item = db.session.get(TestRequestItem, item_id)
    if not item:
        abort(404)
    if not _can_perform(item.test_id):
        abort(403)
    req = item.request
    if req.status in ("cancelled", "released"):
        flash("Tests cannot be rejected for a closed request.", "error")
        return redirect(url_for("technician.capture", request_id=req.id))
    if item.status in ("completed", "verified", "cancelled"):
        flash("Only active, unreleased tests can be rejected.", "error")
        return redirect(url_for("technician.capture", request_id=req.id))
    if item.assigned_to and item.assigned_to != current_user.id:
        flash("This test has already been selected by another technician.", "error")
        return redirect(url_for("technician.capture", request_id=req.id))
    reason = (
        request.form.get(f"reject_reason_{item.id}")
        or request.form.get("reason")
        or ""
    ).strip()
    if not reason:
        flash("A rejection reason is required.", "error")
        return redirect(url_for("technician.capture", request_id=req.id))

    _reject_request_item(item, reason)
    cancelled_request = _refresh_request_after_item_change(req, f"Test rejected: {reason}")
    _notify_doctor_of_sample_issue(req, reason, [item], cancelled_request)
    log_audit(current_user.id, "technician_reject_test_item", "test_request_item", item.id,
              {
                  "reason": reason,
                  "request_id": req.id,
                  "cancelled_request": cancelled_request,
              })
    db.session.commit()
    if cancelled_request:
        flash("Test rejected. No active tests remain, so the request was cancelled.", "success")
        return redirect(url_for("technician.dashboard"))
    flash("Test rejected. The other tests on this request remain active.", "success")
    return redirect(url_for("technician.capture", request_id=req.id))


@bp.route("/capture/<request_id>", methods=["GET", "POST"])
def capture(request_id):
    req = db.session.get(TestRequest, request_id)
    if not req:
        abort(404)
    assigned_ids = _assigned_test_ids()
    eligible_items = [item for item in req.items if item.test_id in assigned_ids]
    if not eligible_items:
        abort(403)

    if request.method == "POST":
        action = request.form.get("action") or "complete"
        if action not in ("draft", "complete"):
            action = "complete"
        changed = 0
        for item in eligible_items:
            if item.assigned_to != current_user.id:
                continue
            if item.status not in ("in_progress", "to_be_reviewed"):
                continue
            value = (request.form.get(f"value_{item.id}") or "").strip()
            text_value = (request.form.get(f"text_{item.id}") or "").strip()
            notes = (request.form.get(f"notes_{item.id}") or "").strip()
            if action == "complete" and not value and not text_value:
                continue
            if action == "draft" and not value and not text_value and not notes:
                continue
            if value:
                try:
                    item.result_value = float(value)
                except ValueError:
                    flash(f"{item.test.name}: numeric result is invalid.", "error")
                    return redirect(url_for("technician.capture", request_id=req.id))
            else:
                item.result_value = None
            item.result_text = text_value or None
            item.result_notes = notes or None
            if action == "draft":
                db.session.add(TestResultReview(
                    item_id=item.id,
                    reviewer_id=current_user.id,
                    action="draft_saved",
                    note=notes or None,
                ))
            else:
                item.captured_by = current_user.id
                item.captured_at = datetime.now()
                item.completed_at = item.captured_at
                item.status = "completed"
                item.review_notes = None
                if (
                    item.result_value is not None
                    and item.test.reference_low is not None
                    and item.test.reference_high is not None
                ):
                    if item.result_value < float(item.test.reference_low):
                        item.abnormal_flag = "low"
                    elif item.result_value > float(item.test.reference_high):
                        item.abnormal_flag = "high"
                    else:
                        item.abnormal_flag = None
                else:
                    item.abnormal_flag = None
                db.session.add(TestResultReview(
                    item_id=item.id,
                    reviewer_id=current_user.id,
                    action="completed",
                    note=notes or None,
                ))
            changed += 1

        if not changed:
            flash(
                "Enter a result for at least one test assigned to you."
                if action == "complete"
                else "Enter a result or note for at least one test assigned to you.",
                "error",
            )
            return redirect(url_for("technician.capture", request_id=req.id))
        if action == "complete":
            active_items = _active_request_items(req)
            req.status = (
                "completed"
                if active_items and all(item.status in ("completed", "verified") for item in active_items)
                else "in_progress"
            )
            log_audit(current_user.id, "capture_results", "test_request", req.id)
        else:
            log_audit(current_user.id, "save_result_draft", "test_request", req.id)
        db.session.commit()
        if action == "draft":
            flash("Draft saved. The result has not been sent for verification yet.", "success")
            return redirect(url_for("technician.capture", request_id=req.id))
        flash("Results saved and sent for verification.", "success")
        return redirect(url_for("technician.dashboard"))

    now = datetime.now()
    for item in eligible_items:
        item.due_at = _due_at(item)
        item.is_overdue = item.due_at < now
    return render_template(
        "technician/capture.html",
        req=req,
        eligible_items=eligible_items,
    )


@bp.route("/verify")
def verify_list():
    assigned_ids = _assigned_test_ids()
    filters = {
        "q": (request.args.get("q") or "").strip(),
        "priority": (request.args.get("priority") or "").strip(),
        "flag": (request.args.get("flag") or "").strip().lower(),
    }
    if filters["priority"] not in PRIORITIES:
        filters["priority"] = ""
    if filters["flag"] not in ("abnormal", "low", "high", "normal"):
        filters["flag"] = ""

    query = (
        TestRequestItem.query
        .options(
            joinedload(TestRequestItem.test),
            selectinload(TestRequestItem.request).selectinload(TestRequest.patient),
        )
        .join(TestRequest, TestRequest.id == TestRequestItem.request_id)
        .join(TestCatalog, TestCatalog.id == TestRequestItem.test_id)
        .filter(
            TestRequestItem.status == "completed",
            TestRequestItem.test_id.in_(assigned_ids),
            TestRequestItem.captured_by != current_user.id,
        )
    )
    if filters["q"]:
        like = f"%{filters['q']}%"
        query = query.filter(or_(
            TestRequest.request_number.ilike(like),
            TestCatalog.code.ilike(like),
            TestCatalog.name.ilike(like),
        ))
    if filters["priority"]:
        query = query.filter(TestRequest.priority == filters["priority"])
    if filters["flag"] == "low":
        query = query.filter(TestRequestItem.abnormal_flag == "low")
    elif filters["flag"] == "high":
        query = query.filter(TestRequestItem.abnormal_flag == "high")
    elif filters["flag"] == "abnormal":
        query = query.filter(TestRequestItem.abnormal_flag.isnot(None))
    elif filters["flag"] == "normal":
        query = query.filter(TestRequestItem.abnormal_flag.is_(None))

    items = query.order_by(TestRequest.priority.desc(), TestRequestItem.completed_at.asc()).all() if assigned_ids else []
    return render_template(
        "technician/verify.html",
        items=items,
        filters=filters,
        priorities=PRIORITIES,
    )


@bp.route("/verify/<item_id>", methods=["POST"])
def verify_result(item_id):
    item = db.session.get(TestRequestItem, item_id)
    if not item:
        abort(404)
    action = request.form.get("action") or "verify"
    note = (request.form.get("note") or "").strip() or None
    if action == "return" and request.form.get("viewed_result") != "1":
        flash("Open and review the result details before rejecting the result.", "error")
        return redirect(url_for("technician.verify_list"))
    try:
        if action == "return":
            return_item_for_review(item, current_user.id, note)
        else:
            verify_item(item, current_user.id, note=note)
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "error")
        return redirect(url_for("technician.verify_list"))
    db.session.commit()
    message = "Result returned for review." if action == "return" else "Result verified."
    flash(message, "success")
    return redirect(url_for("technician.verify_list"))


@bp.route("/reports")
def reports():
    from flask import send_file
    from sqlalchemy import func

    from ..reports import build_report_pdf, parse_range

    frm, to, start, end = parse_range(request.args)
    by_cat = (
        db.session.query(TestCatalog.category, func.count(TestRequestItem.id))
        .join(TestRequestItem, TestRequestItem.test_id == TestCatalog.id)
        .filter(
            TestRequestItem.captured_by == current_user.id,
            TestRequestItem.completed_at.between(start, end),
        )
        .group_by(TestCatalog.category)
        .order_by(func.count(TestRequestItem.id).desc())
        .all()
    )
    captured = TestRequestItem.query.filter(
        TestRequestItem.captured_by == current_user.id,
        TestRequestItem.completed_at.between(start, end),
    ).count()
    verified = TestRequestItem.query.filter(
        TestRequestItem.verified_by == current_user.id,
        TestRequestItem.verified_at.between(start, end),
    ).count()
    abnormal = TestRequestItem.query.filter(
        TestRequestItem.captured_by == current_user.id,
        TestRequestItem.completed_at.between(start, end),
        TestRequestItem.abnormal_flag.in_(("low", "high")),
    ).count()
    flag_counts = {flag or "normal": count for flag, count in (
        db.session.query(TestRequestItem.abnormal_flag, func.count(TestRequestItem.id))
        .filter(
            TestRequestItem.captured_by == current_user.id,
            TestRequestItem.completed_at.between(start, end),
        )
        .group_by(TestRequestItem.abnormal_flag)
        .all()
    )}
    low_count = flag_counts.get("low", 0)
    high_count = flag_counts.get("high", 0)
    normal_count = max(captured - low_count - high_count, 0)
    detail_rows = (
        db.session.query(TestCatalog.code, TestCatalog.name, func.count(TestRequestItem.id))
        .join(TestRequestItem, TestRequestItem.test_id == TestCatalog.id)
        .filter(
            TestRequestItem.captured_by == current_user.id,
            TestRequestItem.completed_at.between(start, end),
        )
        .group_by(TestCatalog.code, TestCatalog.name)
        .order_by(func.count(TestRequestItem.id).desc())
        .all()
    )
    trend_counts = {}
    trend_days = []
    for offset in range((to - frm).days + 1):
        day = frm + timedelta(days=offset)
        trend_days.append(day)
        trend_counts[day.isoformat()] = 0
    for completed_at, in (
        db.session.query(TestRequestItem.completed_at)
        .filter(
            TestRequestItem.captured_by == current_user.id,
            TestRequestItem.completed_at.between(start, end),
        )
        .all()
    ):
        if completed_at:
            key = completed_at.date().isoformat()
            if key in trend_counts:
                trend_counts[key] += 1

    if request.args.get("format") == "pdf":
        buf = build_report_pdf(
            f"Technician Report - {current_user.full_name or current_user.email}",
            subtitle=f"Range: {frm:%Y-%m-%d} to {to:%Y-%m-%d}",
            summary=[
                f"Tests completed: <b>{captured}</b>",
                f"Tests verified for other technicians: <b>{verified}</b>",
                f"Abnormal results captured: <b>{abnormal}</b>",
            ],
            sections=[
                {
                    "heading": "Tests completed by category",
                    "headers": ["Category", "Tests"],
                    "rows": [[category or "-", count] for category, count in by_cat]
                    or [["No data", 0]],
                },
                {
                    "heading": "Per-test breakdown",
                    "headers": ["Code", "Test", "Count"],
                    "rows": [[code, name, count] for code, name, count in detail_rows]
                    or [["-", "No tests completed", 0]],
                },
            ],
        )
        return send_file(
            buf,
            mimetype="application/pdf",
            as_attachment=True,
            download_name=f"technician-report-{frm}_{to}.pdf",
        )
    return render_template(
        "technician/reports.html",
        frm=frm,
        to=to,
        captured=captured,
        verified=verified,
        abnormal=abnormal,
        by_cat=by_cat,
        detail_rows=detail_rows,
        mix_chart_labels=["Captured", "Verified"],
        mix_chart_values=[captured, verified],
        flag_chart_labels=["Normal", "Low", "High"],
        flag_chart_values=[normal_count, low_count, high_count],
        category_chart_labels=[category or "-" for category, _count in by_cat],
        category_chart_values=[count for _category, count in by_cat],
        trend_chart_labels=[day.strftime("%d %b") for day in trend_days],
        trend_chart_values=[trend_counts[day.isoformat()] for day in trend_days],
        top_test_chart_labels=[f"{code} - {name}" for code, name, _count in detail_rows[:10]],
        top_test_chart_values=[count for _code, _name, count in detail_rows[:10]],
    )
