"""URL helpers that behave correctly behind subfolder reverse proxies."""
from flask import current_app, has_app_context, has_request_context, request, url_for


def _is_external(value):
    return (
        "://" in value
        or value.startswith("//")
        or value.startswith(("mailto:", "tel:", "#"))
    )


def _script_root():
    if not has_request_context():
        return ""
    return (request.script_root or "").rstrip("/")


def _strip_script_root(path):
    value = str(path or "/")
    root = _script_root()
    if root and (value == root or value.startswith(f"{root}/")):
        value = value[len(root):] or "/"
    return value


def app_base_url():
    if has_app_context():
        configured = (current_app.config.get("APP_BASE_URL") or "").rstrip("/")
        if configured:
            return configured
    if has_request_context():
        return request.host_url.rstrip("/")
    return ""


def app_url(path="/", external=False):
    value = str(path or "")
    if not value:
        return "#" if not external else app_base_url()
    if _is_external(value):
        return value

    if external:
        base = app_base_url()
        if not base:
            return value
        value = _strip_script_root(value)
        return f"{base}/{value.lstrip('/')}"

    root = _script_root()
    if root and value.startswith(f"{root}/"):
        return value
    if value.startswith("/"):
        return f"{root}{value}"
    return f"{root}/{value}" if root else value


def external_url_for(endpoint, **values):
    return app_url(url_for(endpoint, **values), external=True)
