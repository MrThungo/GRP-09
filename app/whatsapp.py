"""GreenAPI WhatsApp messaging helpers."""
import json
import re
import urllib.error
import urllib.request

from flask import current_app, has_app_context

from .email import BRAND_NAME
from .models import ROLE_LABELS


ROLE_USAGE_LINES = {
    "patient": (
        "Patients can track laboratory requests, view released results, "
        "download reports and manage doctor access."
    ),
    "doctor": (
        "Doctors can create patient requests, track progress, review verified "
        "results and release them to patients."
    ),
    "lab_technician": (
        "Technicians can view assigned work, receive samples, capture results "
        "and verify tests."
    ),
    "lab_manager": (
        "Managers can manage test catalogues, inventory, suppliers, orders "
        "and laboratory reports."
    ),
    "admin": (
        "Administrators can manage users, roles, reports and system activity."
    ),
}


def _log_warning(message, *args):
    if has_app_context():
        current_app.logger.warning(message, *args)


def _app_url(path="/"):
    if not has_app_context():
        return path
    base = (current_app.config.get("APP_BASE_URL") or "").rstrip("/")
    if not base:
        return path
    return f"{base}/{path.lstrip('/')}"


def _phone_chat_id(phone_number):
    if not has_app_context():
        return None

    digits = re.sub(r"\D", "", phone_number or "")
    if not digits:
        return None
    if digits.startswith("00"):
        digits = digits[2:]

    default_country_code = re.sub(
        r"\D",
        "",
        current_app.config.get("GREENAPI_DEFAULT_COUNTRY_CODE") or "",
    )
    if default_country_code and digits.startswith("0"):
        digits = default_country_code + digits.lstrip("0")

    if len(digits) < 8:
        return None
    return f"{digits}@c.us"


def send_greenapi_message(phone_number, message):
    """Send a WhatsApp text message through GreenAPI."""
    if not has_app_context():
        return False
    if current_app.config.get("GREENAPI_ENABLED") is False:
        return False

    id_instance = current_app.config.get("GREENAPI_ID_INSTANCE")
    api_token = current_app.config.get("GREENAPI_API_TOKEN_INSTANCE")
    if not id_instance or not api_token:
        return False

    chat_id = _phone_chat_id(phone_number)
    if not chat_id:
        _log_warning("WhatsApp welcome not sent: invalid phone number %r.", phone_number)
        return False

    api_url = (current_app.config.get("GREENAPI_API_URL") or "https://api.green-api.com").rstrip("/")
    timeout = current_app.config.get("GREENAPI_TIMEOUT_SECONDS") or 10
    url = f"{api_url}/waInstance{id_instance}/sendMessage/{api_token}"
    payload = json.dumps(
        {
            "chatId": chat_id,
            "message": message,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read()
            return 200 <= response.status < 300
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        _log_warning("GreenAPI send failed for %s: HTTP %s %s", chat_id, exc.code, body)
    except Exception as exc:
        _log_warning("GreenAPI send failed for %s: %s", chat_id, exc)
    return False


def build_account_welcome_message(user, role=None, temporary_password=None):
    role = role or getattr(user, "primary_role", None)
    role_label = ROLE_LABELS.get(role, (role or "portal user").replace("_", " ").title())
    name = getattr(user, "full_name", None) or getattr(user, "email", None) or "there"
    signin_url = _app_url("/signin")
    lines = [
        f"Hello {name},",
        "",
        f"Welcome to {BRAND_NAME}. Your {role_label} account is ready.",
        "",
        "How to use the app:",
        "1. Sign in with your email address.",
        "2. Change your temporary password when prompted.",
        "3. Use your dashboard for requests, results, notifications and messages.",
    ]
    usage_line = ROLE_USAGE_LINES.get(role)
    if usage_line:
        lines.append(f"4. {usage_line}")
    include_temp_password = (
        has_app_context()
        and current_app.config.get("GREENAPI_INCLUDE_TEMP_PASSWORD")
    )
    if temporary_password and include_temp_password:
        lines.extend(["", f"Temporary password: {temporary_password}"])
    else:
        lines.extend(["", "Your temporary password was sent by email."])
    lines.extend(["", f"Sign in: {signin_url}", "", f"- {BRAND_NAME}"])
    return "\n".join(lines)


def send_account_welcome_whatsapp(user, role=None, temporary_password=None):
    message = build_account_welcome_message(
        user,
        role=role,
        temporary_password=temporary_password,
    )
    return send_greenapi_message(getattr(user, "phone", None), message)
