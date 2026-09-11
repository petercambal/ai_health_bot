"""Browser-based Garmin login for /garmin-login (see app/routers/garmin_login.py).

Same security property as the CLI bootstrap (app/garmin/bootstrap.py): the password
is only ever held in memory for the duration of this login, never persisted. MFA needs
two HTTP requests (password, then code), so a pending login's in-memory Garmin client
is kept here briefly, keyed by a random token - garminconnect's resume_login() only
works on the same client instance that started the login (its `client_state` param is
vestigial/unused), so this can't be a stateless token you hand back to the browser.
"""

import secrets
import time

from garminconnect import Garmin

_PENDING_TTL_SECONDS = 600
_pending: dict[str, tuple[Garmin, float]] = {}


def _prune_expired() -> None:
    now = time.monotonic()
    for token in [t for t, (_, created) in _pending.items() if now - created > _PENDING_TTL_SECONDS]:
        _pending.pop(token, None)


def start_login(email: str, password: str) -> tuple[str, bool]:
    """Returns (result, mfa_required).

    mfa_required=False: result is the session_json to persist immediately.
    mfa_required=True: result is a pending-login token to pass to complete_login().
    """
    _prune_expired()
    client = Garmin(email, password, return_on_mfa=True)
    needs_mfa, _ = client.login()
    if needs_mfa == "needs_mfa":
        token = _new_token()
        _pending[token] = (client, time.monotonic())
        return token, True
    return client.client.dumps(), False


def complete_login(token: str, mfa_code: str) -> str:
    """Completes a pending MFA login and returns the session_json to persist.

    A wrong code leaves the pending entry in place so the caller can retry (matches
    garminconnect's own resume_login behavior); raises KeyError if the token is
    unknown or expired.
    """
    _prune_expired()
    entry = _pending.get(token)
    if entry is None:
        raise KeyError("Pending Garmin login not found or expired - start again.")
    client, _created = entry
    client.resume_login({}, mfa_code)  # client_state arg is unused by garminconnect
    _pending.pop(token, None)
    return client.client.dumps()


def _new_token() -> str:
    return secrets.token_urlsafe(24)


__all__ = ["complete_login", "start_login"]
