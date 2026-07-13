"""Shared email/SMS helpers."""
import smtplib
import ssl
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape

from flask import current_app, has_app_context


BRAND_NAME = "NMB-HLab"
EMAIL_SIGNATURE = "Kind Regards\nManagement"
EMAIL = "LabLink718@gmail.com"
PASSWORD = "seul zmgw wbta eauw"
SMTP_SERVER = "smtp.gmail.com"
SMTP_SSL_PORT = 465
SMTP_TLS_PORT = 587
SMTP_TIMEOUT = 30


def _log_warning(message, *args):
    if has_app_context():
        current_app.logger.warning(message, *args)


def _clean_recipients(recipients):
    if isinstance(recipients, str):
        recipients = [recipients]
    return [address.strip() for address in (recipients or []) if address and address.strip()]


def _app_url(path="/"):
    if not has_app_context():
        return path
    base = (current_app.config.get("APP_BASE_URL") or "").rstrip("/")
    if not base:
        return path
    return f"{base}/{path.lstrip('/')}"


def _normalise_signature(body):
    body = (body or "").replace(f"- {BRAND_NAME}", EMAIL_SIGNATURE)
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
                <div style="font-size:13px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;">{BRAND_NAME}</div>
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
                This is an automated message from {BRAND_NAME}. For clinical or urgent matters, contact the laboratory directly.
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>"""


def _html_from_text(subject, body):
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
                <div style="font-size:13px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;">{BRAND_NAME}</div>
                <h1 style="margin:8px 0 0;font-size:22px;line-height:1.25;">{escape(subject or BRAND_NAME)}</h1>
              </td>
            </tr>
            <tr>
              <td style="padding:30px;font-size:15px;line-height:1.7;color:#334155;">
                {safe_body}
              </td>
            </tr>
            <tr>
              <td style="padding:18px 30px;border-top:1px solid #e2e8f0;color:#64748b;font-size:12px;">
                This is an automated message from {BRAND_NAME}. For clinical or urgent matters, contact the laboratory directly.
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>"""


def send_email(recipients, subject, body, html=None, attachments=None):
    """Send email using the hard-coded Gmail SMTP flow from the working project."""
    recipients = _clean_recipients(recipients)
    if not recipients:
        return False
    body = _normalise_signature(body)

    try:
        msg = MIMEMultipart("mixed" if attachments else "alternative")
        msg["Subject"] = subject
        msg["From"] = f"{BRAND_NAME} <{EMAIL}>"
        msg["To"] = ", ".join(recipients)
        msg["Reply-To"] = EMAIL
        msg["List-Unsubscribe"] = f"<mailto:{EMAIL}>"
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
        for method in ("ssl", "starttls"):
            try:
                if method == "ssl":
                    with smtplib.SMTP_SSL(
                        SMTP_SERVER,
                        SMTP_SSL_PORT,
                        timeout=SMTP_TIMEOUT,
                        context=context,
                    ) as smtp:
                        smtp.login(user=EMAIL, password=PASSWORD)
                        refused = smtp.sendmail(from_addr=EMAIL, to_addrs=recipients, msg=message)
                else:
                    with smtplib.SMTP(SMTP_SERVER, SMTP_TLS_PORT, timeout=SMTP_TIMEOUT) as smtp:
                        smtp.ehlo()
                        smtp.starttls(context=context)
                        smtp.ehlo()
                        smtp.login(user=EMAIL, password=PASSWORD)
                        refused = smtp.sendmail(from_addr=EMAIL, to_addrs=recipients, msg=message)
                if refused:
                    raise smtplib.SMTPRecipientsRefused(refused)
                return True
            except Exception as exc:
                errors.append(f"{method}: {exc}")
        _log_warning("Email send failed for %s: %s", recipients, "; ".join(errors))
        return False
    except Exception as exc:
        _log_warning("Email send failed for %s: %s", recipients, exc)
        return False


def Registration(useremail, temporary_password=None):
    """Send a welcome message for newly created portal accounts."""
    signin_url = _app_url("/signin")
    password_line = (
        f"\nTemporary password: {temporary_password}\nYou must change it after signing in.\n"
        if temporary_password else ""
    )
    body = (
        f"Hello {useremail},\n\n"
        f"Your {BRAND_NAME} account has been created.\n"
        f"{password_line}\n"
        f"Sign in here: {signin_url}\n\n"
        "For security, change this password the first time you sign in.\n\n"
        f"{EMAIL_SIGNATURE}"
    )
    details = [("Status", "Account active")]
    if temporary_password:
        details.append(("Temporary password", temporary_password))
    html = _html_email(
        "Welcome to NMB-HLab",
        "Your secure laboratory portal account has been created.",
        details,
        "Sign in",
        signin_url,
    )
    return send_email([useremail], f"Welcome to {BRAND_NAME}", body, html=html)


def Approved(useremail, order_id=None, amount=None):
    """Compatibility helper for approval/confirmation emails."""
    signin_url = _app_url("/signin")
    details = []
    if order_id:
        details.append(("Reference", str(order_id)))
    if amount is not None:
        details.append(("Amount", f"R{amount}"))
    body_lines = [
        f"Hello {useremail},",
        "",
        "Your request has been confirmed on NMB-HLab.",
    ]
    if order_id:
        body_lines.append(f"Reference: {order_id}")
    if amount is not None:
        body_lines.append(f"Amount: R{amount}")
    body_lines += ["", f"Sign in here: {signin_url}", "", EMAIL_SIGNATURE]
    html = _html_email(
        "Request confirmed",
        "Your request has been confirmed on NMB-HLab.",
        details,
        "Open portal",
        signin_url,
        accent="#059669",
    )
    return send_email([useremail], f"{BRAND_NAME} confirmation", "\n".join(body_lines), html=html)


def Approved_(useremail):
    """Compatibility alias for older callers."""
    return Approved(useremail)


def Cancel_Approved(useremail):
    """Compatibility helper for cancellation emails."""
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
    return send_email([useremail], f"{BRAND_NAME} cancellation", body, html=html)


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
