"""Shared email/SMS helpers."""
import os
import smtplib
import ssl
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import parseaddr
from html import escape

from flask import current_app, has_app_context

from .url_utils import app_url


DEFAULT_BRAND_NAME = "MediLab Connect"
BRAND_NAME = DEFAULT_BRAND_NAME
EMAIL_SIGNATURE = "Kind Regards\nManagement"
SMTP_TIMEOUT = 12


def _brand_name():
    if has_app_context():
        return current_app.config.get("APP_NAME") or DEFAULT_BRAND_NAME
    return os.environ.get("APP_NAME") or DEFAULT_BRAND_NAME


def _log_warning(message, *args):
    if has_app_context():
        current_app.logger.warning(message, *args)


def _clean_recipients(recipients):
    if isinstance(recipients, str):
        recipients = [recipients]
    return [address.strip() for address in (recipients or []) if address and address.strip()]


def _app_url(path="/"):
    return app_url(path, external=True) if has_app_context() else path


def _env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _smtp_settings():
    if has_app_context():
        config = current_app.config
        return {
            "server": config.get("MAIL_SERVER") or "localhost",
            "port": int(config.get("MAIL_PORT") or 25),
            "username": config.get("MAIL_USERNAME") or "",
            "password": config.get("MAIL_PASSWORD") or "",
            "use_tls": bool(config.get("MAIL_USE_TLS")),
            "use_ssl": bool(config.get("MAIL_USE_SSL")),
            "sender": config.get("MAIL_DEFAULT_SENDER") or config.get("MAIL_USERNAME") or "no-reply@medilabconnect.local",
        }
    settings = {
        "server": os.environ.get("MAIL_SERVER") or os.environ.get("SMTP_HOST") or os.environ.get("SMTP_SERVER") or "localhost",
        "port": int(os.environ.get("MAIL_PORT") or os.environ.get("SMTP_PORT") or "25"),
        "username": os.environ.get("MAIL_USERNAME") or os.environ.get("SMTP_USERNAME") or "",
        "password": os.environ.get("MAIL_PASSWORD") or os.environ.get("SMTP_PASSWORD") or "",
        "use_tls": _env_bool("MAIL_USE_TLS", _env_bool("SMTP_USE_TLS", False)),
        "use_ssl": _env_bool("MAIL_USE_SSL", _env_bool("SMTP_USE_SSL", False)),
        "sender": (
            os.environ.get("MAIL_DEFAULT_SENDER")
            or os.environ.get("SMTP_DEFAULT_SENDER")
            or os.environ.get("MAIL_USERNAME")
            or os.environ.get("SMTP_USERNAME")
            or "no-reply@medilabconnect.local"
        ),
    }
    if settings["port"] == 465 and not (os.environ.get("MAIL_USE_SSL") or os.environ.get("SMTP_USE_SSL")):
        settings["use_ssl"] = True
        settings["use_tls"] = False
    if settings["use_ssl"]:
        settings["use_tls"] = False
    return settings


def _smtp_timeout():
    value = None
    if has_app_context():
        value = current_app.config.get("SMTP_TIMEOUT_SECONDS")
    if value is None:
        value = os.environ.get("SMTP_TIMEOUT_SECONDS")
    try:
        return max(3, int(value or SMTP_TIMEOUT))
    except (TypeError, ValueError):
        return SMTP_TIMEOUT


def _normalise_smtp_password(server, password):
    """Gmail displays app passwords with spaces; SMTP auth expects the compact token."""
    password = password or ""
    host = (server or "").strip().lower()
    compact = password.replace(" ", "")
    if "gmail.com" in host or "googlemail.com" in host:
        if len(compact) == 16 and compact.isalnum():
            return compact
    return password


def _smtp_log_context(settings):
    return (
        f"server={settings.get('server') or '-'} "
        f"port={settings.get('port') or '-'} "
        f"ssl={bool(settings.get('use_ssl'))} "
        f"tls={bool(settings.get('use_tls'))} "
        f"auth={bool(settings.get('username') and settings.get('password'))} "
        f"sender={parseaddr(settings.get('sender') or '')[1] or '-'}"
    )


