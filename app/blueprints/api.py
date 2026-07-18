"""Tiny JSON API consumed by the frontend (notification bell, blocked-user heartbeat, online users)."""
import hashlib
import json
from datetime import datetime, timedelta
from flask import Blueprint, abort, current_app, jsonify, request, url_for
from flask_login import login_required, current_user
from sqlalchemy import func, or_
from sqlalchemy.orm import selectinload
from ..consultation_recordings import (
    append_recording_chunk,
    finalize_chunked_recording,
    store_recording_file,
)
from ..extensions import db
from ..models import (
    AccessRequest,
    ChatMessage,
    Consumable,
    ConsumableOrder,
    DoctorAvailabilitySlot,
    Notification,
    OnlineConsultation,
    Patient,
    Sample,
    StockMovement,
    TestCatalog,
    TestRequest,
    TestRequestItem,
    User,
    UserRole,
    ConsultationSignal,
)
from ..presence import last_seen_age_label

bp = Blueprint("api", __name__)

ONLINE_WINDOW_MINUTES = 2  # users with last_seen within 2 minutes count as online
ONLINE_WINDOW_SECONDS = ONLINE_WINDOW_MINUTES * 60
WAITING_ROOM_WINDOW_SECONDS = 45
LIVE_SNAPSHOT_CACHE_LIMIT = 500


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


def _consultation_waiting_participants(consultation):
    now = datetime.now()
    cutoff = now - timedelta(seconds=WAITING_ROOM_WINDOW_SECONDS)
    rows = (
        ConsultationSignal.query
        .filter(
            ConsultationSignal.consultation_id == consultation.id,
            ConsultationSignal.signal_type == "waiting",
            ConsultationSignal.created_at >= cutoff,
        )
        .order_by(ConsultationSignal.created_at.asc())
        .all()
    )
    participants = []
    for row in rows:
        if row.sender_id == consultation.patient_user_id:
            participants.append({
                "id": row.sender_id,
                "name": consultation.patient.full_name if consultation.patient else "Patient",
                "role": "patient",
                "waiting_since": row.created_at.isoformat(),
            })
    return participants


def _max_dt(query, column):
    return query.with_entities(func.max(column)).scalar()


def _iso(value):
    return value.isoformat() if value else ""


def _group_counts(query, column):
    rows = query.with_entities(column, func.count()).group_by(column).all()
    return "|".join(
        f"{key or 'none'}:{count}"
        for key, count in sorted(rows, key=lambda row: str(row[0] or ""))
    )


def _patient_for_current_user():
    return Patient.query.filter_by(profile_id=current_user.id, deleted_at=None).first()


def _scoped_request_query(role, patient):
    if role == "patient":
        if not patient:
            return TestRequest.query.filter(TestRequest.id == "__none__")
        return TestRequest.query.filter(TestRequest.patient_id == patient.id)
    if role == "doctor":
        return TestRequest.query.filter(TestRequest.doctor_id == current_user.id)
    return TestRequest.query


def _scoped_item_query(role, patient):
    query = TestRequestItem.query.join(TestRequest, TestRequestItem.request_id == TestRequest.id)
    if role == "patient":
        if not patient:
            return query.filter(TestRequest.id == "__none__")
        return query.filter(TestRequest.patient_id == patient.id)
    if role == "doctor":
        return query.filter(TestRequest.doctor_id == current_user.id)
    if role == "lab_technician":
        return query.filter(or_(
            TestRequestItem.assigned_to == current_user.id,
            TestRequestItem.assigned_to.is_(None),
        ))
    return query


def _scoped_sample_query(role, patient):
    query = Sample.query.join(TestRequest, Sample.request_id == TestRequest.id)
    if role == "patient":
        if not patient:
            return query.filter(TestRequest.id == "__none__")
        return query.filter(TestRequest.patient_id == patient.id)
    if role == "doctor":
        return query.filter(TestRequest.doctor_id == current_user.id)
    return query


