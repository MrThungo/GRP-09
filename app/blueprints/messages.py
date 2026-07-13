from datetime import datetime, timedelta

from flask import Blueprint, abort, jsonify, render_template, request
from flask_login import current_user, login_required
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import selectinload

from ..auth_utils import role_required
from ..extensions import db
from ..models import ChatMessage, Patient, ROLE_LABELS, TestRequest, User, UserRole
from ..presence import presence_label

bp = Blueprint("messages", __name__, template_folder="../templates/messages")

CLINICAL_ROLES = {"doctor", "lab_manager", "lab_technician"}
ALLOWED_ROLES = CLINICAL_ROLES | {"patient"}
ONLINE_WINDOW_MINUTES = 2


@bp.before_request
@login_required
@role_required("doctor", "patient", "lab_manager", "lab_technician")
def _gate():
    pass


def _active_users_query():
    return User.query.options(
        selectinload(User.user_roles),
        selectinload(User.patient_record),
    ).filter(
        User.deleted_at.is_(None),
        User.is_blocked.is_(False),
        User.is_deactivated.is_(False),
    )


def _is_active_user(user):
    return bool(
        user
        and user.id != current_user.id
        and user.primary_role in ALLOWED_ROLES
        and user.deleted_at is None
        and not user.is_blocked
        and not user.is_deactivated
    )


def _my_patient_record():
    return Patient.query.filter_by(profile_id=current_user.id, deleted_at=None).first()


def _doctor_has_patient(doctor_id, patient_id):
    return db.session.query(TestRequest.id).filter_by(
        doctor_id=doctor_id,
        patient_id=patient_id,
    ).first() is not None


def _can_message(user):
    if not _is_active_user(user):
        return False

    role = current_user.primary_role
    target_role = user.primary_role

    if role == "patient":
        patient = _my_patient_record()
        return bool(
            patient
            and target_role == "doctor"
            and _doctor_has_patient(user.id, patient.id)
        )

    if role == "doctor":
        if target_role == "patient":
            patient = user.patient_record
            return bool(
                patient
                and patient.deleted_at is None
                and _doctor_has_patient(current_user.id, patient.id)
            )
        return target_role in {"lab_manager", "lab_technician"}

    if role == "lab_manager":
        return target_role in {"doctor", "lab_technician"}

    if role == "lab_technician":
        return target_role in {"doctor", "lab_manager"}

    return False


def _display_name(user):
    name = " ".join(part for part in [user.full_name, user.surname] if part).strip()
    return name or user.email


def _presence(user):
    now = datetime.now()
    cutoff = now - timedelta(minutes=ONLINE_WINDOW_MINUTES)
    online = bool(user.last_seen and user.last_seen >= cutoff)
    return online, presence_label(user.last_seen, online=online, now=now)


def _unread_count(user_id):
    return ChatMessage.query.filter_by(
        sender_id=user_id,
        recipient_id=current_user.id,
        read_at=None,
    ).count()


def _unread_counts_for_senders(sender_ids):
    if not sender_ids:
        return {}
    rows = (
        db.session.query(ChatMessage.sender_id, func.count(ChatMessage.id))
        .filter(
            ChatMessage.recipient_id == current_user.id,
            ChatMessage.sender_id.in_(sender_ids),
            ChatMessage.read_at.is_(None),
        )
        .group_by(ChatMessage.sender_id)
        .all()
    )
    return {sender_id: count for sender_id, count in rows}


def _contact_payload(user, include_user=False, unread_count=None):
    online, presence_label = _presence(user)
    payload = {
        "id": user.id,
        "name": _display_name(user),
        "email": user.email,
        "role": user.primary_role,
        "role_label": ROLE_LABELS.get(user.primary_role, (user.primary_role or "").title()),
        "unread_count": _unread_count(user.id) if unread_count is None else unread_count,
        "online": online,
        "presence_label": presence_label,
    }
    if include_user:
        payload["user"] = user
    return payload


def _dedupe_users(users, validate=True):
    seen = {}
    for user in users:
        if user and user.id not in seen and (not validate or _can_message(user)):
            seen[user.id] = user
    return sorted(seen.values(), key=lambda item: (_display_name(item).lower(), item.email.lower()))


def _doctor_patient_contacts():
    patients = (
        Patient.query
        .options(selectinload(Patient.profile).selectinload(User.user_roles))
        .join(TestRequest, TestRequest.patient_id == Patient.id)
        .join(User, Patient.profile_id == User.id)
        .filter(
            TestRequest.doctor_id == current_user.id,
            Patient.deleted_at.is_(None),
            Patient.profile_id.isnot(None),
            User.deleted_at.is_(None),
            User.is_blocked.is_(False),
            User.is_deactivated.is_(False),
        )
        .distinct()
        .all()
    )
    return [patient.profile for patient in patients if patient.profile]


