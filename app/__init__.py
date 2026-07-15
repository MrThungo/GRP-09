"""Application factory."""
import os
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from flask import Flask, redirect, url_for, request, jsonify, flash
from flask_login import LoginManager, current_user, logout_user
from flask_migrate import Migrate
from flask_mail import Mail
from itsdangerous import URLSafeTimedSerializer
from sqlalchemy import inspect, text
from sqlalchemy.orm import selectinload
from werkzeug.middleware.proxy_fix import ProxyFix

from .extensions import db
from .models import User
from .DefaultUsers import create_default_users

login_manager = LoginManager()
login_manager.login_view = "auth.login"
migrate = Migrate()
mail = Mail()


def _load_environment_files():
    """Load local environment files, including the project's env.txt."""
    def fallback_values(path):
        values = {}
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip('"').strip("'")
        except OSError:
            return values
        return values

    try:
        from dotenv import dotenv_values
    except ImportError:
        dotenv_values = fallback_values

    project_root = Path(__file__).resolve().parent.parent
    placeholder_values = {
        "replace-with-a-long-random-value",
        "smtp.example.com",
        "no-reply@example.com",
    }
    for path in (project_root / ".env", project_root / "env.txt"):
        if not path.exists():
            continue
        for key, value in dotenv_values(path).items():
            if not key or value is None or key in os.environ:
                continue
            clean_value = value.strip()
            if clean_value in placeholder_values:
                continue
            if key == "DATABASE_URL" and "USERNAME:PASSWORD@SERVER" in clean_value:
                continue
            os.environ[key] = clean_value


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(
        User,
        user_id,
        options=(selectinload(User.user_roles), selectinload(User.patient_record)),
    )


