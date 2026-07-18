"""Patient-aware chatbot logic for Twilio Conversations and portal chat."""
from __future__ import annotations

import base64
import json
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime

from flask import current_app, has_request_context, request, url_for
from sqlalchemy import func
from sqlalchemy.orm import selectinload

from .extensions import db
from .models import (
    AccessRequest,
    Allergy,
    AuditLog,
    Consumable,
    ConsumableOrder,
    Condition,
    ConsentGrant,
    Medication,
    Notification,
    DoctorAvailabilitySlot,
    OnlineConsultation,
    Patient,
    Supplier,
    TechnicianTest,
    TestCatalog,
    TestRequest,
    TestRequestItem,
    TITLE_OPTIONS,
    GENDER_OPTIONS,
    ROLE_LABELS,
    ROLES,
    User,
    UserRole,
)
from .reports import format_reference, format_result_value
from .sa_id import validate_sa_id
from .services import log_audit, notify, send_email as send_portal_email


@dataclass
class ChatbotReply:
    body: str
    links: list[dict[str, str]]


BLOOD_TYPES = ("A+", "A-", "B+", "B-", "AB+", "AB-", "O+", "O-")
PROFILE_FIELD_ALIASES = {
    "title": ("title", "salutation"),
    "phone": ("phone", "cellphone", "cell phone", "mobile", "mobile number", "contact number"),
    "id_number": ("id number", "sa id", "south african id", "identity number"),
    "date_of_birth": ("date of birth", "birth date", "dob"),
    "gender": ("gender", "sex"),
    "blood_type": ("blood type", "blood group"),
    "address": ("address", "home address", "residential address"),
    "conditions": ("chronic conditions", "conditions", "condition"),
    "allergies": ("allergies", "allergy"),
    "medications": ("current medication", "current medications", "medication", "medications", "medicine", "medicines"),
}
STAFF_PROFILE_FIELD_ALIASES = {
    "title": ("title", "salutation"),
    "full_name": ("first name", "full name", "name"),
    "surname": ("surname", "last name"),
    "gender": ("gender", "sex"),
    "email": ("email", "e mail", "username"),
    "phone": ("phone", "cellphone", "cell phone", "mobile", "mobile number", "contact number"),
    "employee_number": ("employee number", "staff number"),
    "sa_id_number": ("sa id", "sa id number", "id number", "south african id", "identity number"),
    "date_of_birth": ("date of birth", "birth date", "dob"),
}
EMAIL_RE = re.compile(r"[\w.!#$%&'*+/=?^_`{|}~-]+@[\w.-]+\.[A-Za-z]{2,}")


def twilio_user_identity(user):
    return f"user-{user.id}"


def twilio_patient_identity(user):
    return twilio_user_identity(user)


def twilio_bot_identity():
    return current_app.config.get("TWILIO_BOT_IDENTITY") or "nmb-hlab-bot"


def user_from_twilio_identity(identity):
    identity = (identity or "").strip()
    for prefix in ("user-", "patient-"):
        if identity.startswith(prefix):
            return db.session.get(User, identity[len(prefix):])
    return None


def patient_for_user(user):
    if not user or not user.has_role("patient"):
        return None
    return Patient.query.filter_by(profile_id=user.id, deleted_at=None).first()


def _dt(value):
    return value.strftime("%Y-%m-%d %H:%M") if value else "-"


