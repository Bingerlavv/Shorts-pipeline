"""Публикация готового вертикального ролика в Instagram Reels по логину и паролю.

Скрипт самостоятельный: ни база, ни очередь, ни веб-панель не нужны — только
файл, подпись и учётные данные. Работает через мобильный API Instagram
(instagrapi), поэтому Business-аккаунт, приложение Meta и публичный адрес
сервера не требуются.

Примеры:
    python scripts/ig_publish.py --check
    python scripts/ig_publish.py storage/renders/clip.mp4 --caption "Текст"
    python scripts/ig_publish.py clip1.mp4 clip2.mp4 --caption-file text.txt --delay 900

Учётные данные берутся из .env в корне репозитория:
    INSTAGRAM_USERNAME=...
    INSTAGRAM_PASSWORD=...
    INSTAGRAM_TOTP_SEED=...   # секрет 2FA (base32), если включена двухфакторка
    INSTAGRAM_PROXY=...       # http://user:pass@host:port, необязательно

Пароль намеренно не принимается аргументом командной строки: аргументы видны
в списке процессов и оседают в истории оболочки. Если в .env его нет, скрипт
спросит пароль сам.

Что важно знать про этот способ: Instagram автоматизацию не одобряет. Чтобы
не поймать блокировку — публикуй единицы роликов в сутки, держи паузы между
ними и не переключай аккаунт между разными адресами (либо всегда используй
один прокси). Сессия сохраняется в storage/instagram/, повторный вход по
паролю Instagram видит как подключение нового устройства.
"""

from __future__ import annotations

import argparse
import getpass
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "server"))

# Кириллица не должна ронять вывод, когда его перенаправляют в файл или в
# конвейер: там кодировка системная, а в ней нет ни «—», ни «…».
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from app.config import settings  # noqa: E402
from app.providers.publish.base import PublishError, PublishRequest  # noqa: E402
from app.providers.publish.instagram_login import (  # noqa: E402
    CheckpointRequired,
    InstagramLoginPublisher,
    TwoFactorRequired,
    keep_device_only,
    load_session,
    login_with_sessionid,
    normalize_username,
    save_session,
    session_file,
)
from app.providers.publish.instagram_login import login as instagram_login  # noqa: E402

# Между публикациями в один аккаунт нужен зазор: подряд идущие загрузки —
# первое, на что Instagram реагирует ограничением.
DEFAULT_DELAY = 600


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="ig_publish",
        description="Публикует ролики в Instagram Reels входом по логину и паролю",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("videos", nargs="*", type=Path, help="MP4-файлы (H.264 + AAC, вертикальные)")
    parser.add_argument("--user", default="", help="логин; по умолчанию INSTAGRAM_USERNAME из .env")
    parser.add_argument("--caption", default="", help="подпись к ролику")
    parser.add_argument("--caption-file", type=Path, help="файл с подписью (UTF-8)")
    parser.add_argument("--hashtags", default="", help="хэштеги через пробел, добавятся к подписи")
    parser.add_argument("--thumbnail", type=Path, help="обложка; по умолчанию берётся кадр из ролика")
    parser.add_argument("--proxy", default="", help="прокси; по умолчанию INSTAGRAM_PROXY из .env")
    parser.add_argument(
        "--delay",
        type=int,
        default=DEFAULT_DELAY,
        help=f"пауза в секундах между роликами (по умолчанию {DEFAULT_DELAY})",
    )
    parser.add_argument("--no-feed", action="store_true", help="не показывать Reel в ленте профиля")
    parser.add_argument("--check", action="store_true", help="только проверить вход, ничего не публиковать")
    parser.add_argument(
        "--relogin",
        action="store_true",
        help="войти заново, не трогая сохранённое устройство",
    )
    parser.add_argument(
        "--sessionid",
        nargs="?",
        const="",
        default=None,
        help="войти по cookie sessionid из браузера вместо пароля; "
        "без значения скрипт спросит его скрытым вводом",
    )
    return parser.parse_args(argv)


def resolve_caption(args: argparse.Namespace) -> str:
    caption = args.caption
    if args.caption_file:
        if not args.caption_file.exists():
            raise SystemExit(f"файл подписи не найден: {args.caption_file}")
        caption = args.caption_file.read_text(encoding="utf-8")
    hashtags = args.hashtags.strip()
    if hashtags:
        caption = f"{caption.rstrip()}\n\n{hashtags}"
    return caption.strip()


def ask_challenge_code(username: str, choice) -> str:  # noqa: ANN001
    """Instagram прислал код на почту или в SMS и ждёт его обратно."""
    where = "SMS" if getattr(choice, "name", "") == "SMS" else "почту"
    print(f"\nInstagram отправил код подтверждения на {where} аккаунта @{username}.")
    return input("Код из письма/SMS: ").strip()


def login_with_prompt(attempt) -> object:  # noqa: ANN001
    """Спрашивает код двухфакторки, если он понадобился и не задан в .env."""
    try:
        return attempt()
    except TwoFactorRequired:
        pass
    code = input("Код двухфакторной аутентификации: ").strip()
    if not code:
        raise SystemExit("код не введён")
    return attempt(code)


