"""Shared personal notification page helpers."""

from flask import flash, redirect, render_template, url_for
from flask_login import current_user

from .extensions import db
from .models import Notification


def render_user_notifications(mark_all_endpoint):
    rows = (
        Notification.query
        .filter_by(user_id=current_user.id)
        .order_by(Notification.created_at.desc())
        .limit(100)
        .all()
    )
    unread_count = sum(1 for row in rows if not row.read)
    return render_template(
        "notifications/index.html",
        rows=rows,
        unread_count=unread_count,
        mark_all_endpoint=mark_all_endpoint,
    )


def mark_user_notifications_read(redirect_endpoint):
    Notification.query.filter_by(user_id=current_user.id, read=False).update(
        {"read": True},
        synchronize_session=False,
    )
    db.session.commit()
    flash("All notifications marked as read.", "success")
    return redirect(url_for(redirect_endpoint))