def _patient_doctor_contacts():
    patient = _my_patient_record()
    if not patient:
        return []
    return (
        _active_users_query()
        .join(TestRequest, TestRequest.doctor_id == User.id)
        .filter(
            TestRequest.patient_id == patient.id,
            TestRequest.doctor_id.isnot(None),
        )
        .distinct()
        .all()
    )


def _clinical_contacts(target_roles):
    return (
        _active_users_query()
        .join(UserRole, UserRole.user_id == User.id)
        .filter(
            User.id != current_user.id,
            UserRole.role.in_(target_roles),
        )
        .distinct()
        .all()
    )


def _contacts_for_current_user(include_user=False):
    role = current_user.primary_role
    users = []
    if role == "doctor":
        users.extend(_doctor_patient_contacts())
        users.extend(_clinical_contacts(["lab_manager", "lab_technician"]))
    elif role == "patient":
        users.extend(_patient_doctor_contacts())
    elif role == "lab_manager":
        users.extend(_clinical_contacts(["doctor", "lab_technician"]))
    elif role == "lab_technician":
        users.extend(_clinical_contacts(["doctor", "lab_manager"]))

    deduped = _dedupe_users(users, validate=False)
    unread_counts = _unread_counts_for_senders([user.id for user in deduped])
    return [
        _contact_payload(
            user,
            include_user=include_user,
            unread_count=unread_counts.get(user.id, 0),
        )
        for user in deduped
    ]


def _contact_or_404(user_id):
    user = db.session.get(User, user_id)
    if not _can_message(user):
        abort(404)
    return user


def _mark_thread_read(sender_id):
    now = datetime.now()
    updated = (
        ChatMessage.query
        .filter_by(sender_id=sender_id, recipient_id=current_user.id, read_at=None)
        .update({"read_at": now}, synchronize_session=False)
    )
    if updated:
        db.session.commit()


def _message_payload(message):
    return {
        "id": message.id,
        "sender_id": message.sender_id,
        "recipient_id": message.recipient_id,
        "body": message.body,
        "mine": message.sender_id == current_user.id,
        "read": bool(message.read_at),
        "read_at": message.read_at.isoformat() if message.read_at else None,
        "created_at": message.created_at.isoformat(),
        "created_label": message.created_at.strftime("%d %b %H:%M"),
    }


@bp.route("/")
def index():
    contacts = _contacts_for_current_user(include_user=True)
    selected = None
    selected_id = request.args.get("with")

    if selected_id:
        selected = next((contact for contact in contacts if contact["id"] == selected_id), None)
    elif contacts:
        selected = contacts[0]

    if selected:
        _mark_thread_read(selected["id"])
        contacts = _contacts_for_current_user(include_user=True)
        selected = next((contact for contact in contacts if contact["id"] == selected["id"]), selected)

    return render_template(
        "messages/index.html",
        contacts=contacts,
        selected_contact=selected,
    )


@bp.route("/api/contacts")
def api_contacts():
    return jsonify({"contacts": _contacts_for_current_user()})


@bp.route("/api/thread/<user_id>")
def api_thread(user_id):
    user = _contact_or_404(user_id)
    _mark_thread_read(user.id)
    messages = (
        ChatMessage.query
        .filter(or_(
            and_(ChatMessage.sender_id == current_user.id, ChatMessage.recipient_id == user.id),
            and_(ChatMessage.sender_id == user.id, ChatMessage.recipient_id == current_user.id),
        ))
        .order_by(ChatMessage.created_at.asc())
        .limit(250)
        .all()
    )
    return jsonify({
        "contact": _contact_payload(user),
        "messages": [_message_payload(message) for message in messages],
    })


@bp.route("/api/thread/<user_id>", methods=["POST"])
def api_send(user_id):
    user = _contact_or_404(user_id)
    payload = request.get_json(silent=True) or request.form
    body = (payload.get("body") or "").strip()
    if not body:
        return jsonify({"error": "Message cannot be empty."}), 400
    if len(body) > 1000:
        return jsonify({"error": "Message is too long."}), 400

    message = ChatMessage(
        sender_id=current_user.id,
        recipient_id=user.id,
        body=body,
    )
    db.session.add(message)
    db.session.commit()
    return jsonify({"message": _message_payload(message)}), 201