def resolve_sessionid(args: argparse.Namespace) -> str:
    """Пустая строка — значит входим паролем."""
    if args.sessionid is None:
        return settings.instagram_sessionid.strip()
    # `--sessionid` без значения: спрашиваем скрытым вводом, чтобы кука не
    # осталась в истории оболочки.
    return (args.sessionid or getpass.getpass("sessionid из браузера: ")).strip()


def connect(args: argparse.Namespace):  # noqa: ANN201
    proxy = (args.proxy or settings.instagram_proxy).strip()
    if proxy:
        print(f"через прокси {proxy.split('@')[-1]}")

    sessionid = resolve_sessionid(args)
    if sessionid:
        print("вхожу по sessionid…")
        try:
            client = login_with_sessionid(sessionid, proxy=proxy)
        except PublishError as exc:
            raise SystemExit(f"вход не удался: {exc}") from None
        return client.username, "", client

    username = normalize_username(args.user or settings.instagram_username)
    if not username:
        raise SystemExit("не задан логин: заполни INSTAGRAM_USERNAME в .env или передай --user")

    password = settings.instagram_password
    if not password:
        password = getpass.getpass(f"Пароль Instagram для @{username}: ")
    if not password:
        raise SystemExit("пароль пустой")

    # Сохранённую сессию login() подхватит сам. При --relogin отдаём ему только
    # устройство: для Instagram важно, чтобы повтор пришёл с того же «телефона».
    session = keep_device_only(load_session(username)) if args.relogin else None

    def attempt(verification_code: str = ""):  # noqa: ANN202
        return instagram_login(
            username,
            password,
            session=session,
            proxy=proxy,
            verification_code=verification_code,
            totp_seed=settings.instagram_totp_seed,
            challenge_code_handler=ask_challenge_code,
        )

    print(f"вхожу как @{username}…")
    try:
        client = login_with_prompt(attempt)
    except CheckpointRequired as exc:
        print(f"\n{exc}\n")
        raise SystemExit(
            "Подтверждать, скорее всего, нечего: уведомления, письма и записи в\n"
            "«Активности входа» Instagram для незнакомого клиента не создаёт.\n"
            "Рабочий путь — войти по sessionid, проверку пройдёт браузер:\n"
            "  1. Войди на instagram.com в браузере.\n"
            "  2. F12 → Application → Storage → Cookies → https://www.instagram.com\n"
            "  3. Скопируй значение куки sessionid (столбец Value).\n"
            "     Через document.cookie её не видно — она HttpOnly.\n"
            "  4. Запусти: python scripts\\ig_publish.py --sessionid --check"
        ) from None
    except PublishError as exc:
        raise SystemExit(f"вход не удался: {exc}") from None

    return username, password, client


def publish_one(
    publisher: InstagramLoginPublisher,
    video: Path,
    caption: str,
    args: argparse.Namespace,
) -> str:
    request = PublishRequest(
        video_path=video,
        title="",
        description=caption,
        thumbnail_path=args.thumbnail,
        extra={"share_to_feed": not args.no_feed},
    )
    result = publisher.publish(
        request,
        on_progress=lambda fraction, message: print(f"  [{fraction * 100:3.0f}%] {message}"),
        on_log=lambda message: print(f"  {message}"),
    )
    return result.url or result.remote_id


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.videos and not args.check:
        raise SystemExit("нечего публиковать: укажи файлы или запусти с --check")

    missing = [str(video) for video in args.videos if not video.exists()]
    if missing:
        raise SystemExit("файлы не найдены: " + ", ".join(missing))
    if args.thumbnail and not args.thumbnail.exists():
        raise SystemExit(f"обложка не найдена: {args.thumbnail}")

    caption = resolve_caption(args)
    username, password, client = connect(args)

    info = client.account_info()
    print(f"вошли: @{info.username} ({info.full_name})")
    if args.check:
        print(f"сессия сохранена: {session_file(info.username)}")
        print("панель возьмёт её же, если подключить этот аккаунт в разделе «Аккаунты»")
        return 0

    publisher = InstagramLoginPublisher({"username": username, "password": password})
    publisher.attach_client(client)

    failed = 0
    for index, video in enumerate(args.videos):
        print(f"\n[{index + 1}/{len(args.videos)}] {video.name}")
        try:
            print(f"  готово: {publish_one(publisher, video, caption, args)}")
        except PublishError as exc:
            failed += 1
            print(f"  не опубликовано: {exc}")
        finally:
            # Сессия меняется по ходу работы — сохраняем даже после ошибки,
            # иначе следующий запуск снова пойдёт входом по паролю.
            save_session(username, client.get_settings())

        if index + 1 < len(args.videos) and args.delay > 0:
            print(f"  пауза {args.delay} с перед следующим роликом…")
            time.sleep(args.delay)

    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nпрервано")
        sys.exit(130)
