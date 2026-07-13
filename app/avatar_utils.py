"""Avatar rendering helpers - uploaded picture, or initials, or default SVG."""
from markupsafe import Markup, escape

_PALETTE = [
    "#0ea5e9", "#6366f1", "#22c55e", "#f59e0b",
    "#ec4899", "#14b8a6", "#a855f7", "#ef4444",
]


def initials_for(user):
    name = (getattr(user, "full_name", "") or getattr(user, "email", "") or "?").strip()
    parts = [p for p in name.replace(".", " ").split() if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


def _color_for(user):
    seed = getattr(user, "id", "") or getattr(user, "email", "") or ""
    return _PALETTE[sum(map(ord, seed)) % len(_PALETTE)]


def render_avatar(user, size=36, classes=""):
    """Return safe HTML - uploaded image or initials bubble."""
    s = int(size)
    style_box = f"width:{s}px;height:{s}px"
    avatar_url = getattr(user, "avatar_url", None)
    if avatar_url:
        return Markup(
            f'<img src="{escape(avatar_url)}" alt="" '
            f'class="rounded-full object-cover {escape(classes)}" '
            f'style="{style_box}">'
        )
    color = _color_for(user)
    text = escape(initials_for(user))
    font_size = max(10, int(s * 0.42))
    return Markup(
        f'<span class="rounded-full inline-flex items-center justify-center '
        f'font-semibold text-white {escape(classes)}" '
        f'style="{style_box};background:{color};font-size:{font_size}px">{text}</span>'
    )
