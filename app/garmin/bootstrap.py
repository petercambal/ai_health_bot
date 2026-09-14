"""One-time interactive Garmin login: uv run python -m app.garmin.bootstrap <telegram_id>

Prompts for email/password/MFA at the terminal, then stores ONLY the resulting
session token in the DB (integrations.credentials, service='garmin') - the password
never touches disk or the database. Every subsequent sync (cron or Telegram-triggered)
reuses that stored session and never asks for credentials again, unless it's later
revoked or expires, in which case re-run this script.
"""

import argparse
import asyncio
import getpass
import sys

from garminconnect import Garmin

from app import database


def _prompt_mfa() -> str:
    print("\nGarmin requires an MFA / 2FA code.")
    return input("Enter the code from SMS or email: ").strip()


async def main(telegram_id: int) -> None:
    email = input("Garmin email: ").strip()
    password = getpass.getpass("Garmin password (hidden while typing): ")

    print("Logging in to Garmin Connect...")
    client = Garmin(email, password, prompt_mfa=_prompt_mfa)
    try:
        client.login()
    except Exception as e:  # noqa: BLE001 - CLI entrypoint, report and exit cleanly
        print(f"Login failed: {e}")
        sys.exit(1)

    session_json = client.client.dumps()

    await database.connect()
    try:
        await database.save_garmin_session(telegram_id, email, session_json)
    except Exception as e:  # noqa: BLE001 - CLI entrypoint, report and exit cleanly
        print(
            f"Login succeeded, but saving to the DB failed: {e}\n"
            f"Check that telegram_id {telegram_id} exists in auth_user."
        )
        sys.exit(1)
    finally:
        await database.disconnect()

    print(
        f"\n✓ Garmin account {email} is linked to telegram_id={telegram_id}.\n"
        "The password was never stored - only the session token, in the DB."
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "telegram_id", type=int, help="Telegram user_id to link the Garmin account to"
    )
    args = parser.parse_args()
    asyncio.run(main(args.telegram_id))
