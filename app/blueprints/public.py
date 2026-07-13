from flask import Blueprint, render_template
from flask_login import current_user
from ..models import User
from ..extensions import db
from datetime import datetime as dt
bp = Blueprint("public", __name__, template_folder="../templates/public")

@bp.route("/")
def landing():
    return render_template("public/landing.html", user=current_user, dt=dt)
