"""Admin: full management of every table, roles and account state."""
import hashlib
import json
import secrets
import os
import uuid
from datetime import date, datetime, timedelta
from flask import (
    Blueprint, render_template, redirect, url_for, request, flash, abort,
    jsonify, current_app, make_response,
)
from flask_login import login_required, current_user
from PIL import Image, UnidentifiedImageError
from sqlalchemy import func, or_
from sqlalchemy.orm import selectinload
from werkzeug.utils import secure_filename

from ..extensions import db
from ..auth_utils import role_required
from ..models import (
    User, UserRole, AuditLog, Patient, TestCatalog, TestRequest, TestRequestItem,
    Notification, SampleType,
    ROLES, ROLE_LABELS, REQUEST_STATUSES, TITLE_OPTIONS, GENDER_OPTIONS,
)
from ..models import Condition, Allergy, Medication
from ..sa_id import validate_sa_id
from ..services import notify, log_audit, send_email
from ..soft_delete import soft_delete
from ..presence import last_seen_age_label
from .api import ONLINE_WINDOW_MINUTES, ONLINE_WINDOW_SECONDS

bp = Blueprint("admin", __name__, template_folder="../templates/admin")
ALLOWED_AVATAR_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
PASSWORD_ACCESS_REQUEST_TTL_HOURS = 24
PASSWORD_ACCESS_APPROVAL_MINUTES = 30
PASSWORD_ACCESS_REQUESTED = "password_access_requested"
PASSWORD_ACCESS_APPROVED = "password_access_approved"
PASSWORD_ACCESS_DENIED = "password_access_denied"
PASSWORD_ACCESS_REVEALED = "password_access_revealed"
LANDING_TEAM_PICTURE_MAX_BYTES = 5 * 1024 * 1024


@bp.before_request
@login_required
@role_required("admin")
def _gate():
    pass


def _is_super_admin(user):
    return bool(user and user.has_role("admin") and user.has_role("super_admin"))


def _require_super_admin():
    if not _is_super_admin(current_user):
        abort(403)


