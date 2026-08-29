"""Публикация Reels входом по логину и паролю (мобильный API Instagram).

Отличие от instagram.py: Graph API требует Business-аккаунт, приложение Meta и
публичный адрес, с которого Instagram сам скачает ролик. Здесь клиент
представляется мобильным приложением, файл уходит напрямую, и ничего из этого
не нужно.

Плата — риск для аккаунта: Instagram не одобряет автоматизацию и отвечает на
подозрительную активность запросом подтверждения, а то и блокировкой. Поэтому
здесь: сессия сохраняется и переиспользуется (каждый вход по паролю выглядит
как вход с нового устройства), между запросами держатся паузы, а публиковать
стоит единицы роликов в сутки, а не десятки.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable

from ...config import settings
from ...media.runner import extract_thumbnail
from ...utils.crypto import CredentialsError, decrypt_json, encrypt_json
from ...utils.proxy import ProxyFormatError, normalize_proxy
from ...utils.text import merge_hashtags, truncate
from .base import ProgressCallback, PublishError, PublishRequest, PublishResult, Publisher

log = logging.getLogger(__name__)

CAPTION_LIMIT = 2200
# Паузы между запросами: подряд идущие обращения без задержек выглядят машинными.
DELAY_RANGE = [2, 6]
# Приметы «устройства», с которого Instagram видит вход. Их нельзя терять между
# попытками: подтверждение входа привязывается именно к устройству.
DEVICE_KEYS = (
    "uuids",
    "device_settings",
    "user_agent",
    "mid",
    "country",
    "country_code",
    "locale",
    "timezone_offset",
    "timezone_name",
)


class TwoFactorRequired(PublishError):
    """Нужен код двухфакторной аутентификации."""


class CheckpointRequired(PublishError):
    """Instagram требует подтвердить вход вручную (checkpoint)."""


def _client_class():  # noqa: ANN202
    try:
        from instagrapi import Client
    except ImportError as exc:
        raise PublishError(
            "не установлен instagrapi. Выполни: .venv\\Scripts\\pip install -r "
            "server/requirements.txt"
        ) from exc
    return Client


def _new_client(
    session: dict[str, Any] | None,
    proxy: str,
    challenge_code_handler: Callable[[str, Any], str] | None,
):  # noqa: ANN202
    Client = _client_class()
    # Приводим запись ещё раз: в учётных данных мог осесть вид продавца,
    # сохранённый до появления разбора или вписанный руками в базу.
    try:
        proxy = normalize_proxy(proxy)
    except ProxyFormatError as exc:
        raise PublishError(f"прокси аккаунта записан неправильно: {exc}") from exc

    client = Client(
        settings=dict(session or {}),
        proxy=proxy or None,
        delay_range=list(DELAY_RANGE),
    )
    client.challenge_code_handler = challenge_code_handler or _refuse_challenge
    client.change_password_handler = _refuse_password_change
    return client


# --- хранилище сессий -------------------------------------------------------
#
# Одно на панель и на scripts/ig_publish.py. Благодаря этому вход, который
# пришлось подтверждать в консоли, годится и для панели, а панель не плодит
# новые «устройства» на каждую попытку.

def normalize_username(username: str) -> str:
    return (username or "").strip().lstrip("@").lower()


def session_file(username: str) -> Path:
    return settings.storage_dir / "instagram" / f"{normalize_username(username)}.json"


def load_session(username: str) -> dict[str, Any]:
    """Читает сохранённую сессию. Понимает и старый открытый вид, и шифрованный.

    Старые файлы, встретившись, тут же переписываются шифрованными: иначе
    открытый sessionid лежал бы на диске до следующего входа, а входим мы редко.
    """
    path = session_file(username)
    if not username or not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("сохранённая сессия %s не читается: %s", path.name, exc)
        return {}

    try:
        return decrypt_json(raw.strip())
    except CredentialsError:
        pass  # либо старый открытый файл, либо сменился ключ — разберём ниже

    try:
        session = json.loads(raw)
    except ValueError:
        log.warning(
            "сессия %s не читается: похоже, SHORTS_SECRET_KEY сменился. "
            "Подключи аккаунт заново",
            path.name,
        )
        return {}

    log.info("переписываю сессию %s в шифрованный вид", path.name)
    save_session(username, session)
    return session


def save_session(username: str, session: dict[str, Any]) -> None:
    """Пишет сессию на диск в шифрованном виде.

    В сессии лежит sessionid — им одним заходят в аккаунт, без пароля и без
    второго фактора. Открытым текстом это ключ от аккаунта в папке проекта,
    хотя те же данные в базе давно шифруются.
    """
    if not username or not session:
        return
    path = session_file(username)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(encrypt_json(session), encoding="utf-8")
    except OSError as exc:  # не повод ронять публикацию
        log.warning("не удалось сохранить сессию %s: %s", path.name, exc)
    except CredentialsError as exc:
        log.warning("сессия %s не сохранена: %s", path.name, exc)


def keep_device_only(session: dict[str, Any] | None) -> dict[str, Any]:
    """Выбрасывает авторизацию, оставляя устройство.

    Нужно, когда вход хочется повторить с нуля, но остаться для Instagram тем
    же телефоном, что и раньше.
    """
    return {key: value for key, value in (session or {}).items() if key in DEVICE_KEYS}


def translate_error(exc: Exception) -> PublishError:
    """Превращает исключение instagrapi в понятную ошибку публикации."""
    from instagrapi import exceptions as ig

    if isinstance(exc, PublishError):
        return exc
    if isinstance(exc, ig.TwoFactorRequired):
        return TwoFactorRequired(
            "включена двухфакторная аутентификация — нужен код из приложения-"
            "аутентификатора или из SMS"
        )
    if isinstance(exc, (ig.BadPassword, ig.BadCredentials)):
        return PublishError("неверный логин или пароль")
    if isinstance(exc, ig.AccountSuspended):
        return PublishError("аккаунт заблокирован Instagram")
    if isinstance(exc, (ig.ChallengeError, ig.CaptchaChallengeRequired)):
        return CheckpointRequired(
            "Instagram требует подтвердить вход, и пройти проверку из кода нельзя. "
            "Открой instagram.com в браузере и войди там — проверка появится прямо "
            "на странице. Если после этого вход отсюда всё равно не проходит, "
            "подключись по sessionid из браузера (см. README, раздел «Публикация»). "
            f"Ответ Instagram: {exc}"
        )
    if isinstance(exc, ig.ProxyAddressIsBlocked):
        return PublishError("Instagram заблокировал адрес прокси — нужен другой")
    if isinstance(exc, ig.LoginRequired):
        return PublishError("сессия истекла — подключи аккаунт заново")
    if isinstance(exc, ig.FeedbackRequired):
        return PublishError(
            f"Instagram временно ограничил действия аккаунта: {exc}. Сделай паузу "
            "на несколько часов",
            retryable=True,
        )
    if isinstance(exc, (ig.PleaseWaitFewMinutes, ig.RateLimitError, ig.ClientThrottledError)):
        return PublishError("Instagram просит подождать — слишком частые запросы",
                            retryable=True)
    if isinstance(exc, (ig.ClientConnectionError, ig.ClientRequestTimeout)):
        return PublishError(f"нет связи с Instagram: {exc}", retryable=True)
    if isinstance(exc, ig.VideoTooLongException):
        return PublishError("ролик слишком длинный для Reels")
    if isinstance(exc, (ig.ClipNotUpload, ig.ClipConfigureError, ig.VideoNotUpload)):
        return PublishError(
            f"Instagram не принял ролик: {exc}. Проверь, что это MP4 (H.264 + AAC), "
            "вертикальный и не длиннее 15 минут",
            retryable=True,
        )
    if isinstance(exc, ig.ClientError):
        return PublishError(f"Instagram: {exc}")
    return PublishError(f"неожиданная ошибка Instagram: {exc}")


def _refuse_challenge(username: str, choice: Any = None) -> str:
    """Заглушка вместо интерактивного ввода кода.

    По умолчанию instagrapi ждёт код в input() — на сервере это вечное
    ожидание в потоке воркера. Лучше сразу сказать, что делать.
    """
    raise CheckpointRequired(
        f"Instagram требует подтвердить вход аккаунта @{username} кодом из письма "
        "или SMS. Сервер такой код спросить не может: запусти "
        "scripts\\ig_publish.py --check — она спросит код в консоли, а сохранённая "
        "сессия подойдёт и панели"
    )


def _refuse_password_change(username: str) -> str:
    raise CheckpointRequired(
        f"Instagram требует сменить пароль аккаунта {username}. Сделай это вручную "
        "в приложении и подключи аккаунт заново"
    )


def clean_sessionid(sessionid: str) -> str:
    """Приводит вставленную куку к виду, который ждёт Instagram.

    Из инструментов разработчика её копируют по-разному: с именем `sessionid=`,
    в кавычках, с переносом строки на конце. Ошибка при этом невнятная, поэтому
    чистим сами и сразу проверяем форму: кука начинается с id аккаунта.
    """
    value = (sessionid or "").strip().strip('"').strip("'").strip()
    if value.lower().startswith("sessionid="):
        value = value[len("sessionid=") :].strip()
    value = value.rstrip(";").strip()
    if len(value) < 30 or not value[0].isdigit():
        raise PublishError(
            "это не похоже на sessionid. Нужна строка вида 71234567890%3AAbCd… — "
            "она начинается с цифр (это id аккаунта) и длиннее 30 символов. "
            "Скопируй её из столбца Value, а не имя куки"
        )
    return value


def resolve_verification_code(verification_code: str = "", totp_seed: str = "") -> str:
    """Код 2FA: введённый вручную или сгенерированный из сохранённого секрета."""
    code = (verification_code or "").strip()
    if code:
        return code
    seed = (totp_seed or "").strip()
    if not seed:
        return ""
    Client = _client_class()
    return Client.totp_generate_code(seed.replace(" ", ""))


def login(
    username: str,
    password: str,
    *,
    session: dict[str, Any] | None = None,
    proxy: str = "",
    verification_code: str = "",
    totp_seed: str = "",
    challenge_code_handler: Callable[[str, Any], str] | None = None,
):  # noqa: ANN201
    """Возвращает залогиненный клиент instagrapi.

    Сначала пробует сохранённую сессию: instagrapi сам проверит её и, если
    Instagram сессию отверг, переподключится по паролю с тем же device_id.

    Настройки клиента сохраняются и после неудачи — это не забота о скорости, а
    условие, при котором повтор вообще имеет смысл: подтверждение входа
    Instagram привязывает к устройству, и попытка с новыми идентификаторами
    упрётся в новую проверку.
    """
    username = normalize_username(username)
    if not username or not password:
        raise PublishError("нужны логин и пароль Instagram")

    client = _new_client(
        dict(session or {}) or load_session(username), proxy, challenge_code_handler
    )
    try:
        ok = client.login(
            username,
            password,
            verification_code=resolve_verification_code(verification_code, totp_seed),
        )
    except Exception as exc:  # noqa: BLE001 — весь разбор в translate_error
        save_session(username, client.get_settings())
        raise translate_error(exc) from exc

    if not ok:
        save_session(username, client.get_settings())
        raise PublishError("Instagram не принял вход, причина не указана")
    save_session(username, client.get_settings())
    return client


def login_with_sessionid(
    sessionid: str, *, proxy: str = "", session: dict[str, Any] | None = None
):  # noqa: ANN201
    """Вход по cookie sessionid из браузера.

    Единственный способ подключиться, когда Instagram упёрся в проверку,
    которую нельзя пройти из кода: в браузере проверка проходится вживую, а
    сюда попадает уже готовая сессия.
    """
    sessionid = clean_sessionid(sessionid)

    client = _new_client(session or {}, proxy, None)
    try:
        client.login_by_sessionid(sessionid)
    except Exception as exc:  # noqa: BLE001
        raise translate_error(exc) from exc

    save_session(client.username or "", client.get_settings())
    return client


def resume(username: str, *, session: dict[str, Any] | None = None, proxy: str = ""):  # noqa: ANN201
    """Вход только по сохранённой сессии, без пароля.

    Так работают аккаунты, подключённые по sessionid: продлить сессию нечем,
    поэтому при её потере остаётся только подключить аккаунт заново.
    """
    session = dict(session or {}) or load_session(username)
    if not session:
        raise PublishError("нет сохранённой сессии Instagram — подключи аккаунт заново")

    client = _new_client(session, proxy, None)
    try:
        client.account_info()
    except Exception as exc:  # noqa: BLE001
        raise translate_error(exc) from exc

    save_session(client.username or username, client.get_settings())
    return client


class InstagramLoginPublisher(Publisher):
    """credentials: username, password, session, totp_seed, proxy."""

    platform = "instagram"

    def __init__(self, credentials: dict[str, Any], meta: dict[str, Any] | None = None) -> None:
        super().__init__(credentials, meta)
        self._client = None
        self._session_before: dict[str, Any] = dict(credentials.get("session") or {})

    def attach_client(self, client) -> None:  # noqa: ANN001
        """Принимает уже залогиненный клиент.

        Нужно для CLI: там код 2FA и подтверждение входа спрашивают у человека
        вживую, а сервер такой возможности не имеет.
        """
        self._client = client

    def client(self):  # noqa: ANN201
        if self._client is None:
            credentials = self.credentials
            username = credentials.get("username", "")
            proxy = credentials.get("proxy", "")
            session = credentials.get("session")
            if credentials.get("password"):
                self._client = login(
                    username,
                    credentials["password"],
                    session=session,
                    proxy=proxy,
                    totp_seed=credentials.get("totp_seed", ""),
                )
            else:
                # Аккаунт подключён по sessionid — пароля нет, продлевать нечем.
                self._client = resume(username, session=session, proxy=proxy)
        return self._client

    def verify(self) -> tuple[bool, str]:
        try:
            info = self.client().account_info()
        except PublishError as exc:
            return False, str(exc)
        except Exception as exc:  # noqa: BLE001
            return False, str(translate_error(exc))
        return True, f"@{info.username}"

    def publish(
        self,
        request: PublishRequest,
        *,
        on_progress: ProgressCallback | None = None,
        on_log: Callable[[str], None] | None = None,
    ) -> PublishResult:
        if not request.video_path.exists():
            raise PublishError(f"файл не найден: {request.video_path}")

        caption = merge_hashtags(request.description, request.hashtags)

        if on_progress:
            on_progress(0.05, "вход в аккаунт")
        client = self.client()

        thumbnail = self._thumbnail(request, on_log=on_log)
        if on_log:
            on_log(f"загружаю {request.video_path.name} ({request.video_path.stat().st_size >> 20} МБ)")
        if on_progress:
            on_progress(0.2, "загружаю ролик")

        try:
            media = client.clip_upload(
                request.video_path,
                truncate(caption, CAPTION_LIMIT),
                thumbnail=thumbnail,
                show_preview_in_feed=bool(request.extra.get("share_to_feed", True)),
            )
        except Exception as exc:  # noqa: BLE001
            raise translate_error(exc) from exc

        if on_progress:
            on_progress(1.0, "опубликовано")

        code = getattr(media, "code", "") or ""
        return PublishResult(
            remote_id=str(getattr(media, "pk", "") or ""),
            url=f"https://www.instagram.com/reel/{code}/" if code else "",
            raw={"code": code, "thumbnail_url": str(getattr(media, "thumbnail_url", "") or "")},
        )

    def refreshed_credentials(self) -> dict[str, Any] | None:
        """Сессия обновляется при каждом входе — её нужно сохранить обратно."""
        if self._client is None:
            return None
        session = self._client.get_settings()
        if session == self._session_before:
            return None
        return {**self.credentials, "session": session}

    def _thumbnail(
        self, request: PublishRequest, *, on_log: Callable[[str], None] | None
    ) -> Path:
        """Обложка обязательна: свою instagrapi добывает через moviepy, которого
        в проекте нет и не нужно — кадр снимает наш ffmpeg."""
        if request.thumbnail_path and Path(request.thumbnail_path).exists():
            return Path(request.thumbnail_path)
        target = settings.thumbs_dir / f"{request.video_path.stem}.ig.jpg"
        if on_log:
            on_log("снимаю кадр для обложки")
        try:
            return extract_thumbnail(request.video_path, target, at_second=1.0)
        except Exception as exc:  # noqa: BLE001
            raise PublishError(f"не удалось снять обложку для Reels: {exc}") from exc