def _scoped_consultation_query(role, patient):
    if role == "patient":
        if not patient:
            return OnlineConsultation.query.filter(OnlineConsultation.id == "__none__")
        return OnlineConsultation.query.filter(OnlineConsultation.patient_id == patient.id)
    if role == "doctor":
        return OnlineConsultation.query.filter(OnlineConsultation.doctor_id == current_user.id)
    return OnlineConsultation.query


def _scoped_access_request_query(role, patient):
    if role == "patient":
        if not patient:
            return AccessRequest.query.filter(AccessRequest.id == "__none__")
        return AccessRequest.query.filter(AccessRequest.patient_id == patient.id)
    if role == "doctor":
        return AccessRequest.query.filter(AccessRequest.doctor_id == current_user.id)
    return AccessRequest.query


def _scoped_availability_query(role, patient):
    if role == "doctor":
        return DoctorAvailabilitySlot.query.filter(DoctorAvailabilitySlot.doctor_id == current_user.id)
    if role == "patient" and patient:
        doctor_ids = (
            OnlineConsultation.query
            .with_entities(OnlineConsultation.doctor_id)
            .filter(OnlineConsultation.patient_id == patient.id)
            .distinct()
        )
        return DoctorAvailabilitySlot.query.filter(DoctorAvailabilitySlot.doctor_id.in_(doctor_ids))
    if role == "patient":
        return DoctorAvailabilitySlot.query.filter(DoctorAvailabilitySlot.id == "__none__")
    return DoctorAvailabilitySlot.query


def _live_snapshot_payload():
    role = current_user.primary_role
    patient = _patient_for_current_user() if role == "patient" else None

    notifications = Notification.query.filter(Notification.user_id == current_user.id)
    messages = ChatMessage.query.filter(or_(
        ChatMessage.sender_id == current_user.id,
        ChatMessage.recipient_id == current_user.id,
    ))
    requests = _scoped_request_query(role, patient)
    request_items = _scoped_item_query(role, patient)
    samples = _scoped_sample_query(role, patient)
    consultations = _scoped_consultation_query(role, patient)
    access_requests = _scoped_access_request_query(role, patient)
    availability = _scoped_availability_query(role, patient)

    payload = {
        "role": role,
        "notifications_latest": _iso(_max_dt(notifications, Notification.created_at)),
        "notifications_unread": notifications.filter(Notification.read.is_(False)).count(),
        "messages_latest": _iso(_max_dt(messages, ChatMessage.created_at)),
        "messages_read_latest": _iso(_max_dt(messages, ChatMessage.read_at)),
        "messages_unread": messages.filter(
            ChatMessage.recipient_id == current_user.id,
            ChatMessage.read_at.is_(None),
        ).count(),
        "requests_created": _iso(_max_dt(requests, TestRequest.created_at)),
        "requests_updated": _iso(_max_dt(requests, TestRequest.updated_at)),
        "requests_released": _iso(_max_dt(requests, TestRequest.released_at)),
        "request_statuses": _group_counts(requests, TestRequest.status),
        "items_created": _iso(_max_dt(request_items, TestRequestItem.created_at)),
        "items_started": _iso(_max_dt(request_items, TestRequestItem.started_at)),
        "items_completed": _iso(_max_dt(request_items, TestRequestItem.completed_at)),
        "items_captured": _iso(_max_dt(request_items, TestRequestItem.captured_at)),
        "items_verified": _iso(_max_dt(request_items, TestRequestItem.verified_at)),
        "item_statuses": _group_counts(request_items, TestRequestItem.status),
        "samples_received": _iso(_max_dt(samples, Sample.received_at)),
        "samples_rejected": _iso(_max_dt(samples, Sample.rejected_at)),
        "sample_statuses": _group_counts(samples, Sample.status),
        "consultations_created": _iso(_max_dt(consultations, OnlineConsultation.created_at)),
        "consultations_updated": _iso(_max_dt(consultations, OnlineConsultation.updated_at)),
        "consultation_statuses": _group_counts(consultations, OnlineConsultation.status),
        "access_created": _iso(_max_dt(access_requests, AccessRequest.created_at)),
        "access_responded": _iso(_max_dt(access_requests, AccessRequest.responded_at)),
        "access_statuses": _group_counts(access_requests, AccessRequest.status),
        "availability_created": _iso(_max_dt(availability, DoctorAvailabilitySlot.created_at)),
        "availability_updated": _iso(_max_dt(availability, DoctorAvailabilitySlot.updated_at)),
        "availability_statuses": _group_counts(availability, DoctorAvailabilitySlot.status),
    }

    if role in ("lab_manager", "admin"):
        payload.update({
            "catalog_latest": _iso(_max_dt(TestCatalog.query, TestCatalog.created_at)),
            "consumables_latest": _iso(_max_dt(Consumable.query, Consumable.updated_at)),
            "orders_latest": _iso(_max_dt(ConsumableOrder.query, ConsumableOrder.updated_at)),
            "stock_latest": _iso(_max_dt(StockMovement.query, StockMovement.created_at)),
            "order_statuses": _group_counts(ConsumableOrder.query, ConsumableOrder.status),
        })
    if role == "admin":
        payload.update({
            "users_latest": _iso(_max_dt(User.query, User.updated_at)),
            "patients_latest": _iso(_max_dt(Patient.query, Patient.updated_at)),
        })

    return payload