def _sender_headers(settings):
    brand = _brand_name()
    sender = (settings.get("sender") or "").strip() or "no-reply@medilabconnect.local"
    sender_address = parseaddr(sender)[1] or sender
    from_header = sender if "<" in sender else f"{brand} <{sender_address}>"
    return from_header, sender_address


def _normalise_signature(body):
    body = body or ""
    for legacy_signature in (f"- {_brand_name()}", "- NMB-HLab", "- NMB-Lab", "- MediLab Connect"):
        body = body.replace(legacy_signature, EMAIL_SIGNATURE)
    if body.strip() and EMAIL_SIGNATURE not in body:
        body = f"{body.rstrip()}\n\n{EMAIL_SIGNATURE}"
    return body


def _html_signature():
    return """
                <p style="margin:28px 0 0;font-size:15px;line-height:1.7;color:#334155;">
                  Kind Regards<br>
                  Management
                </p>
    """


def _html_email(title, intro, details=None, action_label=None, action_url=None, accent="#0284c7"):
    brand = _brand_name()
    details = details or []
    detail_rows = "".join(
        f"""
        <tr>
          <td style="padding:8px 0;color:#64748b;font-size:13px;">{escape(label)}</td>
          <td style="padding:8px 0;color:#0f172a;font-size:14px;font-weight:700;text-align:right;">{escape(value)}</td>
        </tr>
        """
        for label, value in details
        if value is not None and value != ""
    )
    detail_block = (
        f"""
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0"
               style="margin-top:24px;border-top:1px solid #e2e8f0;border-bottom:1px solid #e2e8f0;">
          {detail_rows}
        </table>
        """
        if detail_rows else ""
    )
    action_block = (
        f"""
        <p style="margin:28px 0 0;text-align:center;">
          <a href="{escape(action_url)}"
             style="display:inline-block;background:{accent};color:#ffffff;text-decoration:none;
                    padding:12px 20px;border-radius:8px;font-weight:700;font-size:14px;">
            {escape(action_label)}
          </a>
        </p>
        """
        if action_label and action_url else ""
    )
    return f"""<!doctype html>
<html lang="en">
  <body style="margin:0;background:#f1f5f9;font-family:Arial,Helvetica,sans-serif;color:#334155;">
    <div style="display:none;max-height:0;overflow:hidden;opacity:0;">{escape(intro)}</div>
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="padding:32px 12px;">
      <tr>
        <td align="center">
          <table role="presentation" width="100%" cellspacing="0" cellpadding="0"
                 style="max-width:620px;background:#ffffff;border:1px solid #e2e8f0;border-radius:14px;overflow:hidden;">
            <tr>
              <td style="background:{accent};padding:28px 30px;color:#ffffff;">
                <div style="font-size:13px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;">{brand}</div>
                <h1 style="margin:8px 0 0;font-size:25px;line-height:1.25;">{escape(title)}</h1>
              </td>
            </tr>
            <tr>
              <td style="padding:30px;">
                <p style="margin:0;font-size:15px;line-height:1.7;color:#334155;">{escape(intro)}</p>
                {detail_block}
                {action_block}
                {_html_signature()}
              </td>
            </tr>
            <tr>
              <td style="padding:18px 30px;border-top:1px solid #e2e8f0;color:#64748b;font-size:12px;">
                This is an automated message from {brand}. For clinical or urgent matters, contact the laboratory directly.
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>"""


