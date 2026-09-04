"""Вход в TikTok для аккаунта, подключённого через свой браузер (Patchright).

Открывает видимое окно Chromium с профилем аккаунта, ждёт, пока ты войдёшь в
TikTok, и отмечает аккаунт активным. Тем же окном чинится сессия, когда TikTok
её сбросит, — профиль остаётся прежним.

Примеры:
    python scripts/tiktok_login.py --list
    python scripts/tiktok_login.py --id 3
    python scripts/tiktok_login.py "мой канал"

Сам аккаунт заводится в панели: «Аккаунты» → «TikTok (браузер)».
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "server"))

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from app.db import session_scope  # noqa: E402
from app.models import Account  # noqa: E402
from app.providers.publish import PublishError, publisher_for_account  # noqa: E402
from app.utils.crypto import encrypt_json  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="tiktok_login",
        description="Открывает окно входа в TikTok для браузерного аккаунта",
    )
    parser.add_argument("name", nargs="?", default="", help="часть названия аккаунта")
    parser.add_argument("--id", type=int, default=0, help="id аккаунта из панели")
    parser.add_argument("--list", action="store_true", help="показать браузерные аккаунты TikTok и выйти")
    parser.add_argument("--timeout", type=int, default=300, help="сколько секунд ждать входа")
    return parser.parse_args(argv)


def browser_accounts(db) -> list[Account]:  # noqa: ANN001
    rows = db.query(Account).filter(Account.platform == "tiktok").all()
    return [a for a in rows if (a.meta or {}).get("auth") == "patchright"]


def pick(db, args: argparse.Namespace) -> Account:  # noqa: ANN001
    accounts = browser_accounts(db)
    if not accounts:
        raise SystemExit(
            "нет ни одного браузерного аккаунта TikTok. Заведи его в панели: "
            "«Аккаунты» → «TikTok (браузер)»"
        )
    if args.id:
        found = next((a for a in accounts if a.id == args.id), None)
        if not found:
            raise SystemExit(f"аккаунт с id {args.id} не найден среди браузерных TikTok")
        return found
    if args.name:
        needle = args.name.lower().lstrip("@")
        matches = [a for a in accounts if needle in a.name.lower()]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise SystemExit(f"по «{args.name}» ничего не нашёл")
        raise SystemExit(
            "под «{}» подходит несколько — уточни по --id:\n  {}".format(
                args.name, "\n  ".join(f"{a.id}: {a.name}" for a in matches)
            )
        )
    if len(accounts) == 1:
        return accounts[0]
    raise SystemExit(
        "браузерных аккаунтов несколько — укажи --id:\n  "
        + "\n  ".join(f"{a.id}: {a.name}" for a in accounts)
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    with session_scope() as db:
        if args.list:
            rows = browser_accounts(db)
            if not rows:
                print("браузерных аккаунтов TikTok нет")
            for account in rows:
                state = "активен" if account.is_active else "нет входа"
                print(f"{account.id}: {account.name} — {state}")
            return 0

        account = pick(db, args)
        account_id, account_name = account.id, account.name
        print(f"аккаунт: {account_name} (id {account_id})")
        publisher = publisher_for_account(account)

    try:
        username = publisher.login_interactive(
            timeout=args.timeout, on_log=lambda msg: print(f"  {msg}")
        )
    except PublishError as exc:
        raise SystemExit(f"вход не удался: {exc}") from None

    with session_scope() as db:
        account = db.get(Account, account_id)
        if account is None:
            raise SystemExit("аккаунт исчез, пока шёл вход")
        # Слепок входа уезжает в общую базу: тот же аккаунт заработает на любой
        # машине, переносить профиль руками не нужно.
        refreshed = publisher.refreshed_credentials()
        if refreshed:
            account.credentials_enc = encrypt_json(refreshed)

        account.is_active = True
        account.last_error = ""
        if username:
            account.name = f"@{username}"
            account.meta = {**(account.meta or {}), "username": username}

    who = f"@{username}" if username else account_name
    print(f"готово: {who} вошёл, аккаунт активен. Панель возьмёт эту же сессию.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nпрервано")
        sys.exit(130)