def _ensure_columns(app):
    """Lightweight ALTER TABLE for SQLite when adding new columns to existing DBs."""
    with app.app_context():
        insp = inspect(db.engine)

        def ensure(table, definitions):
            if table not in insp.get_table_names():
                return
            cols = {c["name"] for c in insp.get_columns(table)}
            for name, ddl in definitions:
                if name not in cols:
                    db.session.execute(text(f"ALTER TABLE {table} ADD COLUMN {ddl}"))
                    db.session.commit()

        if "users" in insp.get_table_names():
            cols = {c["name"] for c in insp.get_columns("users")}
            for ddl in [
                ("is_blocked",     "ALTER TABLE users ADD COLUMN is_blocked BOOLEAN NOT NULL DEFAULT 0"),
                ("last_seen",      "ALTER TABLE users ADD COLUMN last_seen DATETIME"),
                ("is_deactivated", "ALTER TABLE users ADD COLUMN is_deactivated BOOLEAN NOT NULL DEFAULT 0"),
                ("deactivated_at", "ALTER TABLE users ADD COLUMN deactivated_at DATETIME"),
                ("deleted_at",     "ALTER TABLE users ADD COLUMN deleted_at DATETIME"),
                ("deleted_by",     "ALTER TABLE users ADD COLUMN deleted_by VARCHAR(36)"),
                ("temp_password",  "ALTER TABLE users ADD COLUMN temp_password VARCHAR(64)"),
                ("title",          "ALTER TABLE users ADD COLUMN title VARCHAR(32)"),
                ("surname",        "ALTER TABLE users ADD COLUMN surname VARCHAR(120)"),
                ("gender",         "ALTER TABLE users ADD COLUMN gender VARCHAR(32)"),
                ("date_of_birth",  "ALTER TABLE users ADD COLUMN date_of_birth DATE"),
                ("employee_number","ALTER TABLE users ADD COLUMN employee_number VARCHAR(64)"),
                ("sa_id_number",   "ALTER TABLE users ADD COLUMN sa_id_number VARCHAR(32)"),
                ("hpcsa_number",   "ALTER TABLE users ADD COLUMN hpcsa_number VARCHAR(64)"),
            ]:
                if ddl[0] not in cols:
                    db.session.execute(text(ddl[1]))
                    db.session.commit()
        if "patients" in insp.get_table_names():
            pcols = {c["name"] for c in insp.get_columns("patients")}
            for ddl in [
                ("chronic_conditions", "ALTER TABLE patients ADD COLUMN chronic_conditions TEXT"),
                ("allergies",          "ALTER TABLE patients ADD COLUMN allergies TEXT"),
                ("current_medication", "ALTER TABLE patients ADD COLUMN current_medication TEXT"),
                ("surname",            "ALTER TABLE patients ADD COLUMN surname VARCHAR(120)"),
                ("id_number",          "ALTER TABLE patients ADD COLUMN id_number VARCHAR(32)"),
                ("deleted_at",         "ALTER TABLE patients ADD COLUMN deleted_at DATETIME"),
                ("deleted_by",         "ALTER TABLE patients ADD COLUMN deleted_by VARCHAR(36)"),
            ]:
                if ddl[0] not in pcols:
                    db.session.execute(text(ddl[1]))
                    db.session.commit()
        for table in (
            "suppliers", "consumables",
            "conditions", "allergies", "medications",
        ):
            ensure(table, [
                ("deleted_at", "deleted_at DATETIME"),
                ("deleted_by", "deleted_by VARCHAR(36)"),
            ])
        ensure("test_catalog", [
            ("reference_text", "reference_text TEXT"),
            ("sample_type", "sample_type VARCHAR(100)"),
            ("consumables_used", "consumables_used TEXT"),
            ("assigned_technician", "assigned_technician VARCHAR(100)"),
            ("deleted_at", "deleted_at DATETIME"),
            ("deleted_by", "deleted_by VARCHAR(36)"),
        ])
        ensure("test_requests", [
            ("release_note", "release_note TEXT"),
            ("cancel_reason", "cancel_reason TEXT"),
            ("cancelled_by", "cancelled_by VARCHAR(36)"),
            ("cancelled_at", "cancelled_at DATETIME"),
        ])
        ensure("online_consultations", [
            ("session_record_filename", "session_record_filename VARCHAR(255)"),
            ("session_record_mime", "session_record_mime VARCHAR(80)"),
            ("session_record_size", "session_record_size INTEGER"),
            ("session_record_body", "session_record_body TEXT"),
        ])
        ensure("test_request_items", [
            ("assigned_to", "assigned_to VARCHAR(36)"),
            ("started_at", "started_at DATETIME"),
            ("completed_at", "completed_at DATETIME"),
            ("verification_notes", "verification_notes TEXT"),
            ("review_notes", "review_notes TEXT"),
            ("near_limit_reminded_at", "near_limit_reminded_at DATETIME"),
        ])
        ensure("samples", [
            ("received_by", "received_by VARCHAR(36)"),
            ("received_at", "received_at DATETIME"),
            ("rejected_by", "rejected_by VARCHAR(36)"),
            ("rejected_at", "rejected_at DATETIME"),
            ("rejection_reason", "rejection_reason TEXT"),
        ])
        ensure("consumable_orders", [
            ("completed_at", "completed_at DATETIME"),
            ("cancelled_at", "cancelled_at DATETIME"),
            ("cancel_reason", "cancel_reason TEXT"),
            ("supplier_notified_at", "supplier_notified_at DATETIME"),
        ])
        if "test_request_items" in insp.get_table_names():
            db.session.execute(text("UPDATE test_request_items SET status = 'submitted' WHERE status = 'pending'"))
            db.session.commit()
        if "consumable_orders" in insp.get_table_names():
            db.session.execute(text("UPDATE consumable_orders SET status = 'complete' WHERE status = 'received'"))
            db.session.execute(text("UPDATE consumable_orders SET status = 'partially_complete' WHERE status = 'partial'"))
            db.session.commit()