def _html_from_text(subject, body):
    brand = _brand_name()
    safe_body = escape(body or "").replace("\n", "<br>")
    return f"""<!doctype html>
<html lang="en">
  <body style="margin:0;background:#f1f5f9;font-family:Arial,Helvetica,sans-serif;color:#334155;">
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="padding:32px 12px;">
      <tr>
        <td align="center">
          <table role="presentation" width="100%" cellspacing="0" cellpadding="0"
                 style="max-width:620px;background:#ffffff;border:1px solid #e2e8f0;border-radius:14px;overflow:hidden;">
            <tr>
              <td style="background:#0284c7;padding:24px 30px;color:#ffffff;">
                <div style="font-size:13px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;">{brand}</div>
                <h1 style="margin:8px 0 0;font-size:22px;line-height:1.25;">{escape(subject or brand)}</h1>
              </td>
            </tr>
            <tr>
              <td style="padding:30px;font-size:15px;line-height:1.7;color:#334155;">
                {safe_body}
              </td>
            </tr>
            <tr>
              <td style="padding:18px 30px;border-top:1px solid #e2e8f0;color:#64748b;font-size:12px;">
                This is an automated message from {brand}. For clinical or urgent matters, contact the laboratory directly.
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>"""


def send_email(recipients, subject, body, html=None, attachments=None):
    """Send email using SMTP settings supplied by the environment/app config."""
    recipients = _clean_recipients(recipients)
    if not recipients:
        return False
    body = _normalise_signature(body)
    settings = _smtp_settings()
    from_header, sender_address = _sender_headers(settings)
    username = settings["username"]
    password = _normalise_smtp_password(settings.get("server"), settings["password"])
    timeout = _smtp_timeout()

    try:
        msg = MIMEMultipart("mixed" if attachments else "alternative")
        msg["Subject"] = subject
        msg["From"] = from_header
        msg["To"] = ", ".join(recipients)
        msg["Reply-To"] = sender_address
        msg["List-Unsubscribe"] = f"<mailto:{sender_address}>"
        if html is None:
            html = _html_from_text(subject, body)

        alternative = MIMEMultipart("alternative") if attachments else msg
        alternative.attach(MIMEText(body or "", "plain", "utf-8"))
        if html:
            alternative.attach(MIMEText(html, "html", "utf-8"))
        if attachments:
            msg.attach(alternative)

        for filename, content_type, data in attachments or []:
            subtype = "octet-stream"
            if content_type and "/" in content_type:
                subtype = content_type.split("/", 1)[1]
            part = MIMEApplication(data, _subtype=subtype)
            part.add_header("Content-Disposition", "attachment", filename=filename)
            if content_type:
                part.replace_header("Content-Type", content_type)
            msg.attach(part)

        context = ssl.create_default_context()
        message = msg.as_string()
        errors = []
        methods = []
        if settings["use_ssl"]:
            methods.append("ssl")
        if settings["use_tls"]:
            methods.append("starttls")
        if not methods:
            methods.append("plain")
        for method in methods:
            try:
                if method == "ssl":
                    with smtplib.SMTP_SSL(
                        settings["server"],
                        settings["port"],
                        timeout=timeout,
                        context=context,
                    ) as smtp:
                        if username and password:
                            smtp.login(user=username, password=password)
                        refused = smtp.sendmail(from_addr=sender_address, to_addrs=recipients, msg=message)
                else:
                    with smtplib.SMTP(settings["server"], settings["port"], timeout=timeout) as smtp:
                        smtp.ehlo()
                        if method == "starttls":
                            smtp.starttls(context=context)
                            smtp.ehlo()
                        if username and password:
                            smtp.login(user=username, password=password)
                        refused = smtp.sendmail(from_addr=sender_address, to_addrs=recipients, msg=message)
                if refused:
                    raise smtplib.SMTPRecipientsRefused(refused)
                return True
            except Exception as exc:
                errors.append(f"{method}: {exc}")
        _log_warning(
            "Email send failed for %s (%s): %s",
            recipients,
            _smtp_log_context(settings),
            "; ".join(errors),
        )
        return False
    except Exception as exc:
        _log_warning(
            "Email send failed for %s (%s): %s",
            recipients,
            _smtp_log_context(settings),
            exc,
        )
        return False


