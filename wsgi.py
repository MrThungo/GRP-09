import os

from app import create_app

application = create_app()
app = application

if __name__ == "__main__":
    port = int(os.environ.get("HTTP_PLATFORM_PORT") or os.environ.get("PORT") or 5000)
    host = os.environ.get("HOST", "127.0.0.1")
    debug = os.environ.get("FLASK_DEBUG", "").strip().lower() in {"1", "true", "yes", "on"}
    application.run(host=host, port=port, debug=debug)