def _clean_command(text):
    text = (text or "").strip().lower()
    text = re.sub(r"[^a-z0-9#\-\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _request_number_from_text(text):
    match = re.search(r"\bREQ-\d{8}-[A-Z0-9]+\b", text or "", re.IGNORECASE)
    return match.group(0).upper() if match else None


def _find_request(patient, text):
    request_number = _request_number_from_text(text)
    query = TestRequest.query.filter_by(patient_id=patient.id)
    if request_number:
        return query.filter(func.upper(TestRequest.request_number) == request_number).first()
    command = _clean_command(text)
    if any(term in command for term in ("latest", "last", "recent", "newest")):
        return query.order_by(TestRequest.created_at.desc()).first()
    return None


def _extract_phone(text):
    match = re.search(r"(?:(?:\+|00)?\d[\d\s().-]{7,}\d)", text or "")
    if not match:
        return None
    phone = re.sub(r"[^\d+]", "", match.group(0))
    return phone if len(re.sub(r"\D", "", phone)) >= 8 else None


def _normalized_text(value):
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def _profile_alias_pairs():
    pairs = []
    for field, aliases in PROFILE_FIELD_ALIASES.items():
        for alias in aliases:
            pairs.append((field, _normalized_text(alias), alias))
    return sorted(pairs, key=lambda item: len(item[1]), reverse=True)


def _alias_to_profile_field(alias):
    normalized = _normalized_text(alias)
    for field, normalized_alias, _raw_alias in _profile_alias_pairs():
        if normalized == normalized_alias:
            return field
    return None


def _profile_alias_pattern():
    return "|".join(
        re.escape(raw_alias).replace(r"\ ", r"\s+")
        for _field, _normalized_alias, raw_alias in _profile_alias_pairs()
    )


def _clean_profile_value(value):
    value = (value or "").strip()
    value = re.sub(r"^[\"']|[\"']$", "", value)
    value = re.sub(r"\s+", " ", value).strip(" .;")
    if _normalized_text(value) in {"none", "no", "n/a", "na", "clear", "blank", "empty", "remove"}:
        return ""
    return value


def _parse_profile_updates(text):
    raw = (text or "").strip()
    if not raw:
        return {}

    alias_pattern = _profile_alias_pattern()
    if not alias_pattern:
        return {}

    marker = r"(?:to|as|is|=|:)"
    next_field = rf"(?:\s*(?:,|;|\band\b)?\s*\b(?:{alias_pattern})\b\s*{marker})"
    pair_pattern = re.compile(
        rf"\b({alias_pattern})\b\s*{marker}\s*(.*?)(?={next_field}|$)",
        re.IGNORECASE | re.DOTALL,
    )
    updates = {}
    for match in pair_pattern.finditer(raw):
        field = _alias_to_profile_field(match.group(1))
        value = _clean_profile_value(match.group(2))
        if field:
            updates[field] = value

    command = _clean_command(raw)
    if re.search(r"\b(clear|reset)\b", command) or re.search(r"\bremove all\b", command):
        for field, normalized_alias, _raw_alias in _profile_alias_pairs():
            if re.search(rf"\b{re.escape(normalized_alias)}\b", command):
                updates.setdefault(field, "")

    return updates


def _has_profile_field_reference(command):
    for _field, normalized_alias, _raw_alias in _profile_alias_pairs():
        if re.search(rf"\b{re.escape(normalized_alias)}\b", command):
            return True
    return False


def _has_protected_profile_reference(command):
    protected_aliases = (
        "email",
        "e mail",
        "mrn",
        "medical record number",
        "profile picture",
        "avatar",
        "photo",
    )
    return any(re.search(rf"\b{re.escape(alias)}\b", command) for alias in protected_aliases)


def _profile_update_requested(text, command):
    if _parse_profile_updates(text):
        return True
    actions = ("update", "change", "set", "edit", "correct", "replace", "clear", "reset")
    return any(action in command for action in actions) and (
        "profile" in command
        or _has_profile_field_reference(command)
        or _has_protected_profile_reference(command)
    )


def _parse_date_value(value):
    value = _clean_profile_value(value)
    if not value:
        return None, None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(value, fmt).date(), None
        except ValueError:
            continue
    return None, "Use date format YYYY-MM-DD, for example 1990-05-24."


def _normalize_blood_type(value):
    value = _clean_profile_value(value)
    if not value:
        return None, None
    normalized = value.upper()
    normalized = normalized.replace("POSITIVE", "+").replace("NEGATIVE", "-")
    normalized = normalized.replace(" POS", "+").replace(" NEG", "-")
    normalized = re.sub(r"\s+", "", normalized)
    if normalized in BLOOD_TYPES:
        return normalized, None
    return None, f"Blood type must be one of: {', '.join(BLOOD_TYPES)}."


def _normalize_option(value, options, label):
    value = _clean_profile_value(value)
    if not value:
        return None, None
    normalized = _normalized_text(value)
    for option in options:
        if _normalized_text(option) == normalized:
            return option, None
    return None, f"{label} must be one of: {', '.join(options)}."


def _split_catalog_values(value):
    value = _clean_profile_value(value)
    if not value:
        return []
    value = re.sub(r"\band\b", ",", value, flags=re.IGNORECASE)
    return [part.strip(" .") for part in value.split(",") if part.strip(" .")]


def _active_catalog_rows(model):
    return (
        model.query
        .filter(model.active.is_(True), model.deleted_at.is_(None))
        .order_by(model.category, model.name)
        .all()
    )


def _match_catalog_items(model, value):
    names = _split_catalog_values(value)
    if not names:
        return [], []
    rows = _active_catalog_rows(model)
    selected = []
    problems = []
    for name in names:
        normalized = _normalized_text(name)
        exact = [row for row in rows if _normalized_text(row.name) == normalized]
        if exact:
            match = exact[0]
        else:
            partial = [row for row in rows if normalized in _normalized_text(row.name)]
            if len(partial) == 1:
                match = partial[0]
            elif len(partial) > 1:
                problems.append(f"'{name}' matches more than one catalogue item.")
                continue
            else:
                problems.append(f"'{name}' is not in the active catalogue.")
                continue
        if all(existing.id != match.id for existing in selected):
            selected.append(match)
    return selected, problems


def _app_link(endpoint, label, **values):
    path = url_for(endpoint, **values)
    base = (current_app.config.get("APP_BASE_URL") or "").rstrip("/")
    if base:
        href = f"{base}/{path.lstrip('/')}"
    elif has_request_context():
        href = urllib.parse.urljoin(request.host_url, path.lstrip("/"))
    else:
        href = path
    return {"label": label, "url": href}


def _role_label(user):
    role = user.primary_role or "workspace"
    return role.replace("_", " ").title()


def _role_endpoint(user, kind):
    role = user.primary_role
    routes = {
        "admin": {
            "dashboard": "admin.dashboard",
            "notifications": "admin.notifications",
            "reports": "admin.reports",
            "users": "admin.users",
            "requests": "admin.requests_list",
            "audit": "admin.audit",
            "profile": "admin.profile",
        },
        "lab_manager": {
            "dashboard": "manager.dashboard",
            "notifications": "manager.notifications",
            "reports": "manager.reports",
            "inventory": "manager.inventory",
            "orders": "manager.orders",
            "requests": "manager.requests_list",
            "doctors": "manager.doctors",
            "technicians": "manager.technicians",
            "profile": "manager.profile",
        },
        "doctor": {
            "dashboard": "doctor.dashboard",
            "notifications": "doctor.notifications",
            "reports": "doctor.reports",
            "requests": "doctor.requests_list",
            "patients": "doctor.patients",
            "alerts": "doctor.alerts",
            "access": "doctor.access_requests",
            "consultations": "doctor.consultations",
            "availability": "doctor.consultations",
            "profile": "doctor.profile",
        },
        "lab_technician": {
            "dashboard": "technician.dashboard",
            "notifications": "technician.notifications",
            "reports": "technician.reports",
            "verify": "technician.verify_list",
            "profile": "technician.profile",
        },
        "patient": {
            "dashboard": "patient.dashboard",
            "notifications": "patient.notifications",
            "reports": "patient.reports",
            "requests": "patient.requests_list",
            "profile": "patient.profile",
            "results": "patient.results",
            "consultations": "patient.consultations",
            "availability": "patient.consultations",
        },
    }
    return routes.get(role, {}).get(kind)


def _role_link(user, kind, label, **values):
    endpoint = _role_endpoint(user, kind)
    return _app_link(endpoint, label, **values) if endpoint else None


def _links(*items):
    return [item for item in items if item]


def _parse_email_request(text):
    raw = (text or "").strip()
    recipient_match = re.search(r"\bto\s+(" + EMAIL_RE.pattern + r")\b", raw, re.IGNORECASE)
    if not recipient_match:
        recipient_match = EMAIL_RE.search(raw)
    recipient = recipient_match.group(1 if recipient_match.lastindex else 0) if recipient_match else ""

    subject = ""
    subject_match = re.search(
        r"\bsubject\s*(?:is|:|=)?\s*(.*?)(?=\s+\b(?:body|message|content)\b\s*(?:is|:|=)?|$)",
        raw,
        re.IGNORECASE | re.DOTALL,
    )
    if subject_match:
        subject = subject_match.group(1).strip(" .;\n")

    body = ""
    body_match = re.search(
        r"\b(?:body|message|content)\s*(?:is|:|=)?\s*(.+)$",
        raw,
        re.IGNORECASE | re.DOTALL,
    )
    if body_match:
        body = body_match.group(1).strip()

    return recipient, subject, body


def _email_help_reply():
    return ChatbotReply(
        body=(
            "To send an email, include the recipient, subject, and message.\n\n"
            "Example:\n"
            "send email to name@example.com subject Appointment update "
            "message Your laboratory request has been updated."
        ),
        links=[],
    )


def _send_email_reply(user, text):
    recipient, subject, body = _parse_email_request(text)
    if not recipient or not EMAIL_RE.fullmatch(recipient) or not subject or not body:
        return _email_help_reply()

    sent = send_portal_email([recipient], subject, body)
    log_audit(
        user.id,
        "chatbot_send_email",
        "email",
        None,
        {"recipient": recipient, "subject": subject, "sent": bool(sent)},
    )
    db.session.commit()
    if not sent:
        return ChatbotReply(
            body="I could not send the email. Please check the email settings or try again later.",
            links=[],
        )
    return ChatbotReply(
        body=f"Done. I sent the email to {recipient}.",
        links=[],
    )


def _send_email_requested(command):
    return "email" in command and bool(re.search(r"\b(send|mail|message)\b", command))


def _role_alias_map():
    aliases = {
        "admin": "admin",
        "administrator": "admin",
        "lab manager": "lab_manager",
        "manager": "lab_manager",
        "doctor": "doctor",
        "lab technician": "lab_technician",
        "technician": "lab_technician",
        "patient": "patient",
    }
    for role in ROLES:
        aliases[role] = role
        aliases[role.replace("_", " ")] = role
        aliases[ROLE_LABELS.get(role, role).lower()] = role
    return aliases


def _role_aliases_for(role):
    return [alias for alias, value in _role_alias_map().items() if value == role]


def _parse_role_from_admin_text(text):
    scrubbed = EMAIL_RE.sub(" ", text or "")
    command = _clean_command(scrubbed)
    for alias, role in sorted(_role_alias_map().items(), key=lambda item: len(item[0]), reverse=True):
        if re.search(rf"\b{re.escape(alias)}\b", command):
            return role
    return None


def _admin_user_action_from_command(command):
    if re.search(r"\bunblock\b", command):
        return "unblock"
    if re.search(r"\bblock\b", command):
        return "block"
    if re.search(r"\breset\b", command) and re.search(r"\bpassword\b", command):
        return "reset_password"
    if re.search(r"\brole\b", command) and any(re.search(rf"\b{term}\b", command) for term in ("assign", "set", "change", "make")):
        return "assign_role"
    if re.search(r"\bmake\b", command) and _parse_role_from_admin_text(command):
        return "assign_role"
    return None


def _admin_action_help_reply():
    roles = ", ".join(ROLE_LABELS[role] for role in ROLES)
    return ChatbotReply(
        body=(
            "Tell me the action and the user. Email is the safest target.\n\n"
            "Examples:\n"
            "- block user name@example.com\n"
            "- unblock user name@example.com\n"
            "- reset password for name@example.com\n"
            "- assign role Doctor to name@example.com\n\n"
            f"Available roles: {roles}."
        ),
        links=[_app_link("admin.users", "Open users")],
    )


def _admin_action_target_query(text, role=None):
    email_match = EMAIL_RE.search(text or "")
    if email_match:
        return email_match.group(0).lower()

    query = text or ""
    if role:
        for alias in sorted(_role_aliases_for(role), key=len, reverse=True):
            query = re.sub(rf"\b{re.escape(alias)}\b", " ", query, flags=re.IGNORECASE)
    query = re.sub(
        r"\b(block|unblock|reset|temporary|password|assign|set|change|make|role|user|account|for|of|to|as|please|the|a|an)\b",
        " ",
        query,
        flags=re.IGNORECASE,
    )
    query = re.sub(r"[^A-Za-z0-9@._+\-\s]", " ", query)
    return re.sub(r"\s+", " ", query).strip()


def _admin_user_summary(user):
    role = ROLE_LABELS.get(user.primary_role, user.primary_role or "Awaiting role")
    status = "blocked" if user.is_blocked else "deactivated" if user.is_deactivated else "active"
    return f"{user.full_name or '-'} <{user.email}> - {role}, {status}"


def _resolve_admin_target_user(text, role=None):
    query = _admin_action_target_query(text, role=role)
    if not query:
        return None, _admin_action_help_reply()

    email_match = EMAIL_RE.fullmatch(query)
    users = (
        User.query
        .options(selectinload(User.user_roles))
        .filter(User.deleted_at.is_(None))
        .all()
    )

    normalized_query = _normalized_text(query)
    digits = "".join(ch for ch in query if ch.isdigit())

    def values_for(user):
        full_name = _normalized_text(" ".join(
            part for part in (user.full_name, user.surname) if part
        ))
        return {
            _normalized_text(user.email),
            _normalized_text(user.id),
            _normalized_text(user.id[:8]),
            _normalized_text(user.employee_number),
            _normalized_text(user.sa_id_number),
            _normalized_text(user.phone),
            _normalized_text(user.full_name),
            _normalized_text(user.surname),
            full_name,
        }

    if email_match:
        for user in users:
            if _normalized_text(user.email) == normalized_query:
                return user, None
        return None, ChatbotReply(
            body=f"I could not find an active user with email {query}.",
            links=[_app_link("admin.users", "Open users")],
        )

    exact_matches = [user for user in users if normalized_query in values_for(user)]
    if len(exact_matches) == 1:
        return exact_matches[0], None

    matches = exact_matches or [
        user for user in users
        if any(normalized_query and normalized_query in value for value in values_for(user))
        or (digits and (
            digits in "".join(ch for ch in (user.sa_id_number or "") if ch.isdigit())
            or digits in "".join(ch for ch in (user.phone or "") if ch.isdigit())
        ))
    ]

    if len(matches) == 1:
        return matches[0], None
    if not matches:
        return None, ChatbotReply(
            body=f"I could not find an active user matching '{query}'. Use the user's email address if possible.",
            links=[_app_link("admin.users", "Open users")],
        )

    suggestions = "\n".join(f"- {_admin_user_summary(user)}" for user in matches[:5])
    extra = "" if len(matches) <= 5 else f"\n...and {len(matches) - 5} more."
    return None, ChatbotReply(
        body=(
            "I found more than one matching user. Send the command again with the exact email address.\n\n"
            f"{suggestions}{extra}"
        ),
        links=[_app_link("admin.users", "Open users")],
    )


def _admin_users_link(actor):
    return _links(_role_link(actor, "users", "Open users"))


def _admin_block_user_reply(actor, target):
    if target.id == actor.id:
        return ChatbotReply(body="I cannot block your own admin account.", links=_admin_users_link(actor))
    if target.is_blocked:
        return ChatbotReply(
            body=f"{target.email} is already blocked.",
            links=_admin_users_link(actor),
        )
    target.is_blocked = True
    log_audit(actor.id, "block_user", "user", target.id)
    db.session.commit()
    return ChatbotReply(body=f"Done. {target.email} has been blocked.", links=_admin_users_link(actor))


def _admin_unblock_user_reply(actor, target):
    if not target.is_blocked:
        return ChatbotReply(
            body=f"{target.email} is not blocked.",
            links=_admin_users_link(actor),
        )
    target.is_blocked = False
    log_audit(actor.id, "unblock_user", "user", target.id)
    db.session.commit()
    return ChatbotReply(body=f"Done. {target.email} has been unblocked.", links=_admin_users_link(actor))


def _admin_reset_password_reply(actor, target):
    new_password = secrets.token_urlsafe(10) + "A1!"
    target.set_password(new_password)
    target.must_change_password = True
    target.temp_password = new_password
    log_audit(actor.id, "reset_password", "user", target.id)
    db.session.commit()
    sent = send_portal_email(
        [target.email],
        "Your NMB-HLab temporary password",
        (
            f"Hello {target.full_name or target.email},\n\n"
            "Your NMB-HLab password has been reset by an administrator.\n\n"
            f"Temporary password: {new_password}\n\n"
            "For security, you will be asked to choose a new password the next time you sign in.\n\n"
            "- NMB-HLab"
        ),
    )
    if sent:
        body = f"Done. I reset the password for {target.email} and emailed the temporary password."
    else:
        body = (
            f"Done. I reset the password for {target.email}, but email is not available.\n\n"
            f"Temporary password: {new_password}"
        )
    return ChatbotReply(body=body, links=_admin_users_link(actor))


def _admin_assign_role_reply(actor, target, role):
    if role not in ROLES:
        return _admin_action_help_reply()
    if target.id == actor.id and role != "admin":
        return ChatbotReply(
            body="I will not change your own admin role from chat because it could remove your access.",
            links=_admin_users_link(actor),
        )
    if target.primary_role == role:
        return ChatbotReply(
            body=f"{target.email} is already {ROLE_LABELS[role]}.",
            links=_admin_users_link(actor),
        )

    UserRole.query.filter_by(user_id=target.id).delete()
    db.session.add(UserRole(user_id=target.id, role=role))
    if role == "patient" and not target.patient_record:
        db.session.add(Patient(
            profile_id=target.id,
            mrn="MRN-" + target.id[:8],
            full_name=target.full_name or target.email,
            email=target.email,
        ))
    notify(
        target.id,
        "Your access has been granted",
        f"You are now a {ROLE_LABELS[role]}. Sign in to access your dashboard.",
        "/app",
    )
    log_audit(actor.id, "assign_role", "user", target.id, {"role": role})
    db.session.commit()
    return ChatbotReply(
        body=f"Done. {target.email} is now {ROLE_LABELS[role]}.",
        links=_admin_users_link(actor),
    )


def _admin_user_action_reply(actor, text, command):
    action = _admin_user_action_from_command(command)
    if not action:
        return None

    role = _parse_role_from_admin_text(text) if action == "assign_role" else None
    if action == "assign_role" and not role:
        return _admin_action_help_reply()

    target, error_reply = _resolve_admin_target_user(text, role=role)
    if error_reply:
        return error_reply

    if action == "block":
        return _admin_block_user_reply(actor, target)
    if action == "unblock":
        return _admin_unblock_user_reply(actor, target)
    if action == "reset_password":
        return _admin_reset_password_reply(actor, target)
    if action == "assign_role":
        return _admin_assign_role_reply(actor, target, role)
    return None


def _help_reply():
    return ChatbotReply(
        body=(
            "I can help with patient portal information.\n\n"
            "Try:\n"
            "- profile\n"
            "- set title to Ms\n"
            "- update profile phone to 0712345678\n"
            "- set blood type to O+\n"
            "- set address to 12 Main Road\n"
            "- set allergies to Penicillin, Latex\n"
            "- request status\n"
            "- latest results\n"
            "- generate report\n"
            "- access requests\n"
            "- notifications\n"
            "- mark notifications read\n"
            "- send email to name@example.com subject Hello message Your message\n"
            "- book a test\n\n"
            "Editable profile fields: title, phone, ID number, date of birth, gender, "
            "blood type, address, chronic conditions, allergies, and medications.\n\n"
            "I can summarize portal data, but I cannot give medical advice. "
            "For clinical questions, contact your doctor or the laboratory."
        ),
        links=[],
    )


def _profile_reply(patient):
    conditions = ", ".join(item.name for item in patient.conditions) or "-"
    allergies = ", ".join(item.name for item in patient.allergy_list) or "-"
    medications = ", ".join(item.name for item in patient.medications) or "-"
    body = (
        f"Patient profile\n"
        f"Name: {patient.full_name or '-'}\n"
        f"Title: {getattr(patient.profile, 'title', None) or '-'}\n"
        f"MRN: {patient.mrn or '-'}\n"
        f"Email: {patient.email or '-'}\n"
        f"Phone: {patient.phone or '-'}\n"
        f"ID number: {patient.id_number or '-'}\n"
        f"Date of birth: {patient.date_of_birth or '-'}\n"
        f"Gender: {patient.gender or '-'}\n"
        f"Blood type: {patient.blood_type or '-'}\n"
        f"Address: {patient.address or '-'}\n"
        f"Chronic conditions: {conditions}\n"
        f"Allergies: {allergies}\n"
        f"Current medication: {medications}"
    )
    return ChatbotReply(body=body, links=[_app_link("patient.profile", "Open profile")])


def _request_status_reply(patient):
    status_rows = dict(
        db.session.query(TestRequest.status, func.count(TestRequest.id))
        .filter(TestRequest.patient_id == patient.id)
        .group_by(TestRequest.status)
        .all()
    )
    recent = (
        TestRequest.query
        .filter_by(patient_id=patient.id)
        .order_by(TestRequest.created_at.desc())
        .limit(5)
        .all()
    )
    if not recent:
        return ChatbotReply(
            body="I could not find any laboratory requests on your profile yet.",
            links=[_app_link("patient.requests_list", "Open my requests")],
        )
    counts = ", ".join(
        f"{status.replace('_', ' ')}: {count}"
        for status, count in sorted(status_rows.items())
    )
    lines = ["Request status summary", counts or "No status counts available.", "", "Recent requests:"]
    for req in recent:
        lines.append(
            f"- {req.request_number}: {req.status.replace('_', ' ').title()} "
            f"({req.priority.title()}, created {_dt(req.created_at)})"
        )
    return ChatbotReply(
        body="\n".join(lines),
        links=[_app_link("patient.requests_list", "Open my requests")],
    )


def _request_detail_reply(patient, text):
    found = _find_request(patient, text)
    req = (
        TestRequest.query
        .options(selectinload(TestRequest.items).joinedload(TestRequestItem.test))
        .filter_by(id=found.id)
        .first()
        if found else None
    )
    if not req:
        return ChatbotReply(
            body="Tell me the request number, for example: status for REQ-20260703-ABC123.",
            links=[_app_link("patient.requests_list", "Open my requests")],
        )

    lines = [
        f"Request {req.request_number}",
        f"Status: {req.status.replace('_', ' ').title()}",
        f"Priority: {req.priority.title()}",
        f"Created: {_dt(req.created_at)}",
    ]
    if req.released_at:
        lines.append(f"Released: {_dt(req.released_at)}")
    if req.doctor:
        lines.append(f"Doctor: {req.doctor.full_name or req.doctor.email}")
    if req.items:
        lines.extend(["", "Tests:"])
        for item in req.items[:8]:
            lines.append(f"- {item.test.code} {item.test.name}: {item.status.replace('_', ' ').title()}")
    links = [_app_link("patient.request_pdf", "Download request PDF", request_id=req.id)]
    if req.status == "released":
        links.insert(0, _app_link("patient.result_pdf", "Download results PDF", request_id=req.id))
    return ChatbotReply(body="\n".join(lines), links=links)


def _request_cancellation_reply(patient, text):
    req = _find_request(patient, text)
    request_label = f" {req.request_number}" if req else ""
    return ChatbotReply(
        body=(
            f"Patients cannot cancel submitted laboratory requests{request_label} "
            "from the portal assistant. Please contact your doctor or the laboratory "
            "if this request needs to be changed."
        ),
        links=[
            _app_link("patient.requests_list", "Open my requests"),
            _app_link("messages.index", "Message care team"),
        ],
    )


def _latest_results_reply(patient):
    req = (
        TestRequest.query
        .options(selectinload(TestRequest.items).joinedload(TestRequestItem.test))
        .filter_by(patient_id=patient.id, status="released")
        .order_by(TestRequest.released_at.desc(), TestRequest.updated_at.desc())
        .first()
    )
    if not req:
        return ChatbotReply(
            body="I could not find released results on your profile yet.",
            links=[_app_link("patient.results", "Open my results")],
        )

    lines = [
        f"Latest released results: {req.request_number}",
        f"Released: {_dt(req.released_at or req.updated_at)}",
        "",
    ]
    for item in list(req.items)[:6]:
        flag = f" ({item.abnormal_flag.upper()})" if item.abnormal_flag else ""
        lines.append(
            f"- {item.test.code} {item.test.name}: "
            f"{format_result_value(item)} {item.test.units or ''}".strip()
            + flag
        )
    if len(req.items) > 6:
        lines.append(f"...and {len(req.items) - 6} more test(s).")
    lines.append("")
    lines.append("This is a portal summary, not medical advice.")
    return ChatbotReply(
        body="\n".join(lines),
        links=[
            _app_link("patient.result_pdf", "Download latest PDF", request_id=req.id),
            _app_link("patient.results", "Open all results"),
        ],
    )


def _abnormal_results_reply(patient):
    rows = (
        db.session.query(TestRequestItem, TestRequest)
        .join(TestRequest, TestRequest.id == TestRequestItem.request_id)
        .options(selectinload(TestRequestItem.test))
        .filter(
            TestRequest.patient_id == patient.id,
            TestRequest.status == "released",
            TestRequestItem.abnormal_flag.isnot(None),
        )
        .order_by(TestRequest.released_at.desc(), TestRequest.updated_at.desc())
        .limit(8)
        .all()
    )
    if not rows:
        return ChatbotReply(
            body="I did not find abnormal released results on your profile.",
            links=[_app_link("patient.results", "Open my results")],
        )
    lines = ["Recent abnormal released results:"]
    for item, req in rows:
        lines.append(
            f"- {req.request_number}: {item.test.code} {item.test.name} "
            f"{format_result_value(item)} {item.test.units or ''} "
            f"({item.abnormal_flag.upper()}, ref {format_reference(item.test)})".strip()
        )
    lines.append("")
    lines.append("Please contact your doctor for clinical interpretation.")
    return ChatbotReply(
        body="\n".join(lines),
        links=[_app_link("patient.results", "Open my results")],
    )


def _search_results_reply(patient, text):
    command = _clean_command(text)
    words = [
        word for word in command.split()
        if word not in {"show", "find", "my", "result", "results", "latest", "for", "test", "blood", "lab"}
    ]
    if not words:
        return _latest_results_reply(patient)
    query_text = " ".join(words)
    like = f"%{query_text}%"
    rows = (
        db.session.query(TestRequestItem, TestRequest)
        .join(TestRequest, TestRequest.id == TestRequestItem.request_id)
        .join(TestCatalog, TestCatalog.id == TestRequestItem.test_id)
        .filter(
            TestRequest.patient_id == patient.id,
            TestRequest.status == "released",
            (TestCatalog.code.ilike(like)) | (TestCatalog.name.ilike(like)),
        )
        .order_by(TestRequest.released_at.desc(), TestRequest.updated_at.desc())
        .limit(6)
        .all()
    )
    if not rows:
        return ChatbotReply(
            body=f"I could not find released results matching '{query_text}'.",
            links=[_app_link("patient.results", "Search my results")],
        )
    lines = [f"Results matching '{query_text}':"]
    for item, req in rows:
        flag = f" ({item.abnormal_flag.upper()})" if item.abnormal_flag else ""
        lines.append(
            f"- {req.request_number}: {item.test.code} {item.test.name} "
            f"{format_result_value(item)} {item.test.units or ''}".strip() + flag
        )
    return ChatbotReply(
        body="\n".join(lines),
        links=[_app_link("patient.results", "Open all results")],
    )


def _report_reply(patient):
    latest = (
        TestRequest.query
        .filter_by(patient_id=patient.id, status="released")
        .order_by(TestRequest.released_at.desc(), TestRequest.updated_at.desc())
        .first()
    )
    links = [
        _app_link("patient.reports", "Open reports"),
        _app_link("patient.reports", "Download full history PDF", format="pdf"),
    ]
    if latest:
        links.insert(0, _app_link("patient.result_pdf", "Download latest result PDF", request_id=latest.id))
    return ChatbotReply(
        body=(
            "Your secure report links are ready. These links require you to be "
            "signed in to the portal, so your medical information is not exposed "
            "through the chat transcript."
        ),
        links=links,
    )


def _access_reply(patient):
    pending = (
        AccessRequest.query
        .filter_by(patient_id=patient.id, status="pending")
        .order_by(AccessRequest.created_at.desc())
        .all()
    )
    active_grants = (
        ConsentGrant.query
        .filter_by(patient_id=patient.id, revoked_at=None)
        .order_by(ConsentGrant.granted_at.desc())
        .all()
    )
    lines = ["Access and consent summary"]
    if pending:
        lines.append("\nPending doctor access requests:")
        for item in pending[:5]:
            doctor_name = item.doctor.full_name if item.doctor else "Doctor"
            lines.append(f"- {doctor_name}: requested {_dt(item.created_at)}")
    else:
        lines.append("\nNo pending doctor access requests.")
    if active_grants:
        lines.append("\nActive grants:")
        for grant in active_grants[:5]:
            doctor_name = grant.doctor.full_name if grant.doctor else "Doctor"
            lines.append(f"- {doctor_name}: granted {_dt(grant.granted_at)}")
    else:
        lines.append("\nNo active consent grants.")
    return ChatbotReply(
        body="\n".join(lines),
        links=[
            _app_link("patient.access_requests", "Review access requests"),
            _app_link("patient.consent", "Manage consent"),
        ],
    )


def _notifications_reply(user):
    unread_count = Notification.query.filter_by(user_id=user.id, read=False).count()
    rows = (
        Notification.query
        .filter_by(user_id=user.id)
        .order_by(Notification.created_at.desc())
        .limit(5)
        .all()
    )
    lines = [f"You have {unread_count} unread notification(s)."]
    if rows:
        lines.extend(["", "Recent notifications:"])
        for row in rows:
            lines.append(f"- {row.title}: {_dt(row.created_at)}")
    return ChatbotReply(
        body="\n".join(lines),
        links=_links(_role_link(user, "notifications", "Open notifications")),
    )


def _mark_notifications_read(user):
    count = Notification.query.filter_by(user_id=user.id, read=False).update({"read": True})
    db.session.commit()
    return ChatbotReply(
        body=f"Done. I marked {count} notification(s) as read.",
        links=_links(_role_link(user, "notifications", "Open notifications")),
    )


def _profile_update_help_reply():
    return ChatbotReply(
        body=(
            "Tell me which profile field to update.\n\n"
            "Examples:\n"
            "- set title to Ms\n"
            "- update profile phone to 0712345678\n"
            "- set ID number to 8001015009087\n"
            "- set date of birth to 1990-05-24\n"
            "- set gender to female\n"
            "- set blood type to O+\n"
            "- set address to 12 Main Road\n"
            "- set chronic conditions to Hypertension, Diabetes\n"
            "- set allergies to Penicillin, Latex\n"
            "- set medications to Metformin\n"
            "- clear allergies\n\n"
            "MRN, email, and profile picture are not updated from chat."
        ),
        links=[_app_link("patient.profile", "Open profile")],
    )


def _update_profile_reply(user, patient, text):
    command = _clean_command(text)
    updates = _parse_profile_updates(text)
    if not updates:
        if _has_protected_profile_reference(command):
            return ChatbotReply(
                body=(
                    "MRN, email, and profile picture are protected profile fields. "
                    "They cannot be updated from the assistant text box."
                ),
                links=[_app_link("patient.profile", "Open profile")],
            )
        return _profile_update_help_reply()

    new_patient_values = {}
    new_user_values = {}
    new_relations = {}
    changed = []

    if "title" in updates:
        title, title_error = _normalize_option(updates["title"], TITLE_OPTIONS, "Title")
        if title_error:
            return ChatbotReply(body=title_error, links=[_app_link("patient.profile", "Open profile")])
        new_user_values["title"] = title
        if getattr(user, "title", None) != title:
            changed.append("title")

    if "phone" in updates:
        value = _clean_profile_value(updates["phone"])
        phone = _extract_phone(value) if value else None
        if value and not phone:
            return ChatbotReply(
                body="I could not read that phone number. Try: update phone to 0712345678.",
                links=[_app_link("patient.profile", "Open profile")],
            )
        new_patient_values["phone"] = phone
        new_user_values["phone"] = phone
        if patient.phone != phone or getattr(user, "phone", None) != phone:
            changed.append("phone")

    parsed_dob = None
    if "date_of_birth" in updates:
        parsed_dob, dob_error = _parse_date_value(updates["date_of_birth"])
        if dob_error:
            return ChatbotReply(body=dob_error, links=[_app_link("patient.profile", "Open profile")])

    id_update_present = "id_number" in updates
    if id_update_present:
        raw_id = _clean_profile_value(updates["id_number"])
        id_number = "".join(ch for ch in raw_id if ch.isdigit()) if raw_id else None
        if id_number:
            valid_id, id_error, dob_from_id = validate_sa_id(id_number)
            if not valid_id:
                return ChatbotReply(
                    body=id_error or "Invalid South African ID number.",
                    links=[_app_link("patient.profile", "Open profile")],
                )
            duplicate_patient = (
                Patient.query
                .filter(Patient.id_number == id_number, Patient.id != patient.id)
                .first()
            )
            duplicate_user = (
                User.query
                .filter(User.sa_id_number == id_number, User.id != user.id)
                .first()
            )
            if duplicate_patient or duplicate_user:
                return ChatbotReply(
                    body="That ID number is already linked to another profile.",
                    links=[_app_link("patient.profile", "Open profile")],
                )
            if parsed_dob and parsed_dob != dob_from_id:
                return ChatbotReply(
                    body="Date of birth does not match the ID number.",
                    links=[_app_link("patient.profile", "Open profile")],
                )
            new_patient_values["id_number"] = id_number
            new_user_values["sa_id_number"] = id_number
            new_patient_values["date_of_birth"] = dob_from_id
            if patient.id_number != id_number or getattr(user, "sa_id_number", None) != id_number:
                changed.append("ID number")
            if patient.date_of_birth != dob_from_id:
                changed.append("date of birth")
        else:
            new_patient_values["id_number"] = None
            new_user_values["sa_id_number"] = None
            if patient.id_number or getattr(user, "sa_id_number", None):
                changed.append("ID number")

    if "date_of_birth" in updates and not (id_update_present and new_patient_values.get("id_number")):
        if patient.id_number and not id_update_present:
            valid_id, _id_error, dob_from_id = validate_sa_id(patient.id_number)
            if valid_id and parsed_dob != dob_from_id:
                return ChatbotReply(
                    body="Date of birth must match your ID number. Update the ID number first if it is wrong.",
                    links=[_app_link("patient.profile", "Open profile")],
                )
        new_patient_values["date_of_birth"] = parsed_dob
        if patient.date_of_birth != parsed_dob:
            changed.append("date of birth")

    if "gender" in updates:
        gender, gender_error = _normalize_option(updates["gender"], GENDER_OPTIONS, "Gender")
        if gender_error:
            return ChatbotReply(body=gender_error, links=[_app_link("patient.profile", "Open profile")])
        new_patient_values["gender"] = gender
        new_user_values["gender"] = gender
        if patient.gender != gender or getattr(user, "gender", None) != gender:
            changed.append("gender")

    if "blood_type" in updates:
        blood_type, blood_error = _normalize_blood_type(updates["blood_type"])
        if blood_error:
            return ChatbotReply(body=blood_error, links=[_app_link("patient.profile", "Open profile")])
        new_patient_values["blood_type"] = blood_type
        if patient.blood_type != blood_type:
            changed.append("blood type")

    if "address" in updates:
        address = _clean_profile_value(updates["address"]) or None
        new_patient_values["address"] = address
        if patient.address != address:
            changed.append("address")

    catalog_fields = {
        "conditions": (Condition, "conditions", "chronic conditions"),
        "allergies": (Allergy, "allergy_list", "allergies"),
        "medications": (Medication, "medications", "medications"),
    }
    for field, (model, relation_name, label) in catalog_fields.items():
        if field not in updates:
            continue
        items, problems = _match_catalog_items(model, updates[field])
        if problems:
            return ChatbotReply(
                body=(
                    f"I could not update {label}:\n"
                    + "\n".join(f"- {problem}" for problem in problems)
                    + "\n\nOpen the profile page to choose from the available catalogue."
                ),
                links=[_app_link("patient.profile", "Open profile")],
            )
        new_relations[relation_name] = items
        old_ids = {item.id for item in getattr(patient, relation_name)}
        new_ids = {item.id for item in items}
        if old_ids != new_ids:
            changed.append(label)

    if not changed:
        return ChatbotReply(
            body="No profile changes were needed. Those fields already have those values.",
            links=[_app_link("patient.profile", "Open profile")],
        )

    for attr, value in new_patient_values.items():
        setattr(patient, attr, value)
    for attr, value in new_user_values.items():
        setattr(user, attr, value)
    for relation_name, items in new_relations.items():
        setattr(patient, relation_name, items)

    log_audit(
        user.id,
        "chatbot_update_profile",
        "patient",
        patient.id,
        {"fields": sorted(set(changed))},
    )
    db.session.commit()
    return ChatbotReply(
        body="Done. I updated: " + ", ".join(sorted(set(changed))) + ".",
        links=[_app_link("patient.profile", "Open profile")],
    )


def _staff_profile_alias_pairs():
    pairs = []
    for field, aliases in STAFF_PROFILE_FIELD_ALIASES.items():
        for alias in aliases:
            pairs.append((field, _normalized_text(alias), alias))
    return sorted(pairs, key=lambda item: len(item[1]), reverse=True)


def _staff_alias_to_profile_field(alias):
    normalized = _normalized_text(alias)
    for field, normalized_alias, _raw_alias in _staff_profile_alias_pairs():
        if normalized == normalized_alias:
            return field
    return None


def _staff_profile_alias_pattern():
    return "|".join(
        re.escape(raw_alias).replace(r"\ ", r"\s+")
        for _field, _normalized_alias, raw_alias in _staff_profile_alias_pairs()
    )


def _parse_staff_profile_updates(text):
    raw = (text or "").strip()
    alias_pattern = _staff_profile_alias_pattern()
    if not raw or not alias_pattern:
        return {}

    marker = r"(?:to|as|is|=|:)"
    next_field = rf"(?:\s*(?:,|;|\band\b)?\s*\b(?:{alias_pattern})\b\s*{marker})"
    pair_pattern = re.compile(
        rf"\b({alias_pattern})\b\s*{marker}\s*(.*?)(?={next_field}|$)",
        re.IGNORECASE | re.DOTALL,
    )
    updates = {}
    for match in pair_pattern.finditer(raw):
        field = _staff_alias_to_profile_field(match.group(1))
        value = _clean_profile_value(match.group(2))
        if field:
            updates[field] = value

    command = _clean_command(raw)
    if re.search(r"\b(clear|reset)\b", command) or re.search(r"\bremove all\b", command):
        for field, normalized_alias, _raw_alias in _staff_profile_alias_pairs():
            if re.search(rf"\b{re.escape(normalized_alias)}\b", command):
                updates.setdefault(field, "")
    return updates


def _has_staff_profile_field_reference(command):
    for _field, normalized_alias, _raw_alias in _staff_profile_alias_pairs():
        if re.search(rf"\b{re.escape(normalized_alias)}\b", command):
            return True
    return False


def _staff_profile_update_requested(text, command):
    if _parse_staff_profile_updates(text):
        return True
    actions = ("update", "change", "set", "edit", "correct", "replace", "clear", "reset")
    return any(action in command for action in actions) and (
        "profile" in command
        or "account" in command
        or _has_staff_profile_field_reference(command)
        or any(term in command for term in ("avatar", "photo", "profile picture"))
    )


def _staff_profile_link(user):
    return _role_link(user, "profile", "Open profile")


def _staff_profile_reply(user):
    link = _staff_profile_link(user)
    title_line = "" if user.primary_role == "doctor" else f"Title: {user.title or '-'}\n"
    body = (
        f"{_role_label(user)} profile\n"
        f"{title_line}"
        f"Name: {user.full_name or '-'}\n"
        f"Surname: {user.surname or '-'}\n"
        f"Gender: {user.gender or '-'}\n"
        f"Email: {user.email or '-'}\n"
        f"Phone: {user.phone or '-'}\n"
        f"Employee number: {user.employee_number or '-'}\n"
        f"SA ID number: {user.sa_id_number or '-'}\n"
        f"Date of birth: {user.date_of_birth or '-'}"
    )
    return ChatbotReply(body=body, links=_links(link))


def _staff_profile_update_help_reply(user):
    title_example = "" if user.primary_role == "doctor" else "- set title to Ms\n"
    return ChatbotReply(
        body=(
            "Tell me which profile field to update.\n\n"
            "Examples:\n"
            f"{title_example}"
            "- update profile phone to 0712345678\n"
            "- set first name to Nomsa\n"
            "- set surname to Dlamini\n"
            "- set gender to Female\n"
            "- set email to name@example.com\n"
            "- set employee number to ADM-001\n"
            "- set SA ID number to 8001015009087\n"
            "- set date of birth to 1990-05-24\n\n"
            "Profile pictures must be updated from the profile page."
        ),
        links=_links(_staff_profile_link(user)),
    )


def _update_staff_profile_reply(user, text):
    command = _clean_command(text)
    updates = _parse_staff_profile_updates(text)
    if not updates:
        if any(term in command for term in ("avatar", "photo", "profile picture")):
            return ChatbotReply(
                body="Profile pictures must be uploaded from the profile page.",
                links=_links(_staff_profile_link(user)),
            )
        return _staff_profile_update_help_reply(user)

    new_values = {}
    changed = []

    if "title" in updates:
        if user.primary_role == "doctor":
            return ChatbotReply(
                body="Doctor profiles do not use a title field in this system.",
                links=_links(_staff_profile_link(user)),
            )
        title, title_error = _normalize_option(updates["title"], TITLE_OPTIONS, "Title")
        if title_error:
            return ChatbotReply(body=title_error, links=_links(_staff_profile_link(user)))
        new_values["title"] = title
        if user.title != title:
            changed.append("title")

    if "full_name" in updates:
        value = _clean_profile_value(updates["full_name"])
        if not value:
            return ChatbotReply(body="First name cannot be blank.", links=_links(_staff_profile_link(user)))
        new_values["full_name"] = value
        if user.full_name != value:
            changed.append("first name")

    if "surname" in updates:
        value = _clean_profile_value(updates["surname"]) or None
        new_values["surname"] = value
        if user.surname != value:
            changed.append("surname")

    if "gender" in updates:
        gender, gender_error = _normalize_option(updates["gender"], GENDER_OPTIONS, "Gender")
        if gender_error:
            return ChatbotReply(body=gender_error, links=_links(_staff_profile_link(user)))
        new_values["gender"] = gender
        if user.gender != gender:
            changed.append("gender")

    if "email" in updates:
        email = _clean_profile_value(updates["email"]).lower()
        if not email or not EMAIL_RE.fullmatch(email):
            return ChatbotReply(body="Use a valid email address.", links=_links(_staff_profile_link(user)))
        if User.query.filter(User.email == email, User.id != user.id).first():
            return ChatbotReply(body="That email is already used by another account.", links=_links(_staff_profile_link(user)))
        new_values["email"] = email
        if user.email != email:
            changed.append("email")

    if "phone" in updates:
        value = _clean_profile_value(updates["phone"])
        phone = _extract_phone(value) if value else None
        if value and not phone:
            return ChatbotReply(
                body="I could not read that phone number. Try: update phone to 0712345678.",
                links=_links(_staff_profile_link(user)),
            )
        new_values["phone"] = phone
        if user.phone != phone:
            changed.append("phone")

    if "employee_number" in updates:
        employee_number = _clean_profile_value(updates["employee_number"]) or None
        if employee_number and User.query.filter(User.employee_number == employee_number, User.id != user.id).first():
            return ChatbotReply(body="That employee number is already used by another account.", links=_links(_staff_profile_link(user)))
        new_values["employee_number"] = employee_number
        if user.employee_number != employee_number:
            changed.append("employee number")

    parsed_dob = None
    if "date_of_birth" in updates:
        parsed_dob, dob_error = _parse_date_value(updates["date_of_birth"])
        if dob_error:
            return ChatbotReply(body=dob_error, links=_links(_staff_profile_link(user)))

    id_update_present = "sa_id_number" in updates
    if id_update_present:
        raw_id = _clean_profile_value(updates["sa_id_number"])
        sa_id_number = "".join(ch for ch in raw_id if ch.isdigit()) if raw_id else None
        if sa_id_number:
            valid_id, id_error, dob_from_id = validate_sa_id(sa_id_number)
            if not valid_id:
                return ChatbotReply(
                    body=id_error or "Invalid South African ID number.",
                    links=_links(_staff_profile_link(user)),
                )
            if User.query.filter(User.sa_id_number == sa_id_number, User.id != user.id).first():
                return ChatbotReply(body="That SA ID number is already used by another account.", links=_links(_staff_profile_link(user)))
            if parsed_dob and parsed_dob != dob_from_id:
                return ChatbotReply(body="Date of birth does not match the SA ID number.", links=_links(_staff_profile_link(user)))
            new_values["sa_id_number"] = sa_id_number
            new_values["date_of_birth"] = dob_from_id
            if user.sa_id_number != sa_id_number:
                changed.append("SA ID number")
            if user.date_of_birth != dob_from_id:
                changed.append("date of birth")
        else:
            new_values["sa_id_number"] = None
            if user.sa_id_number:
                changed.append("SA ID number")

    if "date_of_birth" in updates and not (id_update_present and new_values.get("sa_id_number")):
        current_sa_id = new_values.get("sa_id_number", user.sa_id_number)
        if current_sa_id:
            valid_id, _id_error, dob_from_id = validate_sa_id(current_sa_id)
            if valid_id and parsed_dob != dob_from_id:
                return ChatbotReply(
                    body="Date of birth must match the SA ID number. Update the SA ID number first if it is wrong.",
                    links=_links(_staff_profile_link(user)),
                )
        new_values["date_of_birth"] = parsed_dob
        if user.date_of_birth != parsed_dob:
            changed.append("date of birth")

    if not changed:
        return ChatbotReply(
            body="No profile changes were needed. Those fields already have those values.",
            links=_links(_staff_profile_link(user)),
        )

    for attr, value in new_values.items():
        setattr(user, attr, value)

    log_audit(
        user.id,
        "chatbot_update_profile",
        "user",
        user.id,
        {"fields": sorted(set(changed))},
    )
    db.session.commit()
    return ChatbotReply(
        body="Done. I updated: " + ", ".join(sorted(set(changed))) + ".",
        links=_links(_staff_profile_link(user)),
    )


def _role_help_reply(user):
    role = user.primary_role
    if role == "patient":
        return _help_reply()

    role_examples = {
        "doctor": (
            "- dashboard summary\n"
            "- request status\n"
            "- alerts\n"
            "- patients\n"
            "- reports\n"
            "- notifications\n"
            "- update profile gender to Female\n"
            "- mark notifications read\n"
            "- send email to name@example.com subject Hello message Your message"
        ),
        "lab_technician": (
            "- dashboard summary\n"
            "- work queue\n"
            "- verify queue\n"
            "- reports\n"
            "- notifications\n"
            "- update profile title to Mr\n"
            "- mark notifications read\n"
            "- send email to name@example.com subject Hello message Your message"
        ),
        "lab_manager": (
            "- dashboard summary\n"
            "- inventory\n"
            "- orders\n"
            "- doctors\n"
            "- technicians\n"
            "- reports\n"
            "- notifications\n"
            "- update profile title to Ms\n"
            "- send email to name@example.com subject Hello message Your message"
        ),
        "admin": (
            "- dashboard summary\n"
            "- users\n"
            "- block user name@example.com\n"
            "- unblock user name@example.com\n"
            "- reset password for name@example.com\n"
            "- assign role Doctor to name@example.com\n"
            "- requests\n"
            "- audit log\n"
            "- reports\n"
            "- notifications\n"
            "- profile\n"
            "- update profile phone to 0712345678\n"
            "- send email to name@example.com subject Hello message Your message"
        ),
    }
    body = (
        f"I can help with {_role_label(user)} workspace information.\n\n"
        "Try:\n"
        f"{role_examples.get(role, '- dashboard summary')}\n\n"
        "I only use information available to your signed-in role."
    )
    return ChatbotReply(body=body, links=_links(_role_link(user, "dashboard", "Open dashboard")))


def _status_counts(query):
    return dict(
        query.with_entities(TestRequest.status, func.count(TestRequest.id))
        .group_by(TestRequest.status)
        .all()
    )


def _format_counts(counts):
    if not counts:
        return "No request counts available."
    return ", ".join(
        f"{status.replace('_', ' ')}: {count}"
        for status, count in sorted(counts.items())
    )


def _admin_summary_reply(user):
    users = User.query.filter(User.deleted_at.is_(None)).all()
    pending_count = sum(1 for row in users if row.is_pending)
    blocked_count = sum(1 for row in users if row.is_blocked)
    request_counts = dict(
        db.session.query(TestRequest.status, func.count(TestRequest.id))
        .group_by(TestRequest.status)
        .all()
    )
    role_counts = dict(
        db.session.query(UserRole.role, func.count(UserRole.user_id))
        .join(User, User.id == UserRole.user_id)
        .filter(User.deleted_at.is_(None))
        .group_by(UserRole.role)
        .all()
    )
    recent_logs = AuditLog.query.order_by(AuditLog.created_at.desc()).limit(5).all()
    lines = [
        "Admin workspace summary",
        f"Active user records: {len(users)}",
        f"Pending users: {pending_count}",
        f"Blocked users: {blocked_count}",
        f"Patients: {Patient.query.filter(Patient.deleted_at.is_(None)).count()}",
        f"Requests: {_format_counts(request_counts)}",
        "",
        "Users by role:",
    ]
    for role, count in sorted(role_counts.items()):
        lines.append(f"- {role.replace('_', ' ').title()}: {count}")
    if recent_logs:
        lines.extend(["", "Recent audit activity:"])
        for row in recent_logs:
            lines.append(f"- {row.action}: {_dt(row.created_at)}")
    return ChatbotReply(
        body="\n".join(lines),
        links=_links(
            _role_link(user, "dashboard", "Open dashboard"),
            _role_link(user, "users", "Open users"),
            _role_link(user, "audit", "Open audit log"),
        ),
    )


def _manager_summary_reply(user):
    active_statuses = ("submitted", "samples_received", "in_progress", "completed", "verified", "released")
    request_counts = dict(
        db.session.query(TestRequest.status, func.count(TestRequest.id))
        .filter(TestRequest.status.in_(active_statuses))
        .group_by(TestRequest.status)
        .all()
    )
    low_stock = (
        Consumable.query
        .filter(Consumable.deleted_at.is_(None))
        .filter(Consumable.current_stock <= Consumable.reorder_level * 1.1)
        .order_by(Consumable.current_stock.asc())
        .limit(5)
        .all()
    )
    order_counts = dict(
        db.session.query(ConsumableOrder.status, func.count(ConsumableOrder.id))
        .group_by(ConsumableOrder.status)
        .all()
    )
    doctor_count = (
        User.query.join(UserRole)
        .filter(UserRole.role == "doctor", User.deleted_at.is_(None))
        .count()
    )
    technician_count = (
        User.query.join(UserRole)
        .filter(UserRole.role == "lab_technician", User.deleted_at.is_(None))
        .count()
    )
    lines = [
        "Lab manager workspace summary",
        f"Requests: {_format_counts(request_counts)}",
        f"Orders: {', '.join(f'{status}: {count}' for status, count in sorted(order_counts.items())) or 'none'}",
        f"Doctors: {doctor_count}",
        f"Technicians: {technician_count}",
        f"Suppliers: {Supplier.query.filter(Supplier.deleted_at.is_(None)).count()}",
        "",
        "Lowest stock items:",
    ]
    if low_stock:
        for item in low_stock:
            lines.append(f"- {item.name}: {item.current_stock} {item.unit}, reorder at {item.reorder_level}")
    else:
        lines.append("- No low stock items found.")
    return ChatbotReply(
        body="\n".join(lines),
        links=_links(
            _role_link(user, "dashboard", "Open dashboard"),
            _role_link(user, "inventory", "Open inventory"),
            _role_link(user, "orders", "Open orders"),
        ),
    )


def _doctor_summary_reply(user):
    base_query = TestRequest.query.filter_by(doctor_id=user.id)
    request_counts = _status_counts(base_query)
    ready_count = base_query.filter(
        TestRequest.status == "completed",
        TestRequest.items.any(),
        ~TestRequest.items.any(TestRequestItem.status != "verified"),
    ).count()
    abnormal_count = (
        TestRequestItem.query
        .join(TestRequest, TestRequest.id == TestRequestItem.request_id)
        .filter(
            TestRequest.doctor_id == user.id,
            TestRequestItem.abnormal_flag.isnot(None),
        )
        .count()
    )
    access_pending = AccessRequest.query.filter_by(doctor_id=user.id, status="pending").count()
    recent = (
        base_query
        .options(selectinload(TestRequest.patient))
        .order_by(TestRequest.created_at.desc())
        .limit(5)
        .all()
    )
    lines = [
        "Doctor workspace summary",
        f"Requests: {_format_counts(request_counts)}",
        f"Ready for release review: {ready_count}",
        f"Abnormal flagged items: {abnormal_count}",
        f"Pending patient access requests: {access_pending}",
    ]
    if recent:
        lines.extend(["", "Recent requests:"])
        for req in recent:
            patient_name = req.patient.full_name if req.patient else "-"
            lines.append(f"- {req.request_number}: {patient_name}, {req.status.replace('_', ' ').title()}")
    return ChatbotReply(
        body="\n".join(lines),
        links=_links(
            _role_link(user, "dashboard", "Open dashboard"),
            _role_link(user, "requests", "Open requests"),
            _role_link(user, "alerts", "Open alerts"),
        ),
    )


def _technician_summary_reply(user):
    assigned_ids = {
        test_id for (test_id,) in (
            db.session.query(TechnicianTest.test_id)
            .join(TestCatalog, TestCatalog.id == TechnicianTest.test_id)
            .filter(
                TechnicianTest.technician_id == user.id,
                TestCatalog.active.is_(True),
                TestCatalog.deleted_at.is_(None),
            )
            .all()
        )
    }
    assigned_count = len(assigned_ids)
    selected_count = TestRequestItem.query.filter(
        TestRequestItem.assigned_to == user.id,
        TestRequestItem.status.in_(("in_progress", "completed", "to_be_reviewed")),
    ).count()
    waiting_count = (
        TestRequestItem.query
        .join(TestRequest, TestRequest.id == TestRequestItem.request_id)
        .filter(
            TestRequestItem.test_id.in_(assigned_ids),
            TestRequestItem.assigned_to.is_(None),
            TestRequestItem.status == "submitted",
            TestRequest.status.in_(("submitted", "samples_received", "in_progress")),
        )
        .count()
        if assigned_ids else 0
    )
    verification_count = (
        TestRequestItem.query
        .filter(
            TestRequestItem.test_id.in_(assigned_ids),
            TestRequestItem.status == "completed",
        )
        .count()
        if assigned_ids else 0
    )
    review_count = TestRequestItem.query.filter_by(
        assigned_to=user.id,
        status="to_be_reviewed",
    ).count()
    lines = [
        "Technician workspace summary",
        f"Assigned test types: {assigned_count}",
        f"My active items: {selected_count}",
        f"Waiting queue items: {waiting_count}",
        f"Verification queue items: {verification_count}",
        f"Returned for review: {review_count}",
    ]
    return ChatbotReply(
        body="\n".join(lines),
        links=_links(
            _role_link(user, "dashboard", "Open dashboard"),
            _role_link(user, "verify", "Open verify queue"),
            _role_link(user, "reports", "Open reports"),
        ),
    )


def _role_summary_reply(user):
    role = user.primary_role
    if role == "admin":
        return _admin_summary_reply(user)
    if role == "lab_manager":
        return _manager_summary_reply(user)
    if role == "doctor":
        return _doctor_summary_reply(user)
    if role == "lab_technician":
        return _technician_summary_reply(user)
    if role == "patient":
        patient = patient_for_user(user)
        return _request_status_reply(patient) if patient else _role_help_reply(user)
    return ChatbotReply(
        body="I could not determine your workspace role.",
        links=[],
    )


def _role_reports_reply(user):
    link = _role_link(user, "reports", "Open reports")
    if not link:
        return _role_summary_reply(user)
    return ChatbotReply(
        body=f"Reports are available for your {_role_label(user)} workspace.",
        links=[link],
    )


def _booking_reply():
    return ChatbotReply(
        body=(
            "Laboratory test requests must be created by a doctor, because the "
            "doctor records the requested tests and sample collection details. "
            "You can use the portal to track requests and view results once they "
            "are released."
        ),
        links=[
            _app_link("patient.requests_list", "Open my requests"),
            _app_link("messages.index", "Message care team"),
        ],
    )


def _consultation_label(status):
    return (status or "unknown").replace("_", " ").title()


def _consultation_primary_link(user, consultation):
    role = user.primary_role
    if role == "patient":
        if consultation.status == "started":
            return _app_link(
                "patient.consultation_room",
                "Join live session",
                consultation_id=consultation.id,
                room_token=consultation.room_token,
            )
        if consultation.status == "accepted":
            return _app_link(
                "patient.consultation_waiting",
                "Enter waiting room",
                consultation_id=consultation.id,
                room_token=consultation.room_token,
            )
        return _app_link(
            "patient.consultation_detail",
            "Open consultation",
            consultation_id=consultation.id,
        )
    if role == "doctor" and consultation.status in ("accepted", "started"):
        return _app_link(
            "doctor.consultation_room",
            "Open consultation room",
            consultation_id=consultation.id,
            room_token=consultation.room_token,
        )
    return _role_link(user, "consultations", "Open consultations")


def _format_consultation_row(consultation, viewer_role):
    scheduled = _dt(consultation.scheduled_at) if consultation.scheduled_at else "not scheduled"
    request_number = consultation.request.request_number if consultation.request else "request"
    if viewer_role == "doctor":
        person = consultation.patient.full_name if consultation.patient else "Patient"
    else:
        person = consultation.doctor.full_name or consultation.doctor.email if consultation.doctor else "Doctor"
    preference = consultation.patient_preference.replace("_", " ") if consultation.patient_preference else "not chosen"
    return (
        f"- {request_number}: {person}, {_consultation_label(consultation.status)}, "
        f"{preference}, {scheduled}"
    )


def _consultation_query_for_user(user):
    if user.primary_role == "doctor":
        return OnlineConsultation.query.filter_by(doctor_id=user.id)
    if user.primary_role == "patient":
        patient = patient_for_user(user)
        if not patient:
            return OnlineConsultation.query.filter(OnlineConsultation.id == "__none__")
        return OnlineConsultation.query.filter_by(patient_id=patient.id)
    return OnlineConsultation.query.filter(OnlineConsultation.id == "__none__")


def _consultation_summary_reply(user):
    if user.primary_role not in ("doctor", "patient"):
        return ChatbotReply(
            body="Consultations are available from the doctor and patient workspaces.",
            links=_links(_role_link(user, "dashboard", "Open dashboard")),
        )

    query = _consultation_query_for_user(user)
    rows = (
        query
        .options(
            selectinload(OnlineConsultation.patient),
            selectinload(OnlineConsultation.doctor),
            selectinload(OnlineConsultation.request),
        )
        .order_by(OnlineConsultation.scheduled_at.desc(), OnlineConsultation.created_at.desc())
        .limit(6)
        .all()
    )
    if not rows:
        return ChatbotReply(
            body="I could not find any consultations for your workspace yet.",
            links=_links(_role_link(user, "consultations", "Open consultations")),
        )

    counts = dict(
        query
        .with_entities(OnlineConsultation.status, func.count(OnlineConsultation.id))
        .group_by(OnlineConsultation.status)
        .all()
    )
    lines = [
        f"{_role_label(user)} consultation summary",
        ", ".join(
            f"{_consultation_label(status)}: {count}"
            for status, count in sorted(counts.items())
        ) or "No consultation counts available.",
        "",
        "Recent consultations:",
    ]
    for row in rows:
        lines.append(_format_consultation_row(row, user.primary_role))

    primary = next((row for row in rows if row.status in ("started", "accepted", "invited")), rows[0])
    return ChatbotReply(
        body="\n".join(lines),
        links=_links(
            _consultation_primary_link(user, primary),
            _role_link(user, "consultations", "Open all consultations"),
        ),
    )


def _availability_reply(user):
    if user.primary_role == "doctor":
        now = datetime.now()
        upcoming = (
            DoctorAvailabilitySlot.query
            .filter(
                DoctorAvailabilitySlot.doctor_id == user.id,
                DoctorAvailabilitySlot.starts_at >= now,
            )
            .order_by(DoctorAvailabilitySlot.starts_at.asc())
            .limit(6)
            .all()
        )
        counts = dict(
            DoctorAvailabilitySlot.query
            .filter_by(doctor_id=user.id)
            .with_entities(DoctorAvailabilitySlot.status, func.count(DoctorAvailabilitySlot.id))
            .group_by(DoctorAvailabilitySlot.status)
            .all()
        )
        lines = [
            "Doctor availability summary",
            ", ".join(f"{status}: {count}" for status, count in sorted(counts.items())) or "No slots yet.",
            "",
            "Upcoming slots:",
        ]
        if upcoming:
            for slot in upcoming:
                status = "booked" if slot.booked_consultation_id else slot.status
                lines.append(f"- {_dt(slot.starts_at)} to {slot.ends_at.strftime('%H:%M')}: {status}")
        else:
            lines.append("- No upcoming slots found.")
        return ChatbotReply(
            body="\n".join(lines),
            links=_links(_role_link(user, "availability", "Manage availability")),
        )

    if user.primary_role == "patient":
        patient = patient_for_user(user)
        if not patient:
            return ChatbotReply(body="I could not find your patient profile.", links=[])
        doctor_ids = [
            doctor_id
            for (doctor_id,) in (
                OnlineConsultation.query
                .with_entities(OnlineConsultation.doctor_id)
                .filter(OnlineConsultation.patient_id == patient.id)
                .distinct()
                .all()
            )
        ]
        if not doctor_ids:
            return ChatbotReply(
                body="I could not find any doctor consultations linked to your profile yet.",
                links=_links(_role_link(user, "consultations", "Open consultations")),
            )
        upcoming = (
            DoctorAvailabilitySlot.query
            .options(selectinload(DoctorAvailabilitySlot.doctor))
            .filter(
                DoctorAvailabilitySlot.doctor_id.in_(doctor_ids),
                DoctorAvailabilitySlot.status == "open",
                DoctorAvailabilitySlot.booked_consultation_id.is_(None),
                DoctorAvailabilitySlot.starts_at >= datetime.now(),
            )
            .order_by(DoctorAvailabilitySlot.starts_at.asc())
            .limit(6)
            .all()
        )
        lines = ["Available in-person consultation times:"]
        if upcoming:
            for slot in upcoming:
                doctor = slot.doctor.full_name or slot.doctor.email if slot.doctor else "Doctor"
                location = f", {slot.location}" if slot.location else ""
                lines.append(f"- {_dt(slot.starts_at)} to {slot.ends_at.strftime('%H:%M')}: {doctor}{location}")
        else:
            lines.append("- No open times are available yet. Check consultations again later.")
        return ChatbotReply(
            body="\n".join(lines),
            links=_links(_role_link(user, "availability", "Open consultation calendar")),
        )

    return ChatbotReply(
        body="Availability is only used by doctor and patient consultation workflows.",
        links=_links(_role_link(user, "dashboard", "Open dashboard")),
    )


ROLE_INTENTS = {
    "patient": {
        "help",
        "profile",
        "request_status",
        "request_detail",
        "latest_results",
        "abnormal_results",
        "search_results",
        "reports",
        "access",
        "notifications",
        "consultations",
        "availability",
        "booking_help",
        "dashboard_summary",
        "unknown",
    },
    "doctor": {
        "help",
        "profile",
        "requests_summary",
        "dashboard_summary",
        "alerts",
        "patients",
        "reports",
        "notifications",
        "consultations",
        "availability",
        "unknown",
    },
    "lab_technician": {
        "help",
        "profile",
        "work_queue",
        "verify_queue",
        "dashboard_summary",
        "reports",
        "notifications",
        "unknown",
    },
    "lab_manager": {
        "help",
        "profile",
        "dashboard_summary",
        "inventory",
        "orders",
        "doctors",
        "technicians",
        "reports",
        "notifications",
        "unknown",
    },
    "admin": {
        "help",
        "profile",
        "dashboard_summary",
        "users",
        "requests_summary",
        "audit",
        "reports",
        "notifications",
        "unknown",
    },
}


def _local_llm_enabled():
    return bool(
        current_app.config.get("LOCAL_LLM_ENABLED")
        and current_app.config.get("LOCAL_LLM_API_URL")
    )


def _local_llm_in_cooldown():
    return time.monotonic() < float(current_app.config.get("_LOCAL_LLM_DISABLED_UNTIL", 0) or 0)


def _record_local_llm_failure(exc):
    cooldown = float(current_app.config.get("LOCAL_LLM_FAILURE_COOLDOWN_SECONDS", 20) or 20)
    current_app.config["_LOCAL_LLM_DISABLED_UNTIL"] = time.monotonic() + max(0.0, cooldown)
    current_app.logger.info("Local LLM intent router unavailable: %s", exc)


def _local_llm_endpoint(base_url, provider):
    base_url = (base_url or "").strip().rstrip("/")
    provider = (provider or "auto").lower()
    if not base_url:
        return "", "none"
    if provider in {"openai", "lmstudio", "openai-compatible"}:
        if base_url.endswith("/chat/completions"):
            return base_url, "openai"
        return f"{base_url}/chat/completions" if base_url.endswith("/v1") else f"{base_url}/v1/chat/completions", "openai"
    if provider == "ollama":
        return base_url if base_url.endswith("/api/chat") else f"{base_url}/api/chat", "ollama"
    if "/v1" in base_url or base_url.endswith("/chat/completions"):
        if base_url.endswith("/chat/completions"):
            return base_url, "openai"
        return f"{base_url}/chat/completions", "openai"
    return base_url if base_url.endswith("/api/chat") else f"{base_url}/api/chat", "ollama"


def _extract_json_object(raw):
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        pass
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return {}
    try:
        return json.loads(match.group(0))
    except ValueError:
        return {}


def _intent_prompt(user, text):
    role = user.primary_role or "unknown"
    intents = sorted(ROLE_INTENTS.get(role, {"help", "unknown"}))
    return (
        "You are a secure intent router for a medical lab portal assistant.\n"
        "Return JSON only. Do not answer the user.\n"
        f"Signed-in role: {role}.\n"
        f"Allowed intents: {', '.join(intents)}.\n"
        "Pick only one allowed intent. If the user asks for medical diagnosis, "
        "treatment, or anything outside the role, use unknown.\n"
        "Schema: {\"intent\":\"one_allowed_intent\",\"confidence\":0.0_to_1.0}.\n"
        f"User message: {text[:900]}"
    )


def _local_llm_intent(user, text):
    if not _local_llm_enabled() or _local_llm_in_cooldown():
        return None

    endpoint, provider = _local_llm_endpoint(
        current_app.config.get("LOCAL_LLM_API_URL"),
        current_app.config.get("LOCAL_LLM_PROVIDER"),
    )
    if not endpoint:
        return None

    model = current_app.config.get("LOCAL_LLM_MODEL") or "llama3.1:8b"
    timeout = float(current_app.config.get("LOCAL_LLM_TIMEOUT_SECONDS", 1.8) or 1.8)
    prompt = _intent_prompt(user, text)
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if provider == "openai":
        payload = {
            "model": model,
            "temperature": 0,
            "max_tokens": 80,
            "messages": [
                {"role": "system", "content": "Return strict JSON only."},
                {"role": "user", "content": prompt},
            ],
        }
    else:
        payload = {
            "model": model,
            "stream": False,
            "options": {"temperature": 0},
            "messages": [{"role": "user", "content": prompt}],
        }

    req = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8") or "{}")
    except Exception as exc:
        _record_local_llm_failure(exc)
        return None

    if provider == "openai":
        content = (
            data.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
        )
    else:
        content = data.get("message", {}).get("content", "")

    parsed = _extract_json_object(content)
    intent = _normalized_text(parsed.get("intent", "")).replace(" ", "_")
    try:
        confidence = float(parsed.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0
    if confidence < 0.45:
        return None
    if intent not in ROLE_INTENTS.get(user.primary_role, set()):
        return None
    return intent


def _role_reply_for_intent(user, intent, text):
    if not intent or intent == "unknown":
        return None
    role = user.primary_role
    if intent == "help":
        return _role_help_reply(user)
    if intent == "profile":
        if role == "patient":
            patient = patient_for_user(user)
            return _profile_reply(patient) if patient else None
        return _staff_profile_reply(user) if _staff_profile_link(user) else None
    if intent == "notifications":
        return _notifications_reply(user)
    if intent == "reports":
        return _role_reports_reply(user)
    if intent in {"dashboard_summary", "requests_summary", "work_queue", "verify_queue", "inventory", "orders", "users", "patients", "doctors", "technicians", "alerts", "audit"}:
        return _role_summary_reply(user)
    if intent == "consultations":
        return _consultation_summary_reply(user)
    if intent == "availability":
        return _availability_reply(user)
    if role == "patient":
        patient = patient_for_user(user)
        if not patient:
            return None
        if intent == "request_status":
            return _request_status_reply(patient)
        if intent == "request_detail":
            return _request_detail_reply(patient, text)
        if intent == "latest_results":
            return _latest_results_reply(patient)
        if intent == "abnormal_results":
            return _abnormal_results_reply(patient)
        if intent == "search_results":
            return _search_results_reply(patient, text)
        if intent == "access":
            return _access_reply(patient)
        if intent == "booking_help":
            return _booking_reply()
    return None


def _llm_assisted_reply(user, text):
    intent = _local_llm_intent(user, text)
    return _role_reply_for_intent(user, intent, text)


def handle_patient_chat_message(user, text):
    patient = patient_for_user(user)
    if not patient:
        return ChatbotReply(
            body="I could not find an active patient profile for this chat identity.",
            links=[],
        )

    command = _clean_command(text)
    if not command or command in {"hi", "hello", "hey", "help", "menu", "start"}:
        return _help_reply()
    if "mark" in command and "notification" in command and "read" in command:
        return _mark_notifications_read(user)
    if _profile_update_requested(text, command):
        return _update_profile_reply(user, patient, text)
    if "cancel" in command and "request" in command:
        return _request_cancellation_reply(patient, text)
    if any(term in command for term in ("profile", "my info", "my details", "mrn")):
        return _profile_reply(patient)
    if any(term in command for term in ("access", "consent", "share", "doctor request")):
        return _access_reply(patient)
    if any(term in command for term in ("consultation", "appointment", "meeting", "video", "online session", "waiting room")):
        return _consultation_summary_reply(user)
    if any(term in command for term in ("availability", "calendar", "slot", "available time")):
        return _availability_reply(user)
    if "request" in command and _find_request(patient, text):
        return _request_detail_reply(patient, text)
    if any(term in command for term in ("status", "request", "track", "progress")):
        return _request_status_reply(patient)
    if any(term in command for term in ("abnormal", "high result", "low result", "flagged")):
        return _abnormal_results_reply(patient)
    if any(term in command for term in ("find result", "show result", "result for", "results for")):
        return _search_results_reply(patient, text)
    if any(term in command for term in ("latest result", "results", "lab result", "blood result")):
        return _latest_results_reply(patient)
    if any(term in command for term in ("report", "pdf", "download")):
        return _report_reply(patient)
    if any(term in command for term in ("notification", "alert")):
        return _notifications_reply(user)
    if any(term in command for term in ("book", "booking", "appointment", "test")):
        return _booking_reply()
    llm_reply = _llm_assisted_reply(user, text)
    if llm_reply:
        return llm_reply
    return ChatbotReply(
        body=(
            "I did not understand that yet. Send 'help' to see what I can do. "
            "For medical interpretation, contact your doctor or the laboratory."
        ),
        links=[],
    )


def handle_chat_message(user, text):
    if not user or user.is_pending:
        return ChatbotReply(
            body="I could not determine an active workspace role for this assistant session.",
            links=[],
        )

    command = _clean_command(text)
    if not command or command in {"hi", "hello", "hey", "help", "menu", "start"}:
        return _role_help_reply(user)

    if _send_email_requested(command):
        return _send_email_reply(user, text)
    if "mark" in command and "notification" in command and "read" in command:
        return _mark_notifications_read(user)
    if any(term in command for term in ("notification", "alert")) and user.primary_role != "doctor":
        return _notifications_reply(user)

    if user.has_role("patient") and user.primary_role == "patient":
        return handle_patient_chat_message(user, text)

    if user.primary_role == "admin":
        admin_action_reply = _admin_user_action_reply(user, text, command)
        if admin_action_reply:
            return admin_action_reply

    if user.primary_role != "patient" and _staff_profile_link(user) and _staff_profile_update_requested(text, command):
        return _update_staff_profile_reply(user, text)
    if any(term in command for term in ("profile", "my info", "my details", "account details")):
        if _staff_profile_link(user):
            return _staff_profile_reply(user)

    if any(term in command for term in ("report", "reports", "pdf", "download")):
        return _role_reports_reply(user)
    if any(term in command for term in ("consultation", "appointment", "meeting", "video", "online session", "waiting room")):
        return _consultation_summary_reply(user)
    if any(term in command for term in ("availability", "calendar", "slot", "available time")):
        return _availability_reply(user)
    if any(term in command for term in (
        "dashboard",
        "summary",
        "status",
        "request",
        "requests",
        "queue",
        "work",
        "inventory",
        "stock",
        "order",
        "orders",
        "users",
        "patients",
        "doctors",
        "technicians",
        "audit",
        "alerts",
        "verify",
    )):
        return _role_summary_reply(user)
    if any(term in command for term in ("notification", "alert")):
        return _notifications_reply(user)

    llm_reply = _llm_assisted_reply(user, text)
    if llm_reply:
        return llm_reply

    return ChatbotReply(
        body=(
            f"I did not understand that yet for the {_role_label(user)} workspace. "
            "Send 'help' to see what I can do."
        ),
        links=_links(_role_link(user, "dashboard", "Open dashboard")),
    )


def format_reply_for_twilio(reply):
    lines = [reply.body]
    if reply.links:
        lines.append("")
        lines.append("Links:")
        for link in reply.links:
            lines.append(f"- {link['label']}: {link['url']}")
    return "\n".join(lines)


def _twilio_credentials():
    return (
        current_app.config.get("TWILIO_ACCOUNT_SID"),
        current_app.config.get("TWILIO_AUTH_TOKEN"),
    )


def _twilio_api_request(method, path, data=None):
    account_sid, auth_token = _twilio_credentials()
    if not account_sid or not auth_token:
        raise RuntimeError("Twilio Account SID and Auth Token are not configured.")

    base_url = (
        current_app.config.get("TWILIO_CONVERSATIONS_API_BASE")
        or "https://conversations.twilio.com/v1"
    ).rstrip("/")
    url = f"{base_url}/{path.lstrip('/')}"
    body = None
    headers = {}
    if data is not None:
        body = urllib.parse.urlencode(data).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    token = base64.b64encode(f"{account_sid}:{auth_token}".encode("utf-8")).decode("ascii")
    headers["Authorization"] = f"Basic {token}"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=current_app.config["TWILIO_CHATBOT_TIMEOUT_SECONDS"]) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = {"message": raw}
        return exc.code, payload


def post_twilio_conversation_reply(conversation_sid, body):
    if not conversation_sid:
        return False
    status, payload = _twilio_api_request(
        "POST",
        f"Conversations/{urllib.parse.quote(conversation_sid, safe='')}/Messages",
        {
            "Author": twilio_bot_identity(),
            "Body": body,
        },
    )
    if not (200 <= status < 300):
        current_app.logger.warning("Twilio bot reply failed: HTTP %s %s", status, payload)
        return False
    return True


def _ensure_participant(conversation_sid, identity):
    status, payload = _twilio_api_request(
        "POST",
        f"Conversations/{urllib.parse.quote(conversation_sid, safe='')}/Participants",
        {"Identity": identity},
    )
    if status in (200, 201, 409):
        return True
    current_app.logger.warning("Twilio participant add failed: HTTP %s %s", status, payload)
    return False


def ensure_twilio_user_conversation(user):
    unique_name = f"portal-assistant-{user.id}"
    status, payload = _twilio_api_request(
        "GET",
        f"Conversations/{urllib.parse.quote(unique_name, safe='')}",
    )
    if status == 404:
        status, payload = _twilio_api_request(
            "POST",
            "Conversations",
            {
                "UniqueName": unique_name,
                "FriendlyName": f"NMB-HLab portal assistant - {user.full_name or user.email}",
            },
        )
    if not (200 <= status < 300):
        raise RuntimeError(f"Twilio conversation setup failed: HTTP {status} {payload}")
    conversation_sid = payload.get("sid")
    if not conversation_sid:
        raise RuntimeError("Twilio conversation setup did not return a conversation SID.")
    _ensure_participant(conversation_sid, twilio_user_identity(user))
    _ensure_participant(conversation_sid, twilio_bot_identity())
    return conversation_sid


def ensure_twilio_patient_conversation(user):
    return ensure_twilio_user_conversation(user)


def generate_twilio_chat_token(user):
    try:
        from twilio.jwt.access_token import AccessToken
        from twilio.jwt.access_token.grants import ChatGrant
    except ImportError as exc:
        raise RuntimeError("Install the twilio package to use Twilio Conversations chat.") from exc

    account_sid = current_app.config.get("TWILIO_ACCOUNT_SID")
    api_key_sid = current_app.config.get("TWILIO_API_KEY_SID")
    api_key_secret = current_app.config.get("TWILIO_API_KEY_SECRET")
    service_sid = current_app.config.get("TWILIO_CONVERSATIONS_SERVICE_SID")
    if not all((account_sid, api_key_sid, api_key_secret, service_sid)):
        raise RuntimeError("Twilio Conversations token settings are incomplete.")

    identity = twilio_user_identity(user)
    token = AccessToken(account_sid, api_key_sid, api_key_secret, identity=identity)
    token.add_grant(ChatGrant(service_sid=service_sid))
    jwt = token.to_jwt()
    if isinstance(jwt, bytes):
        jwt = jwt.decode("utf-8")
    return jwt, identity


def twilio_conversations_configured():
    required = (
        "TWILIO_ACCOUNT_SID",
        "TWILIO_AUTH_TOKEN",
        "TWILIO_API_KEY_SID",
        "TWILIO_API_KEY_SECRET",
        "TWILIO_CONVERSATIONS_SERVICE_SID",
    )
    return all(current_app.config.get(name) for name in required)
