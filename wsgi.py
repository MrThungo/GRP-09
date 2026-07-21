import os
import sys
import traceback

PROJECT_HOME = os.path.dirname(os.path.abspath(__file__))
LOCAL_SITE_PACKAGES = os.path.join(PROJECT_HOME, ".venv", "Lib", "site-packages")
STARTUP_LOG = os.path.join(PROJECT_HOME, "App_Data", "logs", "startup-error.log")

if os.path.isdir(LOCAL_SITE_PACKAGES) and LOCAL_SITE_PACKAGES not in sys.path:
    sys.path.insert(0, LOCAL_SITE_PACKAGES)

def write_startup_log(message):
    try:
        os.makedirs(os.path.dirname(STARTUP_LOG), exist_ok=True)
        with open(STARTUP_LOG, "a", encoding="utf-8") as handle:
            handle.write(message)
    except Exception:
        pass


try:
    write_startup_log("\n--- Python process started ---\n")

    from app import create_app

    application = create_app()

    @application.get("/__bridge/health")
    def _bridge_health():
        return (
            "ok\n" + os.environ.get("PYTHON_BRIDGE_DEPLOYMENT_ID", "") + "\n",
            200,
            {"Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-store"},
        )
except Exception:
    write_startup_log("\n--- Flask startup failed ---\n" + traceback.format_exc())
    raise

app = application

if __name__ == "__main__":
    port = int(os.environ.get("HTTP_PLATFORM_PORT") or os.environ.get("PORT") or 5000)
    host = os.environ.get("HOST", "127.0.0.1")
    debug = os.environ.get("FLASK_DEBUG", "").strip().lower() in {"1", "true", "yes", "on"}
    application.run(host=host, port=port, debug=debug)
