"""Small helpers for online/offline presence labels."""
from datetime import datetime


def last_seen_age_label(last_seen, now=None):
    if not last_seen:
        return "never seen"

    now = now or datetime.now()
    minutes = max(1, int((now - last_seen).total_seconds() // 60))

    if minutes < 60:
        value = minutes
        unit = "minute"
    elif minutes < 1440:
        value = max(1, minutes // 60)
        unit = "hour"
    else:
        value = max(1, minutes // 1440)
        unit = "day"

    suffix = "" if value == 1 else "s"
    return f"{value} {unit}{suffix} ago"


def presence_label(last_seen, online=False, now=None):
    if online:
        return "Online"
    if last_seen:
        return f"Last seen {last_seen_age_label(last_seen, now)}"
    return "Offline"