def _live_snapshot_cache_entry():
    now = datetime.now()
    ttl = max(0, int(current_app.config.get("LIVE_SNAPSHOT_CACHE_SECONDS", 3)))
    cache = current_app.config.setdefault("_LIVE_SNAPSHOT_CACHE", {})
    cache_key = f"{current_user.id}:{current_user.primary_role}"
    cached = cache.get(cache_key)
    if cached and ttl and (now - cached["created_at"]).total_seconds() < ttl:
        return cached

    payload = _live_snapshot_payload()
    version = hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    entry = {
        "created_at": now,
        "version": version,
        "body": {
            "version": version,
            "generated_at": now.isoformat(),
            "notifications_unread": payload.get("notifications_unread", 0),
            "messages_unread": payload.get("messages_unread", 0),
        },
    }
    cache[cache_key] = entry
    if len(cache) > LIVE_SNAPSHOT_CACHE_LIMIT:
        oldest_key = min(cache, key=lambda key: cache[key]["created_at"])
        cache.pop(oldest_key, None)
    return entry


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


@bp.route("/live/snapshot")
@login_required
def live_snapshot():
    entry = _live_snapshot_cache_entry()
    version = entry["version"]
    if request.if_none_match.contains(version):
        response = current_app.response_class(status=304)
    else:
        response = jsonify(entry["body"])
    response.set_etag(version)
    response.cache_control.private = True
    response.cache_control.max_age = max(0, int(current_app.config.get("LIVE_SNAPSHOT_CACHE_SECONDS", 3)))
    response.vary.add("Cookie")
    return response


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


@bp.route("/notifications/clear", methods=["POST"])
@login_required
def clear_notifications():
    count = Notification.query.filter_by(user_id=current_user.id).delete(
        synchronize_session=False,
    )
    db.session.commit()
    return jsonify({"ok": True, "cleared": count})


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


def _webrtc_ice_server_payload():
    ice_servers = []
    stun_urls = [
        url for url in current_app.config.get("WEBRTC_STUN_URLS", [])
        if url
    ]
    turn_urls = [
        url for url in current_app.config.get("WEBRTC_TURN_URLS", [])
        if url
    ]
    turn_username = current_app.config.get("WEBRTC_TURN_USERNAME") or ""
    turn_credential = current_app.config.get("WEBRTC_TURN_CREDENTIAL") or ""
    turn_configured = bool(turn_urls and turn_username and turn_credential)

    if stun_urls:
        ice_servers.append({"urls": stun_urls})
    if turn_configured:
        ice_servers.append({
            "urls": turn_urls,
            "username": turn_username,
            "credential": turn_credential,
        })

    payload = {
        "iceServers": ice_servers,
        "turnConfigured": turn_configured,
    }
    if current_app.config.get("WEBRTC_FORCE_RELAY"):
        payload["iceTransportPolicy"] = "relay"
    return payload


