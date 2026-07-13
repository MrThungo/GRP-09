import secrets
from datetime import date
from flask import (
    Blueprint, render_template, redirect, url_for, flash, request, current_app,
    session,
)
from flask_login import login_user, logout_user, login_required, current_user
from itsdangerous import BadSignature, SignatureExpired

from ..extensions import db
from ..models import User, UserRole, Patient
from ..auth_utils import password_policy_error
from ..email import send_email
from ..services import notify_admins, log_audit
from ..sa_id import validate_sa_id
from ..DefaultUsers import DEFAULT_USER_PASSWORDS
from ..whatsapp import send_account_welcome_whatsapp

bp = Blueprint("auth", __name__, template_folder="../templates/auth")


def _post_login_redirect(user):
    if user.is_pending:
        if user.must_change_password:
            return redirect(url_for("auth.change_password"))
        return redirect(url_for("auth.app_home"))
    return redirect(url_for("app_home"))


@bp.route("/login", methods=["GET", "POST"])
@bp.route("/signin", methods=["GET", "POST"])
@bp.route("/sign-in", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return _post_login_redirect(current_user)
    if request.method == "POST":
        email = (request.form.get("email") or "").lower().strip()
        password = request.form.get("password") or ""
        if not email or not password:
            flash("Email and password are required.", "error")
        else:
            user = User.query.filter_by(email=email).first()
            if user and getattr(user, "deleted_at", None):
                flash("Your account has been deleted. Please contact an administrator.", "error")
            elif user and getattr(user, "is_blocked", False):
                flash("Your account has been blocked. Please contact an administrator.", "error")
            elif user and getattr(user, "is_deactivated", False):
                flash("Your account is deactivated. An administrator must reactivate it before you can sign in.", "error")
            elif user and user.check_password(password):
                login_user(user)
                session.permanent = False
                if user.must_change_password:
                    session["_temp_password"] = password
                response = _post_login_redirect(user)
                return response
            else:
                flash("Invalid email or password", "error")
    return render_template("auth/login.html")


@bp.route("/signup", methods=["GET", "POST"])
def signup():
    """Public self-registration. New accounts default to the Patient role."""
    if current_user.is_authenticated:
        return _post_login_redirect(current_user)
    if request.method == "POST":
        first_name = (request.form.get("first_name") or "").strip()
        surname = (request.form.get("surname") or "").strip()
        raw_sa_id = (request.form.get("id_number") or "").strip()
        sa_id = "".join(ch for ch in raw_sa_id if ch.isdigit()) if raw_sa_id else ""
        dob_str = (request.form.get("date_of_birth") or "").strip()
        cellphone = (request.form.get("cellphone") or "").strip()
        email = (request.form.get("email") or "").lower().strip()
        address = (request.form.get("address") or "").strip()

        ok, err, dob_from_id = validate_sa_id(raw_sa_id)
        dob = None
        if dob_str:
            try:
                dob = date.fromisoformat(dob_str)
            except ValueError:
                dob = None

        errors = []
        if not first_name: errors.append("First name is required.")
        if not surname: errors.append("Surname is required.")
        if not ok: errors.append(err or "Invalid SA ID number.")
        if not cellphone: errors.append("Cellphone number is required.")
        if not email: errors.append("Email is required.")
        if not address: errors.append("Home address is required.")
        if dob and dob_from_id and dob != dob_from_id:
            errors.append("Date of birth does not match the SA ID number.")
        if not dob:
            dob = dob_from_id
        if not errors and User.query.filter_by(email=email).first():
            errors.append("That email is already registered.")
        if not errors and User.query.filter_by(sa_id_number=sa_id).first():
            errors.append("That SA ID number is already registered.")
        if not errors and Patient.query.filter_by(id_number=sa_id).first():
            errors.append("That SA ID number is already registered.")

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template(
                "auth/signup.html",
                form={
                    "first_name": first_name, "surname": surname,
                    "id_number": sa_id, "date_of_birth": dob_str,
                    "cellphone": cellphone, "email": email, "address": address,
                },
            )

        full_name = f"{first_name} {surname}".strip()
        # Auto-generate a temporary password. The user will be forced to
        # change it on first login via the global overlay in base.html.
        generated_password = secrets.token_urlsafe(9)
        user = User(
            email=email, full_name=full_name, phone=cellphone,
            sa_id_number=sa_id,
            must_change_password=True,
        )
        user.set_password(generated_password)
        user.temp_password = generated_password
        db.session.add(user)
        db.session.flush()
        db.session.add(UserRole(user_id=user.id, role="patient"))
        db.session.add(Patient(
            profile_id=user.id, mrn="MRN-" + user.id[:8],
            full_name=full_name, surname=surname, id_number=sa_id,
            date_of_birth=dob, phone=cellphone, email=email, address=address,
        ))
        notify_admins(
            "New patient registered",
            f"{full_name} ({email}) just signed up as a patient.",
            url_for("admin.users"),
        )
        log_audit(user.id, "signup", "user", user.id)
        db.session.commit()
        sent = send_email(
            [email],
            "Your NMB-HLab patient account",
            (
                f"Hello {full_name},\n\n"
                "Your NMB-HLab patient account has been created.\n\n"
                f"Temporary password: {generated_password}\n\n"
                "For security, you will be asked to choose a new password the first time you sign in.\n\n"
                "- NMB-HLab"
            ),
        )
        whatsapp_sent = send_account_welcome_whatsapp(
            user,
            role="patient",
            temporary_password=generated_password,
        )
        # Auto-login and stash the temp password in session so the forced
        # change-password overlay can pre-fill the "current password" field.
        login_user(user)
        session.permanent = True
        session["_temp_password"] = generated_password
        flash_message = (
            "Welcome! We emailed your temporary password. Please set a new password to continue."
            if sent else
            "Welcome! Your account was created, but the email could not be sent. Please set a new password to continue."
        )
        if whatsapp_sent:
            flash_message += " We also sent setup steps by WhatsApp."
        flash(
            flash_message,
            "success" if sent else "error",
        )
        return _post_login_redirect(user)
    return render_template("auth/signup.html")


@bp.route("/pending")
@login_required
def pending():
    if not current_user.is_pending:
        return redirect(url_for("app_home"))
    return render_template("auth/pending.html")


@bp.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    from flask import session
    # Prefer session value (just-typed at login); fall back to the stored temp
    # password so users who never received their welcome email can still change.
    temp_password = session.get("_temp_password") or (current_user.temp_password or "")
    current_password_prefill = temp_password
    if not current_password_prefill:
        demo_password = DEFAULT_USER_PASSWORDS.get((current_user.email or "").lower())
        if demo_password and current_user.check_password(demo_password):
            current_password_prefill = demo_password
    if request.method == "POST":
        current = request.form.get("current_password") or ""
        new = request.form.get("new_password") or ""
        confirm = request.form.get("confirm_password") or ""
        policy_error = password_policy_error(new)
        if not current_user.check_password(current):
            flash("Current password is incorrect.", "error")
        elif policy_error:
            flash(policy_error, "error")
        elif new != confirm:
            flash("New passwords do not match.", "error")
        elif current_user.must_change_password and current and new == current:
            flash("New password must be different from the temporary password.", "error")
        else:
            current_user.set_password(new)
            current_user.must_change_password = False
            current_user.temp_password = None
            db.session.commit()
            session.pop("_temp_password", None)
            flash("Password updated.", "success")
            return redirect(url_for("app_home"))
    return render_template(
        "auth/change_password.html",
        temp_password=temp_password,
        current_password_prefill=current_password_prefill,
        first_login=current_user.must_change_password,
    )


@bp.route("/change-username", methods=["GET", "POST"])
@login_required
def change_username():
    if request.method == "POST":
        email = (request.form.get("email") or "").lower().strip()
        password = request.form.get("password") or ""
        if not current_user.check_password(password):
            flash("Current password is incorrect.", "error")
        elif "@" not in email or "." not in email.rsplit("@", 1)[-1]:
            flash("Enter a valid e-mail address.", "error")
        elif User.query.filter(User.email == email, User.id != current_user.id).first():
            flash("That e-mail address is already in use.", "error")
        else:
            old_email = current_user.email
            current_user.email = email
            if current_user.patient_record:
                current_user.patient_record.email = email
            log_audit(
                current_user.id,
                "change_username",
                "user",
                current_user.id,
                {"old_email": old_email, "new_email": email},
            )
            db.session.commit()
            flash("Username updated.", "success")
            return redirect(url_for("app_home"))
    return render_template("auth/change_username.html")


@bp.route("/logout", methods=["POST"])
@login_required
def logout():
    response = redirect(url_for("public.landing"))
    logout_user()
    return response


# ---------------------------------------------------------------------------
# Forgot password (sends a 5-minute reset link via SMTP)
# ---------------------------------------------------------------------------

def _build_reset_url(token: str) -> str:
    base = current_app.config.get("APP_BASE_URL") or request.host_url.rstrip("/")
    return f"{base}{url_for('auth.reset_password', token=token)}"


def _send_reset_email(user: User, reset_url: str) -> bool:
    sent = send_email(
        [user.email],
        "NMB-HLab - password reset",
        (
            f"Hello {user.full_name or user.email},\n\n"
            "A password reset was requested for your NMB-HLab account.\n\n"
            "Use the secure link below to set a new password. This link expires in 5 minutes:\n"
            f"{reset_url}\n\n"
            "If you did not request this change, no action is required. Your password will remain unchanged.\n\n"
            "- NMB-HLab"
        ),
    )
    if not sent:
        current_app.logger.warning("Reset link (dev fallback): %s", reset_url)
    return sent


@bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if current_user.is_authenticated:
        return _post_login_redirect(current_user)
    if request.method == "POST":
        email = (request.form.get("email") or "").lower().strip()
        if not email:
            flash("Please enter your email address.", "error")
            return render_template("auth/forgot_password.html")
        user = User.query.filter_by(email=email).first()
        # Always respond the same way to avoid leaking which emails exist.
        if user:
            token = current_app.password_reset_serializer.dumps(user.id)
            reset_url = _build_reset_url(token)
            sent = _send_reset_email(user, reset_url)
            log_audit(user.id, "request_password_reset", "user", user.id)
            db.session.commit()
            if not sent:
                # Surface link in dev if SMTP isn't configured.
                flash(
                    "Email could not be sent (SMTP not configured). Reset link "
                    f"(valid 5 minutes): {reset_url}",
                    "error",
                )
                return render_template("auth/forgot_password.html")
        flash(
            "If that email matches an account, we've sent a password reset "
            "link. The link will expire in 5 minutes.",
            "success",
        )
        return redirect(url_for("auth.login"))
    return render_template("auth/forgot_password.html")


@bp.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    max_age = current_app.config["PASSWORD_RESET_MAX_AGE_SECONDS"]
    try:
        user_id = current_app.password_reset_serializer.loads(token, max_age=max_age)
    except SignatureExpired:
        flash("This reset link has expired. Please request a new one.", "error")
        return redirect(url_for("auth.forgot_password"))
    except BadSignature:
        flash("This reset link is invalid.", "error")
        return redirect(url_for("auth.forgot_password"))

    user = db.session.get(User, user_id)
    if not user:
        flash("Account no longer exists.", "error")
        return redirect(url_for("auth.forgot_password"))

    if request.method == "POST":
        new = request.form.get("new_password") or ""
        confirm = request.form.get("confirm_password") or ""
        policy_error = password_policy_error(new)
        if policy_error:
            flash(policy_error, "error")
        elif new != confirm:
            flash("Passwords do not match.", "error")
        else:
            user.set_password(new)
            user.must_change_password = False
            user.temp_password = None
            log_audit(user.id, "complete_password_reset", "user", user.id)
            db.session.commit()
            flash("Password updated. You can now sign in.", "success")
            return redirect(url_for("auth.login"))
    return render_template("auth/reset_password.html", email=user.email)
