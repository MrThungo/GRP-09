"""Tiny JSON API consumed by the frontend (notification bell, blocked-user heartbeat, online users)."""
import json
from datetime import datetime, timedelta
from flask import Blueprint, abort, jsonify, request, url_for
from flask_login import login_required, current_user
from sqlalchemy.orm import selectinload
from ..extensions import db
from ..models import Notification, User, UserRole, OnlineConsultation, ConsultationSignal
from ..presence import last_seen_age_label

bp = Blueprint("api", __name__)

ONLINE_WINDOW_MINUTES = 2  # users with last_seen within 2 minutes count as online
ONLINE_WINDOW_SECONDS = ONLINE_WINDOW_MINUTES * 60


def _consultation_participant_or_404(consultation_id, room_token):
    consultation = (OnlineConsultation.query
                    .options(
                        selectinload(OnlineConsultation.patient),
                        selectinload(OnlineConsultation.request),
                    )
                    .filter_by(id=consultation_id)
                    .first())
    if not consultation or consultation.room_token != room_token:
        abort(404)
    if current_user.id not in (consultation.doctor_id, consultation.patient_user_id):
        abort(403)
    return consultation


@bp.route("/me")
def me():
    """Lightweight heartbeat used by the client to detect blocked/expired sessions."""
    if not current_user.is_authenticated:
        return jsonify({"authenticated": False, "blocked": False})
    return jsonify({
        "authenticated": True,
        "blocked": bool(getattr(current_user, "is_blocked", False)),
        "id": current_user.id,
        "email": current_user.email,
        "role": current_user.primary_role,
    })


@bp.route("/notifications")
@login_required
def list_notifications():
    rows = (Notification.query
            .with_entities(
                Notification.id,
                Notification.title,
                Notification.body,
                Notification.link,
                Notification.read,
                Notification.created_at,
            )
            .filter_by(user_id=current_user.id)
            .order_by(Notification.created_at.desc())
            .limit(20).all())
    return jsonify([
        {"id": n.id, "title": n.title, "body": n.body, "link": n.link,
         "read": n.read, "created_at": n.created_at.isoformat()}
        for n in rows
    ])


@bp.route("/notifications/<nid>/read", methods=["POST"])
@login_required
def mark_read(nid):
    updated = (
        Notification.query
        .filter_by(id=nid, user_id=current_user.id)
        .update({"read": True}, synchronize_session=False)
    )
    if not updated:
        return jsonify({"error": "not found"}), 404
    db.session.commit()
    return jsonify({"ok": True})


@bp.route("/online-users")
@login_required
def online_users():
    """Admin-only feed of user online/offline presence."""
    if not current_user.has_role("admin"):
        return jsonify({"error": "forbidden"}), 403
    now = datetime.now()
    cutoff = now - timedelta(seconds=ONLINE_WINDOW_SECONDS)
    page = max(1, int(request.args.get("page", 1)))
    per_page = min(50, max(1, int(request.args.get("per_page", 12))))
    q = User.query
    total = q.count()
    online_count = (User.query
                    .filter(User.last_seen != None)  # noqa: E711
                    .filter(User.last_seen >= cutoff)
                    .count())
    rows = (
        q.options(selectinload(User.user_roles))
        .order_by(User.last_seen.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )
    return jsonify({
        "count": online_count,
        "online_count": online_count,
        "offline_count": max(total - online_count, 0),
        "total_count": total,
        "window_minutes": ONLINE_WINDOW_MINUTES,
        "users": [
            {"id": u.id, "email": u.email, "full_name": u.full_name,
             "role": u.primary_role, "avatar_url": u.avatar_url,
             "last_seen": u.last_seen.isoformat() if u.last_seen else None,
             "last_seen_minutes": int((now - u.last_seen).total_seconds() // 60) if u.last_seen else None,
             "last_seen_label": last_seen_age_label(u.last_seen, now),
             "online": bool(u.last_seen and u.last_seen >= cutoff)}
            for u in rows
        ],
    })


@bp.route("/consultations/<consultation_id>/<room_token>/status")
@login_required
def consultation_status(consultation_id, room_token):
    consultation = _consultation_participant_or_404(consultation_id, room_token)
    is_doctor = current_user.id == consultation.doctor_id
    return jsonify({
        "id": consultation.id,
        "status": consultation.status,
        "started": consultation.status == "started",
        "doctor_started_at": consultation.doctor_started_at.isoformat() if consultation.doctor_started_at else None,
        "scheduled_at": consultation.scheduled_at.isoformat() if consultation.scheduled_at else None,
        "room_url": url_for(
            "doctor.consultation_room" if is_doctor else "patient.consultation_room",
            consultation_id=consultation.id,
            room_token=consultation.room_token,
        ),
    })


@bp.route("/consultations/<consultation_id>/<room_token>/signals", methods=["GET", "POST"])
@login_required
def consultation_signals(consultation_id, room_token):
    consultation = _consultation_participant_or_404(consultation_id, room_token)
    if consultation.status != "started":
        return jsonify({"error": "session_not_started"}), 409

    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        signal_type = (data.get("type") or "").strip()
        if signal_type not in {"ready", "offer", "answer", "ice", "leave"}:
            return jsonify({"error": "invalid_signal_type"}), 400
        db.session.add(ConsultationSignal(
            consultation_id=consultation.id,
            sender_id=current_user.id,
            signal_type=signal_type,
            payload=json.dumps(data.get("payload") or {}),
        ))
        db.session.commit()
        return jsonify({"ok": True})

    rows = (ConsultationSignal.query
            .filter(
                ConsultationSignal.consultation_id == consultation.id,
                ConsultationSignal.sender_id != current_user.id,
            )
            .order_by(ConsultationSignal.created_at.asc())
            .limit(120)
            .all())
    return jsonify({
        "signals": [
            {
                "id": row.id,
                "type": row.signal_type,
                "payload": json.loads(row.payload or "{}"),
                "created_at": row.created_at.isoformat(),
            }
            for row in rows
        ]
    })
