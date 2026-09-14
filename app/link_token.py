"""Signed, time-limited tokens for the /garmin-login and /nutrition-login links sent
by the bot (previously lived in app/garmin/link_token.py - moved here once a second
service started using it, since it has nothing Garmin-specific in it).

A user gets a link only by asking the bot itself (/garmin_link, /nutrition_link),
which already knows their real Telegram id - so the link doesn't need the user to
know or type their own id, and a stranger can't construct a valid one for someone
else's id without the HMAC secret. Time-limited (not single-use) is enough for this
app's trust model: a small group behind a private tunnel/NAS reverse proxy, not the
open internet.
"""

import base64
import hashlib
import hmac
import time

from app.config import settings

_TTL_SECONDS = 30 * 60

# Reuses the webhook secret as the HMAC key rather than adding a dedicated config
# value - it's already a per-deployment secret not exposed to end users.
_SECRET = settings.telegram_webhook_secret.encode()


def generate_link_token(user_id: int) -> str:
    expires_at = int(time.time()) + _TTL_SECONDS
    payload = f"{user_id}:{expires_at}".encode()
    signature = hmac.new(_SECRET, payload, hashlib.sha256).hexdigest()[:32]
    encoded_payload = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    return f"{encoded_payload}.{signature}"


def verify_link_token(token: str) -> int | None:
    try:
        encoded_payload, signature = token.split(".", 1)
        padding = "=" * (-len(encoded_payload) % 4)
        payload = base64.urlsafe_b64decode(encoded_payload + padding)
        user_id_str, expires_at_str = payload.decode().split(":", 1)
    except (ValueError, UnicodeDecodeError):
        return None

    expected_signature = hmac.new(_SECRET, payload, hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(signature, expected_signature):
        return None
    if int(expires_at_str) < time.time():
        return None

    return int(user_id_str)
