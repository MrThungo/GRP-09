import os

from app import create_app

app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("HTTP_PLATFORM_PORT") or os.environ.get("PORT") or 5001)
    host = os.environ.get("HOST", "127.0.0.3")
    debug = os.environ.get("FLASK_DEBUG", "").strip().lower() in {"1", "true", "yes", "on"}
    app.run(host=host, port=port, debug=debug)