def _ensure_indexes(app):
    """Create SQLite indexes added after the original schema was created."""
    with app.app_context():
        insp = inspect(db.engine)
        tables = set(insp.get_table_names())

        def ensure(table, name, columns):
            if table not in tables:
                return
            db.session.execute(text(
                f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({columns})"
            ))

        for table, name, columns in [
            ("users", "idx_users_last_seen", "last_seen"),
            ("users", "idx_users_active_presence", "deleted_at, is_blocked, is_deactivated, last_seen"),
            ("user_roles", "idx_user_roles_role_user", "role, user_id"),
            ("patients", "idx_patients_deleted_name", "deleted_at, full_name"),
            ("patients", "idx_patients_profile_deleted", "profile_id, deleted_at"),
            ("test_catalog", "idx_test_catalog_active_deleted", "active, deleted_at"),
            ("test_requests", "idx_requests_doctor_status_created", "doctor_id, status, created_at"),
            ("test_requests", "idx_requests_patient_status_created", "patient_id, status, created_at"),
            ("test_requests", "idx_requests_created", "created_at"),
            ("test_requests", "idx_requests_released", "released_at"),
            ("test_request_items", "idx_request_items_assigned_status_reminder", "assigned_to, status, near_limit_reminded_at"),
            ("test_request_items", "idx_request_items_request_status", "request_id, status"),
            ("test_request_items", "idx_request_items_status_test", "status, test_id"),
            ("test_request_items", "idx_request_items_captured", "captured_by, captured_at"),
            ("test_request_items", "idx_request_items_verified", "verified_by, verified_at"),
            ("samples", "idx_samples_request_status", "request_id, status"),
            ("notifications", "idx_notifications_user_read_created", "user_id, read, created_at"),
            ("notifications", "idx_notifications_user_created", "user_id, created_at"),
            ("chat_messages", "idx_chat_pair_created", "sender_id, recipient_id, created_at"),
            ("chat_messages", "idx_chat_unread_thread", "recipient_id, sender_id, read_at"),
            ("consent_grants", "idx_consent_doctor_patient_active", "doctor_id, patient_id, revoked_at, granted_at"),
            ("consent_grants", "idx_consent_patient_active", "patient_id, revoked_at, granted_at"),
            ("consent_grant_items", "idx_consent_grant_items_request", "request_id, grant_id"),
            ("consent_grant_request_items", "idx_consent_request_items_item", "item_id, grant_id"),
            ("access_requests", "idx_access_patient_status_created", "patient_id, status, created_at"),
            ("access_requests", "idx_access_doctor_status_created", "doctor_id, status, created_at"),
            ("technician_tests", "idx_technician_tests_technician_test", "technician_id, test_id"),
            ("technician_tests", "idx_technician_tests_test", "test_id"),
            ("online_consultations", "idx_online_consults_doctor_status", "doctor_id, status, scheduled_at"),
            ("online_consultations", "idx_online_consults_patient_status", "patient_id, status, scheduled_at"),
            ("online_consultations", "idx_online_consults_request_created", "request_id, created_at"),
            ("consultation_signals", "idx_consult_signals_room_created", "consultation_id, created_at"),
            ("consumables", "idx_consumables_stock", "deleted_at, current_stock, reorder_level"),
        ]:
            ensure(table, name, columns)
        db.session.commit()


def _env_bool(name, default=False):
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _static_asset_version(app):
    """Cache-bust CSS/JS when files change while allowing browser caching."""
    candidates = [
        os.path.join(app.static_folder, "css", "app.css"),
        os.path.join(app.static_folder, "js", "app.js"),
        os.path.join(app.static_folder, "js", "consultation.js"),
    ]
    try:
        return str(max(int(os.path.getmtime(path)) for path in candidates if os.path.exists(path)))
    except ValueError:
        return "1"