def _audit_details(entry):
    try:
        value = json.loads(entry.details or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _audit_detail_label(key):
    labels = {
        "role": "Role",
        "roles": "Roles",
        "doctor_id": "Doctor ID",
        "patient_id": "Patient ID",
        "request_id": "Request ID",
        "request_ids": "Request IDs",
        "item_id": "Test item ID",
        "item_ids": "Test item IDs",
        "user_id": "User ID",
        "expires_at": "Expires at",
        "must_change_password": "Password change required",
        "student_number": "Student number",
    }
    return labels.get(key, str(key).replace("_", " ").strip().title())


def _audit_detail_value(key, value):
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if key in {"role", "previous_role", "new_role"}:
        return ROLE_LABELS.get(str(value), str(value).replace("_", " ").title())
    if key == "roles" and isinstance(value, (list, tuple)):
        return ", ".join(
            ROLE_LABELS.get(str(role), str(role).replace("_", " ").title())
            for role in value
        )
    if isinstance(value, dict):
        return "; ".join(
            f"{_audit_detail_label(child_key)}: "
            f"{_audit_detail_value(child_key, child_value)}"
            for child_key, child_value in value.items()
        )
    if isinstance(value, (list, tuple)):
        return ", ".join(_audit_detail_value(key.rstrip("s"), item) for item in value)
    return str(value)


def _audit_detail_rows(raw_details):
    if not raw_details:
        return []
    try:
        parsed = json.loads(raw_details)
    except (TypeError, ValueError, json.JSONDecodeError):
        return [{"label": "Details", "value": str(raw_details)}]
    if not isinstance(parsed, dict):
        return [{"label": "Details", "value": _audit_detail_value("details", parsed)}]
    return [
        {
            "label": _audit_detail_label(key),
            "value": _audit_detail_value(key, value),
        }
        for key, value in parsed.items()
    ]


def _password_request_decision(request_id):
    return (
        AuditLog.query
        .filter(
            AuditLog.entity_type == "password_access_request",
            AuditLog.entity_id == request_id,
            AuditLog.action.in_((PASSWORD_ACCESS_APPROVED, PASSWORD_ACCESS_DENIED)),
        )
        .order_by(AuditLog.created_at.desc())
        .first()
    )


def _password_request_reveal(request_id):
    return (
        AuditLog.query
        .filter_by(
            entity_type="password_access_request",
            entity_id=request_id,
            action=PASSWORD_ACCESS_REVEALED,
        )
        .order_by(AuditLog.created_at.desc())
        .first()
    )


def _password_access_state(request_entry, target=None):
    if not request_entry:
        return {"status": "none", "request": None}

    now = datetime.now()
    decision = _password_request_decision(request_entry.id)
    state = {
        "status": "pending",
        "request": request_entry,
        "decision": decision,
        "expires_at": None,
    }
    if not decision:
        if request_entry.created_at < now - timedelta(hours=PASSWORD_ACCESS_REQUEST_TTL_HOURS):
            state["status"] = "expired"
        return state

    if decision.action == PASSWORD_ACCESS_DENIED:
        state["status"] = "denied"
        return state

    decision_details = _audit_details(decision)
    try:
        expires_at = datetime.fromisoformat(decision_details.get("expires_at", ""))
    except (TypeError, ValueError):
        expires_at = decision.created_at + timedelta(minutes=PASSWORD_ACCESS_APPROVAL_MINUTES)
    state["expires_at"] = expires_at
    if expires_at <= now:
        state["status"] = "expired"
        return state

    reveal = _password_request_reveal(request_entry.id)
    state["reveal"] = reveal
    if not reveal:
        state["status"] = "approved"
        return state

    reveal_details = _audit_details(reveal)
    expected_fingerprint = reveal_details.get("password_fingerprint") or ""
    current_fingerprint = (
        hashlib.sha256((target.temp_password or "").encode("utf-8")).hexdigest()
        if target and target.temp_password else ""
    )
    if (
        target
        and target.must_change_password
        and expected_fingerprint
        and secrets.compare_digest(expected_fingerprint, current_fingerprint)
    ):
        state["status"] = "revealed"
    else:
        state["status"] = "consumed"
    return state


def _latest_password_request(requester_id, target_id):
    entry = (
        AuditLog.query
        .filter_by(
            actor_id=requester_id,
            action=PASSWORD_ACCESS_REQUESTED,
            entity_type="user",
            entity_id=target_id,
        )
        .order_by(AuditLog.created_at.desc())
        .first()
    )
    target = db.session.get(User, target_id)
    return _password_access_state(entry, target)


def _password_request_or_404(request_id):
    entry = (
        AuditLog.query
        .filter_by(
            id=request_id,
            action=PASSWORD_ACCESS_REQUESTED,
            entity_type="user",
        )
        .first()
    )
    if not entry:
        abort(404)
    return entry


def _available_super_admin_ids(exclude_user_id=None):
    query = (
        db.session.query(User.id)
        .join(UserRole, UserRole.user_id == User.id)
        .filter(
            UserRole.role == "super_admin",
            User.deleted_at.is_(None),
            User.is_blocked.is_(False),
            User.is_deactivated.is_(False),
        )
    )
    if exclude_user_id:
        query = query.filter(User.id != exclude_user_id)
    return [user_id for (user_id,) in query.distinct().all()]


def _admin_role_choices():
    return tuple(role for role in ROLES if role != "super_admin")


def _admin_dob_from_form():
    raw = (request.form.get("date_of_birth") or "").strip()
    sa_id = (request.form.get("sa_id_number") or "").strip()
    dob = None
    if raw:
        try:
            dob = date.fromisoformat(raw)
        except ValueError:
            return None, "Date of birth must be a valid date."
    if sa_id:
        valid, id_error, dob_from_id = validate_sa_id(sa_id)
        if not valid:
            return None, id_error or "Invalid South African ID number."
        if dob and dob_from_id and dob != dob_from_id:
            return None, "Date of birth does not match the SA ID number."
        dob = dob or dob_from_id
    return dob, None


@bp.route("/profile", methods=["GET", "POST"])
def profile():
    admin = current_user
    if request.method == "POST":
        full_name = (request.form.get("full_name") or "").strip()
        surname = (request.form.get("surname") or "").strip() or None
        title = (request.form.get("title") or "").strip() or None
        gender = (request.form.get("gender") or "").strip() or None
        email = (request.form.get("email") or "").lower().strip()
        phone = (request.form.get("phone") or "").strip() or None
        employee_number = (request.form.get("employee_number") or "").strip() or None
        raw_sa_id = (request.form.get("sa_id_number") or "").strip()
        sa_id = "".join(ch for ch in raw_sa_id if ch.isdigit()) if raw_sa_id else None
        dob, dob_error = _admin_dob_from_form()

        if not full_name:
            flash("First name is required.", "error")
            return redirect(url_for("admin.profile"))
        if not email:
            flash("Email is required.", "error")
            return redirect(url_for("admin.profile"))
        if dob_error:
            flash(dob_error, "error")
            return redirect(url_for("admin.profile"))
        if title and title not in TITLE_OPTIONS:
            flash("Please select a valid title.", "error")
            return redirect(url_for("admin.profile"))
        if gender and gender not in GENDER_OPTIONS:
            flash("Please select a valid gender.", "error")
            return redirect(url_for("admin.profile"))
        if User.query.filter(User.email == email, User.id != admin.id).first():
            flash("That email is already used by another account.", "error")
            return redirect(url_for("admin.profile"))
        if employee_number and User.query.filter(User.employee_number == employee_number, User.id != admin.id).first():
            flash("That employee number is already used by another account.", "error")
            return redirect(url_for("admin.profile"))
        if sa_id and User.query.filter(User.sa_id_number == sa_id, User.id != admin.id).first():
            flash("That SA ID number is already used by another account.", "error")
            return redirect(url_for("admin.profile"))

        avatar = request.files.get("avatar")
        if avatar and avatar.filename:
            ext = os.path.splitext(avatar.filename)[1].lower()
            if ext not in ALLOWED_AVATAR_EXT:
                flash("Profile picture must be PNG/JPG/GIF/WEBP.", "error")
                return redirect(url_for("admin.profile"))
            filename = secure_filename(f"{admin.id}{ext}")
            avatar.save(os.path.join(current_app.config["AVATAR_UPLOAD_DIR"], filename))
            admin.avatar_url = (
                url_for("static", filename=f"avatars/{filename}")
                + f"?v={uuid.uuid4().hex[:6]}"
            )

        admin.title = title
        admin.full_name = full_name
        admin.surname = surname
        admin.gender = gender
        admin.email = email
        admin.phone = phone
        admin.employee_number = employee_number
        admin.sa_id_number = sa_id
        admin.date_of_birth = dob

        log_audit(current_user.id, "update_admin_profile", "user", admin.id)
        db.session.commit()
        flash("Profile updated successfully.", "success")
        return redirect(url_for("admin.profile"))

    return render_template(
        "admin/profile.html",
        admin=admin,
        title_options=TITLE_OPTIONS,
        gender_options=GENDER_OPTIONS,
    )


@bp.route("/online-users")
def online_users():
    """Paginated grid of users with online/offline presence."""
    from datetime import datetime, timedelta
    now = datetime.now()
    page = max(1, int(request.args.get("page", 1)))
    per_page = 12
    online_cutoff = now - timedelta(seconds=ONLINE_WINDOW_SECONDS)
    q = User.query.filter(User.deleted_at.is_(None))
    total = q.count()
    online_count = (User.query
                    .filter(User.deleted_at.is_(None))
                    .filter(User.last_seen != None)  # noqa: E711
                    .filter(User.last_seen >= online_cutoff)
                    .count())
    users_page = (
        q.options(selectinload(User.user_roles))
        .order_by(User.last_seen.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )
    pages = max(1, (total + per_page - 1) // per_page)
    return render_template(
        "admin/online_users.html",
        users=users_page, page=page, pages=pages, total=total,
        online_count=online_count, offline_count=max(total - online_count, 0),
        ROLE_LABELS=ROLE_LABELS, online_window_minutes=ONLINE_WINDOW_MINUTES,
        now=now, online_cutoff=online_cutoff, last_seen_age_label=last_seen_age_label,
    )


@bp.route("/landing-team", methods=["GET", "POST"])
def landing_team_pictures():
    _require_super_admin()
    from ..landing_team import (
        TEAM_MEMBER_SPECS,
        landing_team_members,
        landing_team_picture_filename,
    )

    if request.method == "POST":
        student_number = "".join(
            character
            for character in (request.form.get("student_number") or "")
            if character.isdigit()
        )
        member = next(
            (
                spec for spec in TEAM_MEMBER_SPECS
                if spec["student_number"] == student_number
            ),
            None,
        )
        picture = request.files.get("picture")
        if not member:
            flash("Select a valid landing-page team member.", "error")
            return redirect(url_for("admin.landing_team_pictures"))
        if not picture or not picture.filename:
            flash("Choose a picture to upload.", "error")
            return redirect(url_for("admin.landing_team_pictures"))

        picture.stream.seek(0, os.SEEK_END)
        picture_size = picture.stream.tell()
        picture.stream.seek(0)
        if picture_size > LANDING_TEAM_PICTURE_MAX_BYTES:
            flash("Landing-page pictures must be 5 MB or smaller.", "error")
            return redirect(url_for("admin.landing_team_pictures"))

        try:
            image = Image.open(picture.stream)
            image.verify()
            picture.stream.seek(0)
            image = Image.open(picture.stream)
            image.thumbnail((1200, 1200))
            if image.mode not in ("RGB", "L"):
                background = Image.new("RGB", image.size, "white")
                if "A" in image.getbands():
                    background.paste(image, mask=image.getchannel("A"))
                else:
                    background.paste(image.convert("RGB"))
                image = background
            elif image.mode == "L":
                image = image.convert("RGB")
        except (UnidentifiedImageError, OSError, ValueError):
            flash("Upload a valid PNG, JPG, GIF, or WEBP picture.", "error")
            return redirect(url_for("admin.landing_team_pictures"))

        os.makedirs(current_app.config["AVATAR_UPLOAD_DIR"], exist_ok=True)
        filename = landing_team_picture_filename(student_number)
        target = os.path.join(current_app.config["AVATAR_UPLOAD_DIR"], filename)
        temporary_target = f"{target}.{uuid.uuid4().hex}.tmp"
        try:
            image.save(temporary_target, format="JPEG", quality=88, optimize=True)
            os.replace(temporary_target, target)
        finally:
            if os.path.exists(temporary_target):
                os.remove(temporary_target)

        log_audit(
            current_user.id,
            "update_landing_team_picture",
            "landing_team_member",
            student_number,
            {"name": member["name"], "student_number": student_number},
        )
        db.session.commit()
        flash(f"Landing-page picture updated for {member['name']}.", "success")
        return redirect(url_for("admin.landing_team_pictures"))

    members = landing_team_members(current_app.config["AVATAR_UPLOAD_DIR"])
    return render_template("admin/landing_team.html", members=members)


# ---------- Dashboard with charts ----------
@bp.route("/")
def dashboard():
    users = (
        User.query
        .options(selectinload(User.user_roles))
        .filter(User.deleted_at.is_(None))
        .all()
    )
    pending_count = sum(1 for u in users if u.is_pending)
    blocked_count = sum(1 for u in users if getattr(u, "is_blocked", False))

    # Users by role (for pie chart)
    role_counts = {ROLE_LABELS[r]: 0 for r in ROLES}
    role_counts["Pending"] = 0
    for u in users:
        if u.is_pending:
            role_counts["Pending"] += 1
        elif u.has_role("super_admin"):
            role_counts[ROLE_LABELS["super_admin"]] += 1
        else:
            role_counts[ROLE_LABELS[u.primary_role]] += 1

    # Requests by status (bar chart)
    status_rows = (db.session.query(TestRequest.status, func.count(TestRequest.id))
                   .group_by(TestRequest.status).all())
    status_counts = {s: 0 for s in REQUEST_STATUSES}
    for s, n in status_rows:
        status_counts[s] = n

    # Requests over the last 14 days (line chart)
    today = datetime.now().date()
    days = [(today - timedelta(days=i)) for i in range(13, -1, -1)]
    by_day = {d.isoformat(): 0 for d in days}
    cutoff = datetime.combine(days[0], datetime.min.time())
    rows = (db.session.query(func.date(TestRequest.created_at), func.count(TestRequest.id))
            .filter(TestRequest.created_at >= cutoff)
            .group_by(func.date(TestRequest.created_at)).all())
    for d, n in rows:
        key = d if isinstance(d, str) else d.isoformat()
        if key in by_day:
            by_day[key] = n

    return render_template(
        "admin/dashboard.html",
        user_count=len(users),
        pending_count=pending_count,
        blocked_count=blocked_count,
        request_count=TestRequest.query.count(),
        patient_count=Patient.query.filter(Patient.deleted_at.is_(None)).count(),
        logs=AuditLog.query.order_by(AuditLog.created_at.desc()).limit(15).all(),
        chart_roles_labels=list(role_counts.keys()),
        chart_roles_values=list(role_counts.values()),
        chart_status_labels=list(status_counts.keys()),
        chart_status_values=list(status_counts.values()),
        chart_days_labels=list(by_day.keys()),
        chart_days_values=list(by_day.values()),
    )


# ---------- Users & roles ----------
@bp.route("/users")
def users():
    status = request.args.get("status", "all")
    search = (request.args.get("q") or "").strip()

    query = (
        User.query
        .options(selectinload(User.user_roles), selectinload(User.patient_record))
        .filter(User.deleted_at.is_(None))
    )

    if status == "blocked":
        query = query.filter(User.is_blocked == True)

    elif status == "active":
        query = query.filter(User.is_blocked == False, User.is_deactivated == False)

    if search:
        like = f"%{search}%"
        id_search = "".join(ch for ch in search if ch.isdigit())
        id_like = f"%{id_search}%" if id_search else like
        search_lower = search.lower()
        matched_roles = [
            role for role, label in ROLE_LABELS.items()
            if search_lower in role.replace("_", " ").lower()
            or search_lower in label.lower()
        ]
        pending_terms = {"pending", "awaiting", "awaiting role", "no role", "unassigned"}
        criteria = [
            User.full_name.ilike(like),
            User.surname.ilike(like),
            User.email.ilike(like),
            User.id.ilike(like),
            User.phone.ilike(like),
            User.employee_number.ilike(like),
            User.sa_id_number.ilike(id_like),
            User.hpcsa_number.ilike(like),
            User.user_roles.any(UserRole.role.ilike(like)),
            User.patient_record.has(or_(
                Patient.full_name.ilike(like),
                Patient.surname.ilike(like),
                Patient.email.ilike(like),
                Patient.phone.ilike(like),
                Patient.id_number.ilike(id_like),
                Patient.mrn.ilike(like),
            )),
        ]
        if matched_roles:
            criteria.append(User.user_roles.any(UserRole.role.in_(matched_roles)))
        if search_lower in pending_terms:
            criteria.append(~User.user_roles.any())
        query = query.filter(or_(*criteria))

    rows = query.order_by(User.created_at.desc()).all()

    return render_template(
        "admin/users.html",
        users=rows,
        status=status,
        q=search,
        ROLES=_admin_role_choices(),
        ROLE_LABELS=ROLE_LABELS
    )


@bp.route("/users/<user_id>")
def user_detail(user_id):
    u = db.session.get(User, user_id)
    if not u:
        abort(404)
    password_access = _latest_password_request(current_user.id, u.id)
    can_request_password_access = bool(
        u.id != current_user.id
        and not u.has_role("super_admin")
        and not u.deleted_at
        and _available_super_admin_ids(exclude_user_id=current_user.id)
    )
    return render_template(
        "admin/user_detail.html",
        u=u,
        ROLES=_admin_role_choices(),
        ROLE_LABELS=ROLE_LABELS,
        password_access=password_access,
        can_request_password_access=can_request_password_access,
        is_super_admin=_is_super_admin(current_user),
    )


@bp.route("/users/<user_id>/role", methods=["POST"])
def set_role(user_id):
    u = db.session.get(User, user_id)
    if not u:
        abort(404)
    new_role = request.form.get("role", "").strip()
    if new_role not in ROLES:
        flash("Invalid role.", "error")
        return redirect(url_for("admin.users"))
    if new_role == "super_admin":
        abort(403)
    if u.has_role("super_admin") and not _is_super_admin(current_user):
        abort(403)
    if u.id == current_user.id:
        flash("You cannot remove your own administrator access.", "error")
        return redirect(request.referrer or url_for("admin.users"))
    UserRole.query.filter_by(user_id=u.id).delete()
    db.session.add(UserRole(user_id=u.id, role=new_role))
    if new_role == "patient" and not u.patient_record:
        db.session.add(Patient(
            profile_id=u.id, mrn="MRN-" + u.id[:8],
            full_name=u.full_name or u.email, email=u.email,
        ))
    notify(u.id, "Your access has been granted",
           f"You are now a {ROLE_LABELS[new_role]}. Sign in to access your dashboard.", "/app")
    log_audit(current_user.id, "assign_role", "user", u.id, {"role": new_role})
    db.session.commit()
    flash(f"{u.email} is now {ROLE_LABELS[new_role]}.", "success")
    return redirect(request.referrer or url_for("admin.users"))


@bp.route("/users/<user_id>/revoke", methods=["POST"])
def revoke(user_id):
    u = db.session.get(User, user_id)
    if not u:
        abort(404)
    if u.has_role("super_admin") and not _is_super_admin(current_user):
        abort(403)
    if u.id == current_user.id:
        flash("You cannot revoke your own roles.", "error")
        return redirect(request.referrer or url_for("admin.users"))
    UserRole.query.filter_by(user_id=u.id).delete()
    log_audit(current_user.id, "revoke_roles", "user", u.id)
    db.session.commit()
    flash(f"Roles revoked for {u.email}.", "success")
    return redirect(request.referrer or url_for("admin.users"))


@bp.route("/users/<user_id>/block", methods=["POST"])
def block(user_id):
    u = db.session.get(User, user_id)
    if not u:
        abort(404)
    if u.id == current_user.id:
        flash("You cannot block your own account.", "error")
        return redirect(request.referrer or url_for("admin.users"))
    if u.has_role("super_admin") and not _is_super_admin(current_user):
        abort(403)
    u.is_blocked = True
    log_audit(current_user.id, "block_user", "user", u.id)
    db.session.commit()
    flash(f"{u.email} has been blocked.", "success")
    return redirect(request.referrer or url_for("admin.users"))


@bp.route("/users/<user_id>/unblock", methods=["POST"])
def unblock(user_id):
    u = db.session.get(User, user_id)
    if not u:
        abort(404)
    u.is_blocked = False
    log_audit(current_user.id, "unblock_user", "user", u.id)
    db.session.commit()
    flash(f"{u.email} has been unblocked.", "success")
    return redirect(request.referrer or url_for("admin.users"))


@bp.route("/users/<user_id>/activate", methods=["POST"])
def activate(user_id):
    """Reactivate a soft-deleted (self-deactivated) account."""
    u = db.session.get(User, user_id)
    if not u:
        abort(404)
    if u.deleted_at:
        flash("Restore deleted users from the recycle bin.", "error")
        return redirect(url_for("manager.recycle_bin"))
    u.is_deactivated = False
    u.deactivated_at = None
    log_audit(current_user.id, "activate_user", "user", u.id)
    db.session.commit()
    flash(f"{u.email} has been reactivated.", "success")
    return redirect(request.referrer or url_for("admin.users"))


@bp.route("/users/<user_id>/reset-password", methods=["POST"])
def reset_password(user_id):
    u = db.session.get(User, user_id)
    if not u:
        abort(404)
    flash(
        "Direct password resets are disabled. Use the dual-control password access request.",
        "error",
    )
    return redirect(url_for("admin.user_detail", user_id=u.id))


@bp.route("/users/<user_id>/password-access/request", methods=["POST"])
def request_password_access(user_id):
    target = db.session.get(User, user_id)
    if not target or target.deleted_at:
        abort(404)
    if target.id == current_user.id:
        flash("You cannot request access to your own password.", "error")
        return redirect(url_for("admin.user_detail", user_id=target.id))
    if target.has_role("super_admin"):
        flash("Super administrator passwords cannot be reset through this workflow.", "error")
        return redirect(url_for("admin.user_detail", user_id=target.id))
    if not current_user.check_password(request.form.get("current_password") or ""):
        flash("Your administrator password is incorrect.", "error")
        return redirect(url_for("admin.user_detail", user_id=target.id))

    approver_ids = _available_super_admin_ids(exclude_user_id=current_user.id)
    if not approver_ids:
        flash("A different active super administrator is required to approve this request.", "error")
        return redirect(url_for("admin.user_detail", user_id=target.id))

    existing_state = _latest_password_request(current_user.id, target.id)
    if existing_state["status"] in {"pending", "approved", "revealed"}:
        flash("You already have an active password access request for this user.", "error")
        return redirect(url_for("admin.user_detail", user_id=target.id))

    entry = AuditLog(
        actor_id=current_user.id,
        action=PASSWORD_ACCESS_REQUESTED,
        entity_type="user",
        entity_id=target.id,
        details=json.dumps({
            "requested_by": current_user.id,
            "target_user_id": target.id,
            "expires_after_hours": PASSWORD_ACCESS_REQUEST_TTL_HOURS,
        }),
    )
    db.session.add(entry)
    db.session.flush()
    for approver_id in approver_ids:
        notify(
            approver_id,
            "Password access approval required",
            (
                f"{current_user.full_name or current_user.email} requested a temporary "
                f"password for {target.full_name or target.email}."
            ),
            url_for("admin.password_access_requests"),
        )
    db.session.commit()
    flash("Password access request sent to a super administrator.", "success")
    return redirect(url_for("admin.user_detail", user_id=target.id))


@bp.route("/password-access")
def password_access_requests():
    _require_super_admin()
    request_entries = (
        AuditLog.query
        .filter_by(action=PASSWORD_ACCESS_REQUESTED, entity_type="user")
        .order_by(AuditLog.created_at.desc())
        .limit(200)
        .all()
    )
    user_ids = {
        user_id
        for entry in request_entries
        for user_id in (entry.actor_id, entry.entity_id)
        if user_id
    }
    users_by_id = {
        user.id: user
        for user in User.query.filter(User.id.in_(user_ids)).all()
    } if user_ids else {}
    rows = []
    for entry in request_entries:
        target = users_by_id.get(entry.entity_id)
        rows.append({
            "request": entry,
            "requester": users_by_id.get(entry.actor_id),
            "target": target,
            "state": _password_access_state(entry, target),
        })
    return render_template(
        "admin/password_access_requests.html",
        pending_rows=[row for row in rows if row["state"]["status"] == "pending"],
        history_rows=[row for row in rows if row["state"]["status"] != "pending"],
        approval_minutes=PASSWORD_ACCESS_APPROVAL_MINUTES,
    )


@bp.route("/password-access/<request_id>/approve", methods=["POST"])
def approve_password_access(request_id):
    _require_super_admin()
    entry = _password_request_or_404(request_id)
    if entry.actor_id == current_user.id:
        flash("The requesting administrator cannot approve their own request.", "error")
        return redirect(url_for("admin.password_access_requests"))
    if not current_user.check_password(request.form.get("current_password") or ""):
        flash("Your super administrator password is incorrect.", "error")
        return redirect(url_for("admin.password_access_requests"))
    if _password_request_decision(entry.id):
        flash("This request has already been decided.", "error")
        return redirect(url_for("admin.password_access_requests"))
    if _password_access_state(entry)["status"] != "pending":
        flash("This request has expired and can no longer be approved.", "error")
        return redirect(url_for("admin.password_access_requests"))

    requester = db.session.get(User, entry.actor_id)
    target = db.session.get(User, entry.entity_id)
    if not requester or not requester.has_role("admin") or not target or target.deleted_at:
        flash("The requester or target account is no longer available.", "error")
        return redirect(url_for("admin.password_access_requests"))
    if target.has_role("super_admin"):
        flash("Super administrator passwords cannot be reset through this workflow.", "error")
        return redirect(url_for("admin.password_access_requests"))

    expires_at = datetime.now() + timedelta(minutes=PASSWORD_ACCESS_APPROVAL_MINUTES)
    db.session.add(AuditLog(
        actor_id=current_user.id,
        action=PASSWORD_ACCESS_APPROVED,
        entity_type="password_access_request",
        entity_id=entry.id,
        details=json.dumps({
            "requested_by": requester.id,
            "target_user_id": target.id,
            "approved_by": current_user.id,
            "expires_at": expires_at.isoformat(),
        }),
    ))
    notify(
        requester.id,
        "Password access request approved",
        (
            f"Your request for {target.full_name or target.email} was approved. "
            f"Reveal it within {PASSWORD_ACCESS_APPROVAL_MINUTES} minutes."
        ),
        url_for("admin.user_detail", user_id=target.id),
    )
    db.session.commit()
    flash("Password access approved.", "success")
    return redirect(url_for("admin.password_access_requests"))


@bp.route("/password-access/<request_id>/deny", methods=["POST"])
def deny_password_access(request_id):
    _require_super_admin()
    entry = _password_request_or_404(request_id)
    if entry.actor_id == current_user.id:
        flash("The requesting administrator cannot decide their own request.", "error")
        return redirect(url_for("admin.password_access_requests"))
    if not current_user.check_password(request.form.get("current_password") or ""):
        flash("Your super administrator password is incorrect.", "error")
        return redirect(url_for("admin.password_access_requests"))
    if _password_request_decision(entry.id):
        flash("This request has already been decided.", "error")
        return redirect(url_for("admin.password_access_requests"))

    requester = db.session.get(User, entry.actor_id)
    target = db.session.get(User, entry.entity_id)
    db.session.add(AuditLog(
        actor_id=current_user.id,
        action=PASSWORD_ACCESS_DENIED,
        entity_type="password_access_request",
        entity_id=entry.id,
        details=json.dumps({
            "requested_by": entry.actor_id,
            "target_user_id": entry.entity_id,
            "denied_by": current_user.id,
        }),
    ))
    if requester:
        notify(
            requester.id,
            "Password access request denied",
            f"Your request for {(target.full_name or target.email) if target else 'the selected user'} was denied.",
            url_for("admin.users"),
        )
    db.session.commit()
    flash("Password access denied.", "success")
    return redirect(url_for("admin.password_access_requests"))


@bp.route("/password-access/<request_id>/reveal", methods=["POST"])
def reveal_password_access(request_id):
    entry = _password_request_or_404(request_id)
    if entry.actor_id != current_user.id:
        abort(404)
    target = db.session.get(User, entry.entity_id)
    if not target or target.deleted_at:
        abort(404)
    if target.has_role("super_admin"):
        abort(403)
    if not current_user.check_password(request.form.get("current_password") or ""):
        flash("Your administrator password is incorrect.", "error")
        return redirect(url_for("admin.user_detail", user_id=target.id))

    state = _password_access_state(entry, target)
    if state["status"] not in {"approved", "revealed"}:
        flash("This approval is unavailable, expired, or already consumed.", "error")
        return redirect(url_for("admin.user_detail", user_id=target.id))

    if state["status"] == "revealed":
        temporary_password = target.temp_password
    else:
        temporary_password = secrets.token_urlsafe(12) + "A1!"
        target.set_password(temporary_password)
        target.must_change_password = True
        target.temp_password = temporary_password
        db.session.add(AuditLog(
            actor_id=current_user.id,
            action=PASSWORD_ACCESS_REVEALED,
            entity_type="password_access_request",
            entity_id=entry.id,
            details=json.dumps({
                "requested_by": current_user.id,
                "target_user_id": target.id,
                "approved_by": state["decision"].actor_id,
                "password_fingerprint": hashlib.sha256(
                    temporary_password.encode("utf-8")
                ).hexdigest(),
            }),
        ))
        notify(
            target.id,
            "Temporary password issued",
            (
                "An approved administrator issued a temporary password for your account. "
                "You must choose a new password the next time you sign in."
            ),
            url_for("auth.change_password"),
        )
        db.session.commit()
        send_email(
            [target.email],
            "Your MediLab Connect password was reset",
            (
                f"Hello {target.full_name or target.email},\n\n"
                "A temporary password was issued through the administrator approval workflow. "
                "Contact your administrator securely to receive it.\n\n"
                "You will be required to choose a new password the next time you sign in.\n\n"
                "- MediLab Connect"
            ),
        )

    response = make_response(render_template(
        "admin/password_reveal.html",
        target=target,
        temporary_password=temporary_password,
        expires_at=state["expires_at"],
    ))
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
    response.headers["Pragma"] = "no-cache"
    return response


@bp.route("/users/<user_id>/delete", methods=["POST"])
def delete_user(user_id):
    u = db.session.get(User, user_id)
    if not u:
        abort(404)
    if u.id == current_user.id:
        flash("You cannot delete your own account.", "error")
        return redirect(url_for("admin.users"))
    if u.has_role("super_admin") and not _is_super_admin(current_user):
        abort(403)
    email = u.email
    soft_delete(u, current_user.id)
    if u.patient_record:
        soft_delete(u.patient_record, current_user.id)
    log_audit(current_user.id, "soft_delete_user", "user", user_id)
    db.session.commit()
    flash(f"{email} moved to recycle bin.", "success")
    return redirect(url_for("admin.users"))


# ---------- Generic admin tables ----------
@bp.route("/patients")
def patients():
    rows = Patient.query.filter(Patient.deleted_at.is_(None)).order_by(Patient.created_at.desc()).all()
    return render_template("admin/patients.html", rows=rows)


@bp.route("/tests")
def tests():
    rows = TestCatalog.query.filter(TestCatalog.deleted_at.is_(None)).order_by(TestCatalog.code).all()
    sample_types = (
        SampleType.query
        .filter(SampleType.active.is_(True))
        .order_by(SampleType.name)
        .all()
    )
    return render_template("admin/tests.html", rows=rows, sample_types=sample_types)


@bp.route("/tests/new", methods=["POST"])
def tests_new():
    code = (request.form.get("code") or "").strip().upper()
    name = (request.form.get("name") or "").strip()
    cat = (request.form.get("category") or "General").strip()
    sample_type = (request.form.get("sample_type") or "").strip()
    units = request.form.get("units") or None
    tat = int(request.form.get("turnaround_hours") or 24)
    if not code or not name:
        flash("Code and name required.", "error")
    elif not sample_type:
        flash("Sample type is required.", "error")
    elif not SampleType.query.filter_by(name=sample_type, active=True).first():
        flash("Select a valid active sample type.", "error")
    elif TestCatalog.query.filter_by(code=code).first():
        flash("Code already exists.", "error")
    else:
        db.session.add(TestCatalog(code=code, name=name, category=cat,
                                   sample_type=sample_type, units=units,
                                   turnaround_hours=tat, active=True))
        log_audit(current_user.id, "create_test", "test_catalog", code)
        db.session.commit()
        flash("Test added.", "success")
    return redirect(url_for("admin.tests"))


@bp.route("/tests/<tid>/toggle", methods=["POST"])
def tests_toggle(tid):
    t = db.session.get(TestCatalog, tid)
    if not t: abort(404)
    t.active = not t.active
    db.session.commit()
    return redirect(url_for("admin.tests"))


@bp.route("/tests/<tid>/delete", methods=["POST"])
def tests_delete(tid):
    t = db.session.get(TestCatalog, tid)
    if not t: abort(404)
    soft_delete(t, current_user.id)
    log_audit(current_user.id, "soft_delete_test", "test_catalog", t.id)
    db.session.commit()
    flash("Test moved to recycle bin.", "success")
    return redirect(url_for("admin.tests"))


@bp.route("/requests")
def requests_list():
    rows = TestRequest.query.order_by(TestRequest.created_at.desc()).limit(200).all()
    return render_template("admin/requests.html", rows=rows)


@bp.route("/audit")
def audit():
    rows = AuditLog.query.order_by(AuditLog.created_at.desc()).limit(500).all()
    for row in rows:
        row.display_details = _audit_detail_rows(row.details)
    return render_template("admin/audit.html", rows=rows)


# ---------- Medical-history catalogues (conditions / allergies / medications) ----------
CATALOG_MODELS = {
    "conditions": (Condition, "Condition"),
    "allergies":  (Allergy,   "Allergy"),
    "medications":(Medication,"Medication"),
}


@bp.route("/catalog/<kind>")
def catalog_list(kind):
    if kind not in CATALOG_MODELS:
        abort(404)
    Model, label = CATALOG_MODELS[kind]
    rows = Model.query.filter(Model.deleted_at.is_(None)).order_by(Model.category, Model.name).all()
    cats = sorted({r.category for r in rows})
    return render_template("admin/catalog.html", rows=rows, kind=kind, label=label, categories=cats)


@bp.route("/catalog/<kind>/new", methods=["POST"])
def catalog_new(kind):
    if kind not in CATALOG_MODELS: abort(404)
    Model, label = CATALOG_MODELS[kind]
    name = (request.form.get("name") or "").strip()
    cat  = (request.form.get("category") or "General").strip() or "General"
    desc = (request.form.get("description") or "").strip() or None
    if not name:
        flash("Name is required.", "error")
    elif Model.query.filter(func.lower(Model.name) == name.lower()).first():
        flash(f"{label} '{name}' already exists.", "error")
    else:
        db.session.add(Model(name=name, category=cat, description=desc, active=True))
        log_audit(current_user.id, f"create_{kind}", kind, name)
        db.session.commit()
        flash(f"{label} added.", "success")
    return redirect(url_for("admin.catalog_list", kind=kind))


@bp.route("/catalog/<kind>/<rid>/toggle", methods=["POST"])
def catalog_toggle(kind, rid):
    if kind not in CATALOG_MODELS: abort(404)
    Model, _ = CATALOG_MODELS[kind]
    r = db.session.get(Model, rid)
    if not r: abort(404)
    r.active = not r.active
    db.session.commit()
    return redirect(url_for("admin.catalog_list", kind=kind))


@bp.route("/catalog/<kind>/<rid>/delete", methods=["POST"])
def catalog_delete(kind, rid):
    if kind not in CATALOG_MODELS: abort(404)
    Model, label = CATALOG_MODELS[kind]
    r = db.session.get(Model, rid)
    if not r: abort(404)
    soft_delete(r, current_user.id)
    log_audit(current_user.id, f"soft_delete_{kind}", kind, r.id)
    db.session.commit()
    flash(f"{label} moved to recycle bin.", "success")
    return redirect(url_for("admin.catalog_list", kind=kind))


@bp.route("/notifications")
def notifications():
    rows = Notification.query.order_by(Notification.created_at.desc()).limit(200).all()
    return render_template("admin/notifications.html", rows=rows)


# ---------- Admin reports ----------
@bp.route("/reports")
def reports():
    from flask import send_file
    from reportlab.lib.units import mm
    from ..reports import build_report_pdf, parse_range
    frm, to, start, end = parse_range(request.args)

    live_users = User.query.filter(User.deleted_at.is_(None))
    total_users = live_users.count()
    active_users = live_users.filter(
        User.is_blocked.is_(False),
        User.is_deactivated.is_(False),
    ).count()
    blocked_users = live_users.filter(User.is_blocked.is_(True)).count()
    deactivated_users = live_users.filter(User.is_deactivated.is_(True)).count()
    deleted_users = User.query.filter(User.deleted_at.isnot(None)).count()

    user_rows = (
        db.session.query(UserRole.role, func.count(UserRole.id))
        .join(User, User.id == UserRole.user_id)
        .filter(User.deleted_at.is_(None))
        .group_by(UserRole.role)
        .order_by(func.count(UserRole.id).desc())
        .all()
    )

    request_range = TestRequest.query.filter(TestRequest.created_at.between(start, end))
    total_requests = request_range.count()
    total_items = (
        db.session.query(func.count(TestRequestItem.id))
        .join(TestRequest, TestRequest.id == TestRequestItem.request_id)
        .filter(TestRequest.created_at.between(start, end))
        .scalar()
        or 0
    )
    abnormal_results = (
        db.session.query(func.count(TestRequestItem.id))
        .join(TestRequest, TestRequest.id == TestRequestItem.request_id)
        .filter(
            TestRequest.created_at.between(start, end),
            TestRequestItem.abnormal_flag.isnot(None),
        )
        .scalar()
        or 0
    )
    released_requests = request_range.filter(TestRequest.status == "released").count()
    cancelled_requests = request_range.filter(TestRequest.status == "cancelled").count()

    req_rows = (
        db.session.query(TestRequest.status, func.count(TestRequest.id))
        .filter(TestRequest.created_at.between(start, end))
        .group_by(TestRequest.status)
        .order_by(func.count(TestRequest.id).desc())
        .all()
    )
    priority_rows = (
        db.session.query(TestRequest.priority, func.count(TestRequest.id))
        .filter(TestRequest.created_at.between(start, end))
        .group_by(TestRequest.priority)
        .order_by(func.count(TestRequest.id).desc())
        .all()
    )
    by_doctor = (
        db.session.query(User.full_name, User.email, func.count(TestRequest.id))
        .join(TestRequest, TestRequest.doctor_id == User.id)
        .filter(TestRequest.created_at.between(start, end))
        .group_by(User.id, User.full_name, User.email)
        .order_by(func.count(TestRequest.id).desc())
        .limit(10)
        .all()
    )
    trend_days = []
    trend_counts = {}
    for offset in range((to - frm).days + 1):
        day = frm + timedelta(days=offset)
        trend_days.append(day)
        trend_counts[day.isoformat()] = 0
    for created_at, in (
        db.session.query(TestRequest.created_at)
        .filter(TestRequest.created_at.between(start, end))
        .all()
    ):
        if created_at:
            key = created_at.date().isoformat()
            if key in trend_counts:
                trend_counts[key] += 1
    account_rows = [
        ["Live users", total_users],
        ["Active users", active_users],
        ["Blocked users", blocked_users],
        ["Deactivated users", deactivated_users],
        ["Deleted users", deleted_users],
    ]
    workflow_rows = [
        ["Requests created", total_requests],
        ["Test items created", total_items],
        ["Released requests", released_requests],
        ["Cancelled requests", cancelled_requests],
        ["Abnormal result items", abnormal_results],
    ]

    if request.args.get("format") == "pdf":
        sections = [
            {"heading": "Account health",
             "headers": ["Indicator", "Count"],
             "rows": account_rows,
             "col_widths": [125 * mm, 55 * mm]},
            {"heading": "Users by role",
             "headers": ["Role", "Count"],
             "rows": [[ROLE_LABELS.get(r, r), n] for r, n in user_rows] or [["No users", 0]],
             "col_widths": [125 * mm, 55 * mm]},
            {"heading": "Workflow summary",
             "headers": ["Indicator", "Count"],
             "rows": workflow_rows,
             "col_widths": [125 * mm, 55 * mm]},
            {"heading": "Requests by status (in range)",
             "headers": ["Status", "Count"],
             "rows": [[(s or "-").replace("_", " ").title(), n] for s, n in req_rows] or [["No requests", 0]],
             "col_widths": [125 * mm, 55 * mm],
             "page_break": True},
            {"heading": "Requests by priority (in range)",
             "headers": ["Priority", "Count"],
             "rows": [[(p or "-").title(), n] for p, n in priority_rows] or [["No requests", 0]],
             "col_widths": [125 * mm, 55 * mm]},
            {"heading": "Top doctors by requests (in range)",
             "headers": ["Doctor", "Requests"],
             "rows": [[(name or email or "-"), n] for name, email, n in by_doctor] or [["No data", 0]],
             "col_widths": [125 * mm, 55 * mm]},
        ]
        buf = build_report_pdf(
            "Administrator Report",
            subtitle=f"Range: {frm:%Y-%m-%d} to {to:%Y-%m-%d}",
            summary=[f"Live users: <b>{total_users}</b>",
                     f"Requests in range: <b>{total_requests}</b>",
                     f"Released requests: <b>{released_requests}</b>"],
            sections=sections,
        )
        return send_file(buf, mimetype="application/pdf", as_attachment=True,
                         download_name=f"admin-report-{frm}_{to}.pdf")
    return render_template("admin/reports.html",
                           frm=frm, to=to,
                           user_rows=user_rows, req_rows=req_rows,
                           priority_rows=priority_rows,
                           by_doctor=by_doctor,
                           account_rows=account_rows, workflow_rows=workflow_rows,
                           total_users=total_users, total_requests=total_requests,
                           abnormal_results=abnormal_results,
                           account_chart_labels=[label for label, _count in account_rows],
                           account_chart_values=[count for _label, count in account_rows],
                           workflow_chart_labels=[label for label, _count in workflow_rows],
                           workflow_chart_values=[count for _label, count in workflow_rows],
                           user_chart_labels=[ROLE_LABELS.get(role, role) for role, _count in user_rows],
                           user_chart_values=[count for _role, count in user_rows],
                           status_chart_labels=[(status or "-").replace("_", " ").title() for status, _count in req_rows],
                           status_chart_values=[count for _status, count in req_rows],
                           priority_chart_labels=[(priority or "-").title() for priority, _count in priority_rows],
                           priority_chart_values=[count for _priority, count in priority_rows],
                           trend_chart_labels=[day.strftime("%d %b") for day in trend_days],
                           trend_chart_values=[trend_counts[day.isoformat()] for day in trend_days],
                           doctor_chart_labels=[name or email or "-" for name, email, _count in by_doctor],
                           doctor_chart_values=[count for _name, _email, count in by_doctor],
                           ROLE_LABELS=ROLE_LABELS)