@bp.route("/consultations/<consultation_id>/<room_token>/ice-servers")
@login_required
def consultation_ice_servers(consultation_id, room_token):
    """Return browser ICE configuration for an invite-only consultation."""
    _consultation_participant_or_404(consultation_id, room_token)
    payload = _webrtc_ice_server_payload()
    return jsonify(payload)


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
        "waiting_participants": _consultation_waiting_participants(consultation) if is_doctor else [],
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

    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        signal_type = (data.get("type") or "").strip()
        if signal_type not in {"waiting", "ready", "offer", "answer", "ice", "leave"}:
            return jsonify({"error": "invalid_signal_type"}), 400
        if signal_type == "waiting":
            if current_user.id != consultation.patient_user_id:
                return jsonify({"error": "forbidden"}), 403
            if consultation.status not in ("accepted", "started"):
                return jsonify({"error": "waiting_room_closed"}), 409
            ConsultationSignal.query.filter_by(
                consultation_id=consultation.id,
                sender_id=current_user.id,
                signal_type="waiting",
            ).delete()
            db.session.add(ConsultationSignal(
                consultation_id=consultation.id,
                sender_id=current_user.id,
                signal_type=signal_type,
                payload=json.dumps(data.get("payload") or {}),
            ))
            db.session.commit()
            return jsonify({"ok": True})
        if consultation.status != "started":
            return jsonify({"error": "session_not_started"}), 409
        db.session.add(ConsultationSignal(
            consultation_id=consultation.id,
            sender_id=current_user.id,
            signal_type=signal_type,
            payload=json.dumps(data.get("payload") or {}),
        ))
        db.session.commit()
        return jsonify({"ok": True})

    if consultation.status != "started":
        return jsonify({"error": "session_not_started"}), 409

    rows = (ConsultationSignal.query
            .filter(
                ConsultationSignal.consultation_id == consultation.id,
                ConsultationSignal.sender_id != current_user.id,
                ConsultationSignal.signal_type != "waiting",
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


@bp.route("/consultations/<consultation_id>/<room_token>/recording", methods=["POST"])
@login_required
def consultation_recording(consultation_id, room_token):
    consultation = _consultation_participant_or_404(consultation_id, room_token)
    if current_user.id != consultation.doctor_id:
        return jsonify({"error": "forbidden"}), 403
    if consultation.status not in ("started", "completed"):
        return jsonify({"error": "session_not_active"}), 409

    try:
        if request.form.get("complete") == "1":
            finalize_chunked_recording(
                consultation,
                request.form.get("recording_id"),
                request.form.get("mime") or "video/webm",
                request.form.get("extension") or ".webm",
            )
            db.session.commit()
            return jsonify({
                "ok": True,
                "filename": consultation.session_record_filename,
                "mime": consultation.session_record_mime,
                "size": consultation.session_record_size,
            })

        chunk = request.files.get("chunk")
        if chunk:
            size = append_recording_chunk(
                consultation,
                request.form.get("recording_id"),
                chunk,
            )
            db.session.commit()
            return jsonify({"ok": True, "size": size})

        uploaded = request.files.get("recording")
        if not uploaded:
            return jsonify({"error": "missing_recording"}), 400
        store_recording_file(consultation, uploaded)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    db.session.commit()
    return jsonify({
        "ok": True,
        "filename": consultation.session_record_filename,
        "mime": consultation.session_record_mime,
        "size": consultation.session_record_size,
    })
