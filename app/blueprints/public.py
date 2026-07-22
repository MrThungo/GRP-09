from datetime import datetime as dt

from flask import Blueprint, current_app, render_template
from flask_login import current_user
from sqlalchemy import or_
from sqlalchemy.orm import selectinload

from ..extensions import db
from ..models import User, UserRole

bp = Blueprint("public", __name__, template_folder="../templates/public")


TEAM_MEMBER_SPECS = [
    {
        "name": "Mncina Nomhle",
        "student_number": "224497847",
        "role": "Lab Manager",
        "role_keys": ("lab_manager",),
        "match_terms": ("Mncina Nomhle", "Nomhle Mncina", "224497847"),
    },
    {
        "name": "Papama Xuza",
        "student_number": "224153498",
        "role": "Doctor",
        "role_keys": ("doctor",),
        "match_terms": ("Papama Xuza", "PapamaXuza", "224153498"),
    },
    {
        "name": "Anam Thembani",
        "student_number": "219598274",
        "role": "Lab Technician",
        "role_keys": ("lab_technician",),
        "match_terms": ("Anam Thembani", "Thembani Anam", "219598274"),
    },
    {
        "name": "Ndumiso Thungo",
        "student_number": "221411046",
        "role": "Admin & Patient",
        "role_keys": ("admin", "patient"),
        "match_terms": ("Ndumiso Thungo", "Thungo Ndumiso", "221411046"),
    },
]


def _initials(name):
    parts = [part for part in (name or "").split() if part]
    if not parts:
        return "?"
    return "".join(part[0].upper() for part in parts[:2])


def _normalise(value):
    return "".join(char.lower() for char in str(value or "") if char.isalnum())


def _user_identity_values(user):
    return (
        user.full_name,
        user.surname,
        " ".join(part for part in [user.full_name, user.surname] if part),
        " ".join(part for part in [user.surname, user.full_name] if part),
        user.email,
        user.employee_number,
        user.sa_id_number,
        user.hpcsa_number,
    )


def _identity_matches(spec, user):
    needles = [_normalise(value) for value in spec.get("match_terms", ())]
    haystacks = [_normalise(value) for value in _user_identity_values(user)]
    return any(
        needle and haystack and (needle == haystack or needle in haystack or haystack in needle)
        for needle in needles
        for haystack in haystacks
    )


def _role_matches(spec, user):
    roles = set(user.roles)
    return bool(roles.intersection(spec.get("role_keys", ())))


def _best_team_user(spec, users):
    identity_matches = [user for user in users if _identity_matches(spec, user)]
    if identity_matches:
        return max(identity_matches, key=lambda user: (bool(user.avatar_url), user.updated_at or user.created_at))

    role_keys = set(spec.get("role_keys", ()))
    role_matches = [user for user in users if _role_matches(spec, user)]
    if not role_matches:
        return None

    return max(
        role_matches,
        key=lambda user: (
            bool(user.avatar_url),
            role_keys.issubset(set(user.roles)),
            len(role_keys.intersection(set(user.roles))),
            user.updated_at or user.created_at,
        ),
    )


def _landing_team_members():
    role_keys = sorted({role for member in TEAM_MEMBER_SPECS for role in member.get("role_keys", ())})
    identifiers = sorted({
        term
        for member in TEAM_MEMBER_SPECS
        for term in member.get("match_terms", ())
        if term.isdigit()
    })
    try:
        users = (
            User.query
            .options(selectinload(User.user_roles))
            .outerjoin(UserRole, UserRole.user_id == User.id)
            .filter(
                User.deleted_at.is_(None),
                User.is_blocked.is_(False),
                User.is_deactivated.is_(False),
                or_(
                    UserRole.role.in_(role_keys),
                    User.employee_number.in_(identifiers),
                    User.sa_id_number.in_(identifiers),
                    User.hpcsa_number.in_(identifiers),
                ),
            )
            .distinct()
            .all()
        )
    except Exception as exc:
        db.session.rollback()
        current_app.logger.warning("Landing team avatars could not be loaded: %s", exc)
        users = []
    members = []
    for spec in TEAM_MEMBER_SPECS:
        user = _best_team_user(spec, users)
        members.append({
            **spec,
            "initials": _initials(spec["name"]),
            "avatar_url": user.avatar_url if user and user.avatar_url else "",
        })
    return members


@bp.route("/")
def landing():
    return render_template(
        "public/landing.html",
        user=current_user,
        dt=dt,
        team_members=_landing_team_members(),
    )
