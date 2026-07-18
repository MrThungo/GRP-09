import os
import sys

# PythonAnywhere project path after cloning from GitHub.
PROJECT_HOME = "/home/PapamaXuza/GRP-09"

if PROJECT_HOME not in sys.path:
    sys.path.insert(0, PROJECT_HOME)

os.chdir(PROJECT_HOME)

# Testing defaults for PythonAnywhere.
os.environ.setdefault("FLASK_ENV", "production")
os.environ.setdefault("SEED_DEFAULT_USERS", "true")
os.environ.setdefault("ENABLE_QUICK_LOGIN", "true")

# Optional: one known password for seeded test accounts.
# Remove this before final production.
os.environ.setdefault("DEFAULT_USER_PASSWORD", "Test@12345")

# PythonAnywhere HTTPS/proxy settings.
os.environ.setdefault("SESSION_COOKIE_SECURE", "true")
os.environ.setdefault("PREFERRED_URL_SCHEME", "https")
os.environ.setdefault("TRUST_PROXY_HEADERS", "true")
os.environ.setdefault("PROXY_FIX_X_FOR", "1")
os.environ.setdefault("PROXY_FIX_X_PROTO", "1")
os.environ.setdefault("PROXY_FIX_X_HOST", "1")
os.environ.setdefault("PROXY_FIX_X_PREFIX", "1")

# WebRTC live consultation settings.
# STUN helps browsers discover their network path, but real PythonAnywhere
# deployments need TURN credentials for reliable doctor-patient video.
os.environ.setdefault("WEBRTC_STUN_URLS", "stun:stun.l.google.com:19302")
os.environ.setdefault("WEBRTC_TURN_URLS", "")
os.environ.setdefault("WEBRTC_TURN_USERNAME", "")
os.environ.setdefault("WEBRTC_TURN_CREDENTIAL", "")
os.environ.setdefault("WEBRTC_FORCE_RELAY", "false")

from app import create_app  # noqa: E402

application = create_app()
app = application
