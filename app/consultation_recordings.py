"""Helpers for storing and serving online consultation recordings."""
from datetime import datetime, timedelta
from pathlib import Path
from shutil import copyfileobj
from uuid import uuid4

from flask import current_app
from werkzeug.utils import secure_filename

from .extensions import db
from .models import OnlineConsultation


RECORDING_BODY_PREFIX = "file:"
VIDEO_EXTENSIONS = {".webm", ".mp4", ".ogg", ".ogv", ".mov", ".mkv"}


def recording_storage_dir():
    path = Path(current_app.instance_path) / "consultation_recordings"
    path.mkdir(parents=True, exist_ok=True)
    return path


def recording_body_filename(consultation):
    body = consultation.session_record_body or ""
    if not body.startswith(RECORDING_BODY_PREFIX):
        return None
    filename = secure_filename(body[len(RECORDING_BODY_PREFIX):])
    return filename or None


def recording_path(consultation):
    filename = recording_body_filename(consultation)
    if not filename:
        return None
    base = recording_storage_dir().resolve()
    path = (base / filename).resolve()
    if path.parent != base or not path.is_file():
        return None
    return path


def has_video_recording(consultation):
    mime = consultation.session_record_mime or ""
    return mime.startswith("video/") and recording_path(consultation) is not None


def default_recording_expiry():
    hours = current_app.config.get("CONSULTATION_RECORDING_RETENTION_HOURS")
    if hours is None:
        hours = int(current_app.config.get("CONSULTATION_RECORDING_RETENTION_DAYS", 30) or 30) * 24
    hours = max(24, int(hours or 24))
    return datetime.now() + timedelta(hours=hours)


def _apply_recording_metadata(consultation, filename, mime_type, size):
    consultation.session_record_filename = filename
    consultation.session_record_mime = mime_type or "video/webm"
    consultation.session_record_size = size
    consultation.session_record_body = f"{RECORDING_BODY_PREFIX}{filename}"
    consultation.session_record_expires_at = default_recording_expiry()
    consultation.session_record_expiry_notified_at = None


def store_recording_file(consultation, uploaded_file):
    original = secure_filename(uploaded_file.filename or "")
    extension = safe_video_extension(Path(original).suffix.lower())

    base = recording_storage_dir()
    filename = f"consultation-{consultation.id}-{uuid4().hex}{extension}"
    path = base / filename
    old_path = recording_path(consultation)

    uploaded_file.save(path)
    size = path.stat().st_size
    if size <= 0:
        path.unlink(missing_ok=True)
        raise ValueError("empty_recording")

    if old_path and old_path != path:
        old_path.unlink(missing_ok=True)

    _apply_recording_metadata(
        consultation,
        filename,
        uploaded_file.mimetype or "video/webm",
        size,
    )
    return path


def safe_video_extension(extension):
    extension = (extension or "").strip().lower()
    if not extension.startswith("."):
        extension = f".{extension}" if extension else ""
    return extension if extension in VIDEO_EXTENSIONS else ".webm"


def safe_recording_id(recording_id):
    recording_id = secure_filename(recording_id or "")
    if not recording_id:
        raise ValueError("missing_recording_id")
    return recording_id[:80]


def chunk_path(consultation, recording_id):
    recording_id = safe_recording_id(recording_id)
    base = recording_storage_dir().resolve()
    path = (base / f"consultation-{consultation.id}-{recording_id}.part").resolve()
    if path.parent != base:
        raise ValueError("invalid_recording_id")
    return path


def append_recording_chunk(consultation, recording_id, uploaded_file):
    path = chunk_path(consultation, recording_id)
    with path.open("ab") as destination:
        copyfileobj(uploaded_file.stream, destination)
    size = path.stat().st_size
    if size <= 0:
        raise ValueError("empty_recording")
    return size


def finalize_chunked_recording(consultation, recording_id, mime_type=None, extension=None):
    temp_path = chunk_path(consultation, recording_id)
    if not temp_path.is_file() or temp_path.stat().st_size <= 0:
        raise ValueError("empty_recording")

    extension = safe_video_extension(extension)
    final_path = recording_storage_dir() / f"consultation-{consultation.id}-{uuid4().hex}{extension}"
    old_path = recording_path(consultation)
    temp_path.replace(final_path)
    if old_path and old_path != final_path:
        old_path.unlink(missing_ok=True)

    _apply_recording_metadata(
        consultation,
        final_path.name,
        mime_type or "video/webm",
        final_path.stat().st_size,
    )
    return final_path


def extend_recording_expiry(consultation, days=30):
    days = max(1, min(365, int(days or 30)))
    base = consultation.session_record_expires_at or datetime.now()
    if base < datetime.now():
        base = datetime.now()
    consultation.session_record_expires_at = base + timedelta(days=days)
    consultation.session_record_expiry_notified_at = None
    return consultation.session_record_expires_at


def clear_recording_metadata(consultation):
    consultation.session_record_filename = None
    consultation.session_record_mime = None
    consultation.session_record_size = None
    consultation.session_record_body = None
    consultation.session_record_expires_at = None
    consultation.session_record_expiry_notified_at = None


def delete_recording_file(consultation):
    path = recording_path(consultation)
    if path:
        path.unlink(missing_ok=True)
    clear_recording_metadata(consultation)
    return bool(path)


def send_recording_expiry_warnings(now=None):
    return 0


def delete_expired_recordings(now=None):
    now = now or datetime.now()
    rows = (
        OnlineConsultation.query
        .filter(
            OnlineConsultation.session_record_body.isnot(None),
            OnlineConsultation.session_record_expires_at.isnot(None),
            OnlineConsultation.session_record_expires_at <= now,
        )
        .limit(50)
        .all()
    )
    for consultation in rows:
        delete_recording_file(consultation)
    if rows:
        db.session.commit()
    return len(rows)


def run_recording_retention_tasks(now=None):
    deleted = delete_expired_recordings(now=now)
    return {"deleted": deleted, "warned": 0}
