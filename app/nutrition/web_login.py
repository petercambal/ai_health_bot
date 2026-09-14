"""Browser-based kaloricketabulky.sk login for /nutrition-login (see
app/routers/nutrition_login.py).

No official login API exists, so this replays the site's own frontend login request.
The password is MD5-hashed client-side before being sent (confirmed by hashing a
known test password and matching a captured real request byte-for-byte) - not a
choice made here, just matching what the site itself does. Same security property as
the Garmin integration (app/garmin/web_login.py): the plaintext password is only ever
held in memory for the duration of this one login call, never persisted or logged.
"""

import hashlib

import aiohttp

_LOGIN_URL = "https://www.kaloricketabulky.sk/login/create"
_TIMEOUT = aiohttp.ClientTimeout(total=15)

# The auth cookie set on a successful login (see app/database.py's nutrition_cookies
# usage) - its presence is how a successful login is distinguished from a rejected
# one, since the response body's shape on failure isn't known.
_AUTH_COOKIE = "kaloricketabulky_token"


class NutritionLoginFailed(Exception):
    """Login was rejected - wrong credentials, or the site's login flow changed."""


async def login(email: str, password: str) -> dict[str, str]:
    """Logs in and returns the resulting cookie jar as a plain {name: value} dict,
    ready to store via database.save_nutrition_cookies. Raises NutritionLoginFailed
    if the response doesn't carry the expected auth cookie."""
    hashed_password = hashlib.md5(password.encode()).hexdigest()  # matches the site's own client-side hashing

    async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
        try:
            async with session.post(
                _LOGIN_URL,
                params={"format": "json", "voucher": "false"},
                json={"email": email, "password": hashed_password},
            ) as resp:
                if resp.status != 200:
                    raise NutritionLoginFailed(f"Login request failed (status={resp.status})")
        except aiohttp.ClientError as e:
            raise NutritionLoginFailed(f"Login request failed: {e}") from e

        cookies = {c.key: c.value for c in session.cookie_jar}

    if _AUTH_COOKIE not in cookies:
        raise NutritionLoginFailed("Login did not return a session - check email/password.")
    return cookies


__all__ = ["NutritionLoginFailed", "login"]
