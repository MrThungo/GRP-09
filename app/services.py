"""Business logic shared across blueprints."""
import json
from datetime import datetime
from .extensions import db
from .models import (
    Notification, AuditLog, StockMovement, Consumable, User, UserRole,
    TestRequest, TestRequestItem, TechnicianTest, TestResultReview, ChatMessage,
)
from .reports import build_request_results_pdf
from .url_utils import app_url


def log_audit(actor_id, action, entity_type=None, entity_id=None, details=None):
    db.session.add(AuditLog(
        actor_id=actor_id, action=action,
        entity_type=entity_type, entity_id=entity_id,
        details=json.dumps(details) if details else None,
    ))


def notify(user_id, title, body=None, link=None):
    db.session.add(Notification(user_id=user_id, title=title, body=body, link=link))


def send_email(recipients, subject, body, attachments=None):
    """Best-effort SMTP send used for project-required e-mail notices."""
    from .email import send_email as configured_send_email

    return configured_send_email(
        recipients=recipients,
        subject=subject,
        body=body,
        attachments=attachments,
    )


def notify_admins(title, body=None, link=None):
    admin_ids = (
        db.session.query(UserRole.user_id).filter_by(role="admin").all()
    )
    for (uid,) in admin_ids:
        notify(uid, title, body, link)


def _has_chronic_history(patient):
    return bool(
        patient
        and (
            (patient.chronic_conditions or "").strip()
            or getattr(patient, "conditions", None)
        )
    )


def apply_stock_movement(movement: StockMovement):
    cons = db.session.get(Consumable, movement.consumable_id)
    if not cons:
        return
    if movement.movement_type == "in":
        cons.current_stock += movement.quantity
    elif movement.movement_type == "out":
        cons.current_stock = max(0, cons.current_stock - movement.quantity)
    else:
        cons.current_stock = movement.quantity


def release_request(req: TestRequest, actor_id, note=None):
    already_released = req.status == "released"
    req.status = "released"
    req.release_note = note or req.release_note
    req.released_at = datetime.now()
    results_url = app_url("/patient/results", external=True)
    pdf_url = app_url(f"/patient/results/{req.id}/pdf", external=True)
    chronic_release = _has_chronic_history(req.patient)
    if not already_released and req.patient and req.patient.profile_id:
        notify(req.patient.profile_id,
               "New test results available",
               note or f"Results for request {req.request_number} have been released. Sign in to view your results.",
               "/patient/results")
    if not already_released and req.doctor_id and req.patient and req.patient.profile_id:
        if chronic_release:
            message_body = (
                f"Your results for request {req.request_number} have been released.\n\n"
                "Because chronic history is recorded on your profile, please sign in to review the results securely in the portal.\n"
                f"Open results: {results_url}"
            )
        else:
            message_body = (
                f"Your results for request {req.request_number} have been released.\n\n"
                "Your released report is attached below as secure portal links:\n"
                f"View results: {results_url}\n"
                f"Download PDF: {pdf_url}"
            )
        if note:
            message_body += f"\n\nRelease note: {note}"
        db.session.add(ChatMessage(
            sender_id=req.doctor_id,
            recipient_id=req.patient.profile_id,
            body=message_body,
        ))
    if not already_released and req.patient and req.patient.email:
        patient_body = (
            f"Hello {req.patient.full_name or req.patient.email},\n\n"
            f"Results for request {req.request_number} are now available in the MediLab Connect portal.\n\n"
        )
        if note:
            patient_body += f"Release note: {note}\n\n"
        patient_body += (
            "Please sign in to view the report. If you have questions about the results, contact your doctor or the laboratory.\n\n"
            f"Open results: {results_url}\n\n"
            ""
        )
        attachments = []
        if not chronic_release:
            pdf = build_request_results_pdf(req)
            patient_body += f"Download PDF after sign-in: {pdf_url}\n\n"
            attachments.append((f"{req.request_number}-results.pdf", "application/pdf", pdf.getvalue()))
        else:
            patient_body += "No report file is attached to this email. Please use the secure portal to view chronic-care results.\n\n"
        send_email(
            [req.patient.email],
            f"MediLab Connect results released: {req.request_number}",
            patient_body,
            attachments,
        )
    if not already_released and req.doctor_id:
        notify(req.doctor_id,
               "Request released",
               f"Request {req.request_number} was released to the patient.",
               "/doctor/requests")
    log_audit(actor_id, "release_request", "test_request", req.id)


