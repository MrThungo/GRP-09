"""Operational reminders for time-sensitive laboratory work."""
import json
from datetime import datetime, timedelta

from flask import current_app
from sqlalchemy.orm import joinedload

from .email import send_email
from .extensions import db
from .models import AuditLog, Notification, TestRequest, TestRequestItem, User

NEAR_LIMIT_WINDOW_MINUTES = 30


def _due_at(item):
    start = item.started_at or item.request.created_at
    return start + timedelta(minutes=max(item.test.turnaround_minutes, 1))


def _send_email(recipients, subject, body):
    return send_email(recipients, subject, body)


def send_near_limit_reminders(now=None, window_minutes=NEAR_LIMIT_WINDOW_MINUTES, limit=200):
    """Send one reminder per assigned item when it is nearing turnaround limit."""
    now = now or datetime.now()
    window_end = now + timedelta(minutes=window_minutes)
    sent = 0

    candidates = (
        TestRequestItem.query
        .options(
            joinedload(TestRequestItem.test),
            joinedload(TestRequestItem.request).joinedload(TestRequest.patient),
        )
        .join(TestRequest, TestRequest.id == TestRequestItem.request_id)
        .filter(
            TestRequestItem.assigned_to.isnot(None),
            TestRequestItem.near_limit_reminded_at.is_(None),
            TestRequestItem.status.in_(("in_progress", "to_be_reviewed")),
            TestRequest.status.in_(("in_progress", "samples_received")),
        )
        .order_by(TestRequestItem.started_at.asc(), TestRequestItem.created_at.asc())
        .limit(limit)
        .all()
    )

    due_items = []
    technician_ids = set()
    for item in candidates:
        if not item.test or not item.request:
            continue
        due_at = _due_at(item)
        if due_at < now or due_at > window_end:
            continue
        due_items.append((item, due_at))
        technician_ids.add(item.assigned_to)

    technicians = {
        user.id: user for user in (
            User.query.filter(User.id.in_(technician_ids)).all()
            if technician_ids else []
        )
    }

    for item, due_at in due_items:
        technician = technicians.get(item.assigned_to)
        if not technician or not technician.is_active:
            continue

        request_number = item.request.request_number
        patient_name = item.request.patient.full_name if item.request.patient else "-"
        due_label = due_at.strftime("%Y-%m-%d %H:%M")
        title = "Test nearing turnaround limit"
        body = (
            f"{item.test.code} {item.test.name} on request {request_number} "
            f"is due by {due_label}."
        )
        link = f"/technician/capture/{item.request_id}"
        db.session.add(Notification(
            user_id=technician.id,
            title=title,
            body=body,
            link=link,
        ))

        app_base = (current_app.config.get("APP_BASE_URL") or "").rstrip("/")
        priority = (item.request.priority or "routine").upper()
        _send_email(
            [technician.email],
            f"NMB-HLab turnaround reminder: {item.test.code}",
            (
                f"Hello {technician.full_name or technician.email},\n\n"
                f"{item.test.code} {item.test.name} is approaching its turnaround time limit.\n\n"
                f"Request: {request_number}\n"
                f"Patient: {patient_name}\n"
                f"Priority: {priority}\n"
                f"Due by: {due_label}\n\n"
                f"Please review the request and update the result status as soon as possible:\n{app_base}{link}\n\n"
                ""
            ),
        )

        item.near_limit_reminded_at = now
        db.session.add(AuditLog(
            actor_id=None,
            action="near_limit_reminder_sent",
            entity_type="test_request_item",
            entity_id=item.id,
            details=json.dumps({
                "technician_id": technician.id,
                "request_id": item.request_id,
                "due_at": due_at.isoformat(),
            }),
        ))
        sent += 1

    if sent:
        db.session.commit()
    return sent
