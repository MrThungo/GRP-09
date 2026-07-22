from datetime import datetime as dt

from flask import Blueprint, current_app, render_template
from flask_login import current_user

from ..extensions import db
from ..models import User

bp = Blueprint("public", __name__, template_folder="../templates/public")


TEAM_MEMBER_SPECS = [
    {
        "name": "Mncina Nomhle",
        "student_number": "224497847",
        "role": "Lab Manager",
        "sa_id_number": "224497847",
    },
    {
        "name": "Papama Xuza",
        "student_number": "224153498",
        "role": "Doctor",
        "sa_id_number": "224153498",
    },
    {
        "name": "Anam Thembani",
        "student_number": "219598274",
        "role": "Lab Technician",
        "sa_id_number": "219598274",
    },
    {
        "name": "Ndumiso Thungo",
        "student_number": "221411046",
        "role": "Admin & Patient",
        "sa_id_number": "221411046",
    },
]


def _initials(name):
    parts = [part for part in (name or "").split() if part]
    if not parts:
        return "?"
    return "".join(part[0].upper() for part in parts[:2])


def _landing_team_members():
    ids = [member["sa_id_number"] for member in TEAM_MEMBER_SPECS]
    try:
        users = {
            user.sa_id_number: user
            for user in User.query.filter(User.sa_id_number.in_(ids)).all()
            if user.sa_id_number
        }
    except Exception as exc:
        db.session.rollback()
        current_app.logger.warning("Landing team avatars could not be loaded: %s", exc)
        users = {}
    members = []
    for spec in TEAM_MEMBER_SPECS:
        user = users.get(spec["sa_id_number"])
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
