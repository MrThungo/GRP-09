from datetime import datetime as dt

from flask import Blueprint, current_app, render_template, url_for
from flask_login import current_user

from ..landing_team import landing_team_members

bp = Blueprint("public", __name__, template_folder="../templates/public")


def _landing_team_members():
    members = landing_team_members(current_app.config["AVATAR_UPLOAD_DIR"])
    for member in members:
        member["avatar_url"] = (
            url_for("static", filename=f"avatars/{member['picture_filename']}")
            if member["picture_filename"]
            else ""
        )
    return members


@bp.route("/")
def landing():
    return render_template(
        "public/landing.html",
        user=current_user,
        dt=dt,
        team_members=_landing_team_members(),
    )
