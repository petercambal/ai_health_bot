"""One-time interactive kaloricketabulky.sk login for testing/manual linking:
uv run python -m app.nutrition.bootstrap <telegram_id>

Prompts for email/password at the terminal (hidden), then stores ONLY the resulting
session cookies in the DB - the password never touches disk or the database. This is
the same flow /nutrition_link + the /nutrition-login web form drives for normal use;
this CLI variant exists for testing the login end-to-end without going through
Telegram, mirroring app/garmin/bootstrap.py.
"""

import argparse
import asyncio
import getpass
import sys

from app import database
from app.nutrition import web_login


async def main(telegram_id: int) -> None:
    email = input("kaloricketabulky.sk email: ").strip()
    password = getpass.getpass("kaloricketabulky.sk password (hidden while typing): ")

    print("Logging in to kaloricketabulky.sk...")
    try:
        cookies = await web_login.login(email, password)
    except web_login.NutritionLoginFailed as e:
        print(f"Login failed: {e}")
        sys.exit(1)

    await database.connect()
    try:
        await database.save_nutrition_cookies(telegram_id, cookies, label=email)
    except Exception as e:  # noqa: BLE001 - CLI entrypoint, report and exit cleanly
        print(
            f"Login succeeded, but saving to the DB failed: {e}\n"
            f"Check that telegram_id {telegram_id} exists in auth_user."
        )
        sys.exit(1)
    finally:
        await database.disconnect()

    print(
        f"\n✓ kaloricketabulky.sk account {email} is linked to telegram_id={telegram_id}.\n"
        "The password was never stored - only the session cookies, in the DB."
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "telegram_id", type=int, help="Telegram user_id to link the kaloricketabulky.sk account to"
    )
    args = parser.parse_args()
    asyncio.run(main(args.telegram_id))
