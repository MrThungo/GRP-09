"""Helpers for storing and serving online consultation recordings."""
from pathlib import Path
from shutil import copyfileobj
from uuid import uuid4

from flask import current_app
from werkzeug.utils import secure_filename


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

    consultation.session_record_filename = filename
    consultation.session_record_mime = uploaded_file.mimetype or "video/webm"
    consultation.session_record_size = size
    consultation.session_record_body = f"{RECORDING_BODY_PREFIX}{filename}"
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

    consultation.session_record_filename = final_path.name
    consultation.session_record_mime = mime_type or "video/webm"
    consultation.session_record_size = final_path.stat().st_size
    consultation.session_record_body = f"{RECORDING_BODY_PREFIX}{final_path.name}"
    return final_path
