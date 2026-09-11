"""One-time interactive Garmin login: uv run python -m app.garmin.bootstrap <telegram_id>

Prompts for email/password/MFA at the terminal, then stores ONLY the resulting
session token in the DB (garmin_account.session_json) - the password never touches
disk or the database. Every subsequent sync (cron or Telegram-triggered) reuses that
stored session and never asks for credentials again, unless it's later revoked or
expires, in which case re-run this script.
"""

import argparse
import asyncio
import getpass
import sys

from garminconnect import Garmin

from app import database


def _prompt_mfa() -> str:
    print("\nGarmin vyžaduje MFA / 2FA kód.")
    return input("Zadaj kód zo SMS alebo e-mailu: ").strip()


async def main(telegram_id: int) -> None:
    email = input("Garmin email: ").strip()
    password = getpass.getpass("Garmin heslo (nezobrazuje sa pri písaní): ")

    print("Prihlasujem sa do Garmin Connect...")
    client = Garmin(email, password, prompt_mfa=_prompt_mfa)
    try:
        client.login()
    except Exception as e:  # noqa: BLE001 - CLI entrypoint, report and exit cleanly
        print(f"Prihlásenie zlyhalo: {e}")
        sys.exit(1)

    session_json = client.client.dumps()

    await database.connect()
    try:
        await database.save_garmin_session(telegram_id, email, session_json)
    except Exception as e:  # noqa: BLE001 - CLI entrypoint, report and exit cleanly
        print(
            f"Prihlásenie prebehlo, ale uloženie do DB zlyhalo: {e}\n"
            f"Over, že telegram_id {telegram_id} existuje v auth_user."
        )
        sys.exit(1)
    finally:
        await database.disconnect()

    print(
        f"\n✓ Garmin účet {email} je prepojený s telegram_id={telegram_id}.\n"
        "Heslo nikde neostalo uložené - iba session token v DB."
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "telegram_id", type=int, help="Telegram user_id, ktorému sa má Garmin účet priradiť"
    )
    args = parser.parse_args()
    asyncio.run(main(args.telegram_id))