def create_app():
    _load_environment_files()

    app = Flask(__name__, static_folder="static", template_folder="templates")
    secret_key = os.environ.get("SECRET_KEY")
    if not secret_key:
        secret_key = secrets.token_hex(32)
        app.logger.warning(
            "SECRET_KEY is not set; using an ephemeral development key. "
            "Set SECRET_KEY in env.txt or the server environment before publishing."
        )
    app.config["SECRET_KEY"] = secret_key
    app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get("DATABASE_URL", "sqlite:///nmb.db")
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["PRESENCE_WRITE_INTERVAL_SECONDS"] = int(
        os.environ.get("PRESENCE_WRITE_INTERVAL_SECONDS", "60")
    )
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = timedelta(days=7)
    app.config["STATIC_ASSET_VERSION"] = _static_asset_version(app)

    # Sessions
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = _env_bool("SESSION_COOKIE_SECURE", False)
    app.config["PREFERRED_URL_SCHEME"] = os.environ.get(
        "PREFERRED_URL_SCHEME",
        "https" if app.config["SESSION_COOKIE_SECURE"] else "http",
    )

    # SMTP / Flask-Mail (set via env: MAIL_SERVER, MAIL_PORT, MAIL_USERNAME, MAIL_PASSWORD,
    # MAIL_USE_TLS, MAIL_USE_SSL, MAIL_DEFAULT_SENDER, APP_BASE_URL)
    app.config["MAIL_SERVER"]   = os.environ.get("MAIL_SERVER", "localhost")
    app.config["MAIL_PORT"]     = int(os.environ.get("MAIL_PORT", "465"))
    app.config["MAIL_USERNAME"] = os.environ.get("MAIL_USERNAME") or None
    app.config["MAIL_PASSWORD"] = os.environ.get("MAIL_PASSWORD") or None
    app.config["MAIL_USE_TLS"]  = _env_bool("MAIL_USE_TLS", False)
    app.config["MAIL_USE_SSL"]  = _env_bool("MAIL_USE_SSL", True)
    app.config["MAIL_DEFAULT_SENDER"] = os.environ.get(
        "MAIL_DEFAULT_SENDER", "no-reply@nmbhlab.local"
    )
    app.config["APP_BASE_URL"] = os.environ.get("APP_BASE_URL", "")
    is_sqlite = app.config["SQLALCHEMY_DATABASE_URI"].lower().startswith("sqlite")
    app.config["ENABLE_QUICK_LOGIN"] = _env_bool("ENABLE_QUICK_LOGIN", is_sqlite)
    # Password-reset token lifetime: 5 minutes
    app.config["PASSWORD_RESET_MAX_AGE_SECONDS"] = 300

    # GreenAPI WhatsApp (set via env: GREENAPI_ID_INSTANCE,
    # GREENAPI_API_TOKEN_INSTANCE, GREENAPI_API_URL)
    greenapi_timeout = (
        os.environ.get("GREENAPI_TIMEOUT_SECONDS")
        or os.environ.get("GREEN_API_TIMEOUT_SECONDS")
        or "10"
    )
    try:
        greenapi_timeout = float(greenapi_timeout)
    except ValueError:
        greenapi_timeout = 10
    app.config["GREENAPI_ENABLED"] = _env_bool(
        "GREENAPI_ENABLED",
        _env_bool("GREEN_API_ENABLED", True),
    )
    app.config["GREENAPI_API_URL"] = (
        os.environ.get("GREENAPI_API_URL")
        or os.environ.get("GREEN_API_API_URL")
        or "https://api.green-api.com"
    ).rstrip("/")
    app.config["GREENAPI_ID_INSTANCE"] = (
        os.environ.get("GREENAPI_ID_INSTANCE")
        or os.environ.get("GREEN_API_ID_INSTANCE")
        or ""
    )
    app.config["GREENAPI_API_TOKEN_INSTANCE"] = (
        os.environ.get("GREENAPI_API_TOKEN_INSTANCE")
        or os.environ.get("GREEN_API_API_TOKEN_INSTANCE")
        or ""
    )
    app.config["GREENAPI_DEFAULT_COUNTRY_CODE"] = (
        os.environ.get("GREENAPI_DEFAULT_COUNTRY_CODE")
        or os.environ.get("GREEN_API_DEFAULT_COUNTRY_CODE")
        or "27"
    )
    app.config["GREENAPI_TIMEOUT_SECONDS"] = greenapi_timeout
    app.config["GREENAPI_INCLUDE_TEMP_PASSWORD"] = _env_bool(
        "GREENAPI_INCLUDE_TEMP_PASSWORD",
        _env_bool("GREEN_API_INCLUDE_TEMP_PASSWORD", False),
    )

    # Twilio Conversations patient chatbot. This is for browser-based chat,
    # not SMS or WhatsApp.
    app.config["TWILIO_ACCOUNT_SID"] = os.environ.get("TWILIO_ACCOUNT_SID", "")
    app.config["TWILIO_AUTH_TOKEN"] = os.environ.get("TWILIO_AUTH_TOKEN", "")
    app.config["TWILIO_API_KEY_SID"] = os.environ.get("TWILIO_API_KEY_SID", "")
    app.config["TWILIO_API_KEY_SECRET"] = os.environ.get("TWILIO_API_KEY_SECRET", "")
    app.config["TWILIO_CONVERSATIONS_SERVICE_SID"] = os.environ.get(
        "TWILIO_CONVERSATIONS_SERVICE_SID", ""
    )
    app.config["TWILIO_BOT_IDENTITY"] = os.environ.get(
        "TWILIO_BOT_IDENTITY", "nmb-hlab-bot"
    )
    app.config["TWILIO_CONVERSATIONS_API_BASE"] = os.environ.get(
        "TWILIO_CONVERSATIONS_API_BASE", "https://conversations.twilio.com/v1"
    )
    app.config["TWILIO_CONVERSATIONS_SDK_URL"] = os.environ.get(
        "TWILIO_CONVERSATIONS_SDK_URL",
        "https://sdk.twilio.com/js/conversations/v2.6/twilio-conversations.min.js",
    )
    app.config["TWILIO_WEBHOOK_PUBLIC_URL"] = os.environ.get("TWILIO_WEBHOOK_PUBLIC_URL", "")
    try:
        twilio_chatbot_timeout = float(os.environ.get("TWILIO_CHATBOT_TIMEOUT_SECONDS", "10"))
    except ValueError:
        twilio_chatbot_timeout = 10
    app.config["TWILIO_CHATBOT_TIMEOUT_SECONDS"] = twilio_chatbot_timeout
    app.config["TWILIO_VALIDATE_REQUESTS"] = _env_bool(
        "TWILIO_VALIDATE_REQUESTS",
        bool(app.config["TWILIO_AUTH_TOKEN"]),
    )

    # Avatar uploads
    app.config["AVATAR_UPLOAD_DIR"] = os.path.join(app.static_folder, "avatars")
    os.makedirs(app.config["AVATAR_UPLOAD_DIR"], exist_ok=True)
    os.makedirs(app.instance_path, exist_ok=True)

    db.init_app(app)
    login_manager.init_app(app)
    migrate.init_app(app, db)
    mail.init_app(app)

    if _env_bool("TRUST_PROXY_HEADERS", False):
        app.wsgi_app = ProxyFix(
            app.wsgi_app,
            x_for=int(os.environ.get("PROXY_FIX_X_FOR", "1")),
            x_proto=int(os.environ.get("PROXY_FIX_X_PROTO", "1")),
            x_host=int(os.environ.get("PROXY_FIX_X_HOST", "1")),
            x_prefix=int(os.environ.get("PROXY_FIX_X_PREFIX", "1")),
        )

    @app.after_request
    def _performance_headers(response):
        if request.endpoint == "static":
            response.cache_control.public = True
            response.cache_control.max_age = 604800
        return response

    # Token serializer for password-reset links
    app.password_reset_serializer = URLSafeTimedSerializer(
        app.config["SECRET_KEY"], salt="password-reset"
    )

    from .blueprints.public import bp as public_bp
    from .blueprints.auth import bp as auth_bp
    from .blueprints.doctor import bp as doctor_bp
    from .blueprints.patient import bp as patient_bp
    from .blueprints.technician import bp as technician_bp
    from .blueprints.manager import bp as manager_bp
    from .blueprints.admin import bp as admin_bp
    from .blueprints.api import bp as api_bp
    from .blueprints.messages import bp as messages_bp
    from .blueprints.chatbot import bp as chatbot_bp

    app.register_blueprint(public_bp)
    app.register_blueprint(auth_bp, url_prefix="/auth")
    app.register_blueprint(doctor_bp, url_prefix="/doctor")
    app.register_blueprint(patient_bp, url_prefix="/patient")
    app.register_blueprint(technician_bp, url_prefix="/technician")
    app.register_blueprint(manager_bp, url_prefix="/manager")
    app.register_blueprint(admin_bp, url_prefix="/admin")
    app.register_blueprint(api_bp, url_prefix="/api")
    app.register_blueprint(messages_bp, url_prefix="/messages")
    app.register_blueprint(chatbot_bp, url_prefix="/chatbot")

    from .avatar_utils import render_avatar, initials_for
    app.jinja_env.globals["render_avatar"] = render_avatar
    app.jinja_env.globals["initials_for"] = initials_for

    @app.before_request
    def _global_guard():
        if current_user.is_authenticated and (
            getattr(current_user, "deleted_at", None)
            or getattr(current_user, "is_blocked", False)
            or getattr(current_user, "is_deactivated", False)
        ):
            if getattr(current_user, "deleted_at", None):
                reason = "deleted"
                msg = "Your account has been deleted. Please contact an administrator."
            elif getattr(current_user, "is_blocked", False):
                reason = "blocked"
                msg = "Your account has been blocked. Please contact an administrator."
            else:
                reason = "deactivated"
                msg = "Your account is deactivated. An administrator must reactivate it before you can sign in."
            logout_user()
            if request.path.startswith("/api/"):
                return jsonify({"error": reason, "message": msg}), 401
            if request.endpoint not in ("auth.login", "public.landing", "static"):
                flash(msg, "error")
                return redirect(url_for("auth.login"))
        if current_user.is_authenticated:
            now = datetime.now()
            last_seen = getattr(current_user, "last_seen", None)
            interval = app.config["PRESENCE_WRITE_INTERVAL_SECONDS"]
            if not last_seen or (now - last_seen).total_seconds() >= interval:
                try:
                    db.session.execute(
                        text("UPDATE users SET last_seen = :last_seen WHERE id = :user_id"),
                        {"last_seen": now, "user_id": current_user.id},
                    )
                    db.session.commit()
                except Exception:
                    db.session.rollback()
            try:
                last_check = app.config.get("_NEAR_LIMIT_REMINDER_LAST_CHECK")
                if not last_check or (now - last_check).total_seconds() >= 60:
                    from .reminders import send_near_limit_reminders
                    send_near_limit_reminders(now=now)
                    app.config["_NEAR_LIMIT_REMINDER_LAST_CHECK"] = now
            except Exception as exc:
                db.session.rollback()
                app.logger.warning("Near-limit reminder check failed: %s", exc)

    @app.route("/app")
    def app_home():
        if not current_user.is_authenticated:
            return redirect(url_for("auth.login"))
        if current_user.is_pending:
            return redirect(url_for("auth.pending"))
        return redirect(url_for(_home_endpoint_for(current_user)))

    @app.errorhandler(404)
    def not_found(_):
        from flask import render_template
        return render_template("404.html"), 404

    with app.app_context():
        database_uri = app.config["SQLALCHEMY_DATABASE_URI"].lower()
        if database_uri.startswith("sqlite"):
            db.create_all()
            _ensure_columns(app)
            _ensure_indexes(app)
            create_default_users()
        elif _env_bool("SEED_DEFAULT_USERS", False):
            create_default_users()
    return app


def _home_endpoint_for(user):
    return {
        "admin": "admin.dashboard",
        "lab_manager": "manager.dashboard",
        "doctor": "doctor.dashboard",
        "lab_technician": "technician.dashboard",
        "patient": "patient.dashboard",
    }.get(user.primary_role, "auth.login")
