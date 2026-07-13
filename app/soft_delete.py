"""Helpers for non-destructive deletes."""
from datetime import datetime


def soft_delete(record, actor_id=None):
    now = datetime.now()
    if hasattr(record, "deleted_at"):
        record.deleted_at = now
    if hasattr(record, "deleted_by"):
        record.deleted_by = actor_id
    if hasattr(record, "active"):
        record.active = False
    if record.__class__.__name__ == "User":
        record.is_deactivated = True
        record.deactivated_at = now
    return record


def restore(record):
    if hasattr(record, "deleted_at"):
        record.deleted_at = None
    if hasattr(record, "deleted_by"):
        record.deleted_by = None
    if hasattr(record, "active"):
        record.active = True
    if record.__class__.__name__ == "User":
        record.is_deactivated = False
        record.deactivated_at = None
    return record