def Registration(useremail, temporary_password=None):
    """Send a welcome message for newly created portal accounts."""
    brand = _brand_name()
    signin_url = _app_url("/signin")
    password_line = (
        f"\nTemporary password: {temporary_password}\nYou must change it after signing in.\n"
        if temporary_password else ""
    )
    body = (
        f"Hello {useremail},\n\n"
        f"Your {brand} account has been created.\n"
        f"{password_line}\n"
        f"Sign in here: {signin_url}\n\n"
        "For security, change this password the first time you sign in.\n\n"
        f"{EMAIL_SIGNATURE}"
    )
    details = [("Status", "Account active")]
    if temporary_password:
        details.append(("Temporary password", temporary_password))
    html = _html_email(
        f"Welcome to {brand}",
        "Your secure laboratory portal account has been created.",
        details,
        "Sign in",
        signin_url,
    )
    return send_email([useremail], f"Welcome to {brand}", body, html=html)


def Approved(useremail, order_id=None, amount=None):
    """Compatibility helper for approval/confirmation emails."""
    brand = _brand_name()
    signin_url = _app_url("/signin")
    details = []
    if order_id:
        details.append(("Reference", str(order_id)))
    if amount is not None:
        details.append(("Amount", f"R{amount}"))
    body_lines = [
        f"Hello {useremail},",
        "",
        f"Your request has been confirmed on {brand}.",
    ]
    if order_id:
        body_lines.append(f"Reference: {order_id}")
    if amount is not None:
        body_lines.append(f"Amount: R{amount}")
    body_lines += ["", f"Sign in here: {signin_url}", "", EMAIL_SIGNATURE]
    html = _html_email(
        "Request confirmed",
        f"Your request has been confirmed on {brand}.",
        details,
        "Open portal",
        signin_url,
        accent="#059669",
    )
    return send_email([useremail], f"{brand} confirmation", "\n".join(body_lines), html=html)


def Approved_(useremail):
    """Compatibility alias for older callers."""
    return Approved(useremail)


def Cancel_Approved(useremail):
    """Compatibility helper for cancellation emails."""
    brand = _brand_name()
    signin_url = _app_url("/signin")
    body = (
        f"Hello {useremail},\n\n"
        "A previous request was cancelled before completion.\n\n"
        f"Sign in here: {signin_url}\n\n"
        f"{EMAIL_SIGNATURE}"
    )
    html = _html_email(
        "Request cancelled",
        "A previous request was cancelled before completion.",
        [("Status", "Cancelled")],
        "Open portal",
        signin_url,
        accent="#dc2626",
    )
    return send_email([useremail], f"{brand} cancellation", body, html=html)


def _send_sms(number, body):
    if not number:
        return False
    if not has_app_context():
        return False

    account_sid = current_app.config.get("TWILIO_ACCOUNT_SID")
    auth_token = current_app.config.get("TWILIO_AUTH_TOKEN")
    from_number = current_app.config.get("TWILIO_FROM_NUMBER")
    if not (account_sid and auth_token and from_number):
        _log_warning("SMS not sent to %s: Twilio is not configured.", number)
        return False

    try:
        from twilio.rest import Client

        Client(account_sid, auth_token).messages.create(
            body=body,
            from_=from_number,
            to=number,
        )
        return True
    except ImportError:
        _log_warning("SMS not sent to %s: twilio package is not installed.", number)
        return False
    except Exception as exc:
        _log_warning("SMS send failed for %s: %s", number, exc)
        return False


def Sms(number, address, name):
    body = f"{BRAND_NAME} update: Hi {name}, your order/update is ready. Address: {address}"
    return _send_sms(number, body)


def Sms2(number, name, messa):
    body = f"{BRAND_NAME} update: Hi {name}, {messa}."
    return _send_sms(number, body)


__all__ = [
    "send_email",
    "Registration",
    "Approved",
    "Approved_",
    "Cancel_Approved",
    "Sms",
    "Sms2",
]
