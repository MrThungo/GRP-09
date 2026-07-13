"""Role-based access decorators."""
from functools import wraps
from flask import abort, redirect, url_for
from flask_login import current_user


def password_policy_error(password):
    """Return an error message when a password misses the project policy."""
    if len(password or "") < 8:
        return "Password must be at least 8 characters."
    if not any(ch.isupper() for ch in password):
        return "Password must include at least one uppercase letter."
    if not any(ch.isdigit() for ch in password):
        return "Password must include at least one number."
    if not any(not ch.isalnum() for ch in password):
        return "Password must include at least one special character."
    return None


def role_required(*roles):
    allowed = set(roles)

    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if not current_user.is_authenticated:
                abort(401)
            if getattr(current_user, "is_blocked", False):
                from flask_login import logout_user
                logout_user()
                return redirect(url_for("auth.login"))
            if current_user.is_pending:
                return redirect(url_for("auth.pending"))
            if not any(current_user.has_role(r) for r in allowed):
                abort(403)
            return view(*args, **kwargs)
        return wrapper
    return decorator