def _technician_can_perform(actor_id, test_id):
    return TechnicianTest.query.filter_by(
        technician_id=actor_id,
        test_id=test_id,
    ).first() is not None


def verify_item(item: TestRequestItem, actor_id, note=None):
    if item.captured_by and item.captured_by == actor_id:
        raise ValueError("A technician cannot verify their own captured results.")
    if not _technician_can_perform(actor_id, item.test_id):
        raise ValueError("You are not assigned to verify this test type.")
    item.verified_by = actor_id
    item.verified_at = datetime.now()
    item.verification_notes = note or item.verification_notes
    item.status = "verified"
    db.session.add(TestResultReview(
        item_id=item.id,
        reviewer_id=actor_id,
        action="verified",
        note=note,
    ))
    req = item.request
    active_items = [i for i in req.items if i.status != "cancelled"]
    if active_items and all(i.status == "verified" for i in active_items):
        req.status = "completed"
        pdf = build_request_results_pdf(req)
        # Notify the requesting doctor so they can review and release to patient.
        if req.doctor_id:
            notify(req.doctor_id,
                   "Results ready for release",
                   f"Request {req.request_number} has been verified and is ready to release.",
                   f"/doctor/requests/{req.id}")
            if req.doctor and req.doctor.email:
                request_url = app_url(f"/doctor/requests/{req.id}", external=True)
                send_email(
                    [req.doctor.email],
                    f"MediLab Connect verified results: {req.request_number}",
                    (
                        f"Hello Dr. {req.doctor.full_name or req.doctor.email},\n\n"
                        f"Results for request {req.request_number} have been verified by the laboratory.\n\n"
                        "Please review the attached report and release it to the patient when appropriate.\n\n"
                        f"Open request: {request_url}\n\n"
                        ""
                    ),
                    [(f"{req.request_number}-verified-results.pdf", "application/pdf", pdf.getvalue())],
                )
    log_audit(actor_id, "verify_item", "test_request_item", item.id)


def return_item_for_review(item: TestRequestItem, actor_id, note):
    if not note:
        raise ValueError("A review note is required when returning a result.")
    if item.captured_by and item.captured_by == actor_id:
        raise ValueError("A technician cannot verify their own captured results.")
    if not _technician_can_perform(actor_id, item.test_id):
        raise ValueError("You are not assigned to verify this test type.")
    item.status = "to_be_reviewed"
    item.review_notes = note
    item.near_limit_reminded_at = None
    if item.request and item.request.status not in ("released", "cancelled"):
        item.request.status = "in_progress"
    db.session.add(TestResultReview(
        item_id=item.id,
        reviewer_id=actor_id,
        action="returned",
        note=note,
    ))
    if item.captured_by:
        notify(item.captured_by,
               "Result returned for review",
               f"{item.test.name} on request {item.request.request_number} needs review: {note}",
               f"/technician/capture/{item.request_id}")
    log_audit(actor_id, "return_item_for_review", "test_request_item", item.id)


def doctor_return_item_for_review(item: TestRequestItem, actor_id, note):
    if not note:
        raise ValueError("A note is required when sending a result back.")
    req = item.request
    if not req or req.doctor_id != actor_id:
        raise ValueError("Only the requesting doctor can send this result back.")
    if req.status in ("released", "cancelled"):
        raise ValueError("Released or cancelled requests cannot be sent back.")
    if item.status != "verified":
        raise ValueError("Only verified results can be sent back to the technician.")

    previous_verified_by = item.verified_by
    item.status = "to_be_reviewed"
    item.review_notes = note
    item.near_limit_reminded_at = None
    item.verified_by = None
    item.verified_at = None
    item.verification_notes = None
    req.status = "in_progress"

    db.session.add(TestResultReview(
        item_id=item.id,
        reviewer_id=actor_id,
        action="doctor_returned",
        note=note,
    ))
    if item.captured_by:
        notify(item.captured_by,
               "Result sent back by doctor",
               f"{item.test.name} on request {req.request_number} needs review: {note}",
               f"/technician/capture/{item.request_id}")
    if previous_verified_by and previous_verified_by != item.captured_by:
        notify(previous_verified_by,
               "Doctor sent back a verified result",
               f"{item.test.name} on request {req.request_number} needs review: {note}",
               f"/technician/verify")
    log_audit(actor_id, "doctor_return_item_for_review", "test_request_item", item.id,
              {"note": note, "request_id": req.id})
