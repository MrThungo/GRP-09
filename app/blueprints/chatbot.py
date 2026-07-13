"""Patient chatbot endpoints, including Twilio Conversations integration."""
from flask import Blueprint, abort, current_app, jsonify, render_template, request
from flask_login import current_user, login_required

from ..chatbot import (
    ensure_twilio_user_conversation,
    format_reply_for_twilio,
    generate_twilio_chat_token,
    handle_chat_message,
    post_twilio_conversation_reply,
    twilio_bot_identity,
    twilio_conversations_configured,
    user_from_twilio_identity,
)

bp = Blueprint("chatbot", __name__, template_folder="../templates/chatbot")


def _validate_twilio_request():
    if not current_app.config.get("TWILIO_VALIDATE_REQUESTS"):
        return True
    auth_token = current_app.config.get("TWILIO_AUTH_TOKEN")
    signature = request.headers.get("X-Twilio-Signature", "")
    if not auth_token or not signature:
        return False
    try:
        from twilio.request_validator import RequestValidator
    except ImportError:
        current_app.logger.warning("Twilio request validation needs the twilio package.")
        return False
    validation_url = current_app.config.get("TWILIO_WEBHOOK_PUBLIC_URL") or request.url
    return RequestValidator(auth_token).validate(validation_url, request.form, signature)


@bp.route("/patient")
@login_required
def patient_chat():
    if current_user.is_pending:
        abort(403)
    return render_template(
        "chatbot/patient.html",
        twilio_sdk_url=current_app.config["TWILIO_CONVERSATIONS_SDK_URL"],
        twilio_configured=twilio_conversations_configured(),
    )


@bp.post("/api/message")
@login_required
def api_message():
    if current_user.is_pending:
        abort(403)
    payload = request.get_json(silent=True) or {}
    message = (payload.get("message") or "").strip()
    reply = handle_chat_message(current_user, message)
    return jsonify({"reply": reply.body, "links": reply.links})


@bp.get("/api/twilio/session")
@login_required
def twilio_session():
    if current_user.is_pending:
        abort(403)
    if not twilio_conversations_configured():
        return jsonify({
            "configured": False,
            "message": "Twilio Conversations is not configured.",
        })
    try:
        token, identity = generate_twilio_chat_token(current_user)
        conversation_sid = ensure_twilio_user_conversation(current_user)
    except Exception as exc:
        current_app.logger.warning("Twilio chatbot session setup failed: %s", exc)
        return jsonify({"configured": False, "message": str(exc)}), 503
    return jsonify({
        "configured": True,
        "identity": identity,
        "token": token,
        "conversation_sid": conversation_sid,
        "bot_identity": twilio_bot_identity(),
    })


@bp.post("/twilio/conversations/webhook")
def twilio_conversations_webhook():
    if not _validate_twilio_request():
        abort(403)

    event_type = request.form.get("EventType", "")
    if event_type != "onMessageAdded":
        return ("", 204)

    author = request.form.get("Author", "")
    if author == twilio_bot_identity():
        return ("", 204)

    user = user_from_twilio_identity(author)
    if not user:
        current_app.logger.warning("Twilio chatbot ignored unknown author %r.", author)
        return ("", 204)

    message = request.form.get("Body", "")
    conversation_sid = request.form.get("ConversationSid", "")
    reply = handle_chat_message(user, message)
    post_twilio_conversation_reply(conversation_sid, format_reply_for_twilio(reply))
    return ("", 204)
