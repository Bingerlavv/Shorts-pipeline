"""Публикация в TikTok через Content Posting API.

У площадки два пути, и разница между ними принципиальная.

«Черновик» (scope video.upload) кладёт ролик во «Входящие» приложения TikTok:
человек открывает уведомление и публикует сам, одним касанием. Работает сразу,
никаких проверок приложения не нужно.

«Сразу в профиль» (scope video.publish) выкладывает без участия человека. Но
пока приложение не прошло аудит TikTok, всё опубликованное принудительно
становится видно только автору — в ответ прилетает
unaudited_client_can_only_post_to_private_accounts. То есть до аудита этот путь
для охвата бесполезен, и по умолчанию мы им не пользуемся.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Callable

import httpx

from ...config import settings
from ...utils.text import merge_hashtags, truncate
from .base import ProgressCallback, PublishError, PublishRequest, PublishResult, Publisher

log = logging.getLogger(__name__)

API = "https://open.tiktokapis.com/v2"
TOKEN_URL = f"{API}/oauth/token/"

# Требования площадки к кускам: не меньше 5 МБ и не больше 64 МБ, последний
# может дотянуть до 128 МБ, всего не больше 1000 кусков. Файл меньше 5 МБ
# заливается целиком одним куском.
MIN_CHUNK = 5 * 1024 * 1024
MAX_CHUNK = 64 * 1024 * 1024
MAX_CHUNKS = 1000
MAX_FILE = 4 * 1024 * 1024 * 1024

TITLE_LIMIT = 2200  # UTF-16 runes по документации; для кириллицы запас есть

# Сколько ждать, пока TikTok обработает залитое. Обработка минутная, но на
# длинных роликах бывает дольше.
STATUS_TIMEOUT = 600
STATUS_INTERVAL = 5.0

TERMINAL_OK = {"PUBLISH_COMPLETE", "SEND_TO_USER_INBOX"}


class TikTokPublisher(Publisher):
    platform = "tiktok"

    def __init__(self, credentials: dict[str, Any], meta: dict[str, Any] | None = None) -> None:
        super().__init__(credentials, meta)
        self._refreshed: dict[str, Any] | None = None

    # --- учётные данные -------------------------------------------------

    def refreshed_credentials(self) -> dict[str, Any] | None:
        return self._refreshed

    def _client(self) -> httpx.Client:
        # trust_env=False: локальный прокси из переменных окружения к TikTok
        # отношения не имеет, а вот сломать запрос может.
        proxy = str(self.credentials.get("proxy") or "").strip() or None
        return httpx.Client(timeout=120, trust_env=False, proxy=proxy)

    def _token(self) -> str:
        """Живой access_token. Обновляет его, если срок вышел или на исходе."""
        token = str(self.credentials.get("access_token") or "")
        expires_at = float(self.credentials.get("expires_at") or 0)
        if token and expires_at - time.time() > 120:
            return token

        refresh = str(self.credentials.get("refresh_token") or "")
        if not refresh:
            raise PublishError(
                "у аккаунта TikTok нет refresh_token — подключи его заново на "
                "странице «Аккаунты»"
            )
        if not settings.tiktok_client_key or not settings.tiktok_client_secret:
            raise PublishError(
                "не заданы TIKTOK_CLIENT_KEY и TIKTOK_CLIENT_SECRET — без них "
                "обновить доступ невозможно"
            )

        with self._client() as client:
            response = client.post(
                TOKEN_URL,
                data={
                    "client_key": settings.tiktok_client_key,
                    "client_secret": settings.tiktok_client_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        payload = _json(response)
        if response.status_code != 200 or not payload.get("access_token"):
            raise PublishError(
                f"TikTok не обновил доступ: {_error_text(payload)}. "
                "Скорее всего, refresh_token истёк — подключи аккаунт заново",
                retryable=False,
            )

        # Обновлённые данные вернутся наружу и лягут в базу: refresh_token у
        # TikTok одноразовый, и старый после обмена уже не сработает.
        updated = dict(self.credentials)
        updated.update(
            access_token=payload["access_token"],
            refresh_token=payload.get("refresh_token", refresh),
            expires_at=time.time() + float(payload.get("expires_in", 0) or 0),
        )
        self.credentials = updated
        self._refreshed = updated
        return updated["access_token"]

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token()}",
            "Content-Type": "application/json; charset=UTF-8",
        }

    # --- проверка -------------------------------------------------------

    def verify(self) -> tuple[bool, str]:
        try:
            info = self._creator_info()
        except PublishError as exc:
            return False, str(exc)
        name = info.get("creator_nickname") or info.get("creator_username") or "аккаунт"
        limit = info.get("max_video_post_duration_sec")
        tail = f", ролики до {limit} с" if limit else ""
        return True, f"TikTok: {name}{tail}"

    def _creator_info(self) -> dict[str, Any]:
        """Сведения об авторе: разрешённые уровни приватности и ограничения.

        Для публикации в профиль это обязательный шаг: privacy_level можно
        брать только из того, что вернул этот запрос.
        """
        with self._client() as client:
            response = client.post(
                f"{API}/post/publish/creator_info/query/", headers=self._headers()
            )
        payload = _json(response)
        if response.status_code != 200:
            raise PublishError(_translate(payload, response.status_code))
        return payload.get("data") or {}

    # --- публикация -----------------------------------------------------

    def publish(
        self,
        request: PublishRequest,
        *,
        on_progress: ProgressCallback | None = None,
        on_log: Callable[[str], None] | None = None,
    ) -> PublishResult:
        say = on_log or (lambda _message: None)
        step = on_progress or (lambda _fraction, _message: None)

        path = Path(request.video_path)
        size = path.stat().st_size
        if size <= 0:
            raise PublishError("файл ролика пуст")
        if size > MAX_FILE:
            raise PublishError(f"TikTok принимает файлы до 4 ГБ, а этот {size / 2**30:.1f} ГБ")

        extra = request.extra or {}
        direct = str(extra.get("mode") or "draft").lower() == "direct"
        # У TikTok одно текстовое поле. Подпись собирает стадия публикации по
        # шаблону из пресета и кладёт в description; title — короткий заголовок
        # и годится только как запасной вариант, если шаблон дал пустоту.
        caption = truncate(
            merge_hashtags(request.description or request.title or "", request.hashtags),
            TITLE_LIMIT,
        )

        if direct:
            body = self._direct_body(extra, caption, size)
            endpoint = f"{API}/post/publish/video/init/"
            say("публикую сразу в профиль")
        else:
            body = {"source_info": _source_info(size)}
            endpoint = f"{API}/post/publish/inbox/video/init/"
            say("отправляю черновик во «Входящие» TikTok")

        with self._client() as client:
            response = client.post(endpoint, headers=self._headers(), json=body)
        payload = _json(response)
        if response.status_code != 200:
            raise PublishError(_translate(payload, response.status_code))

        data = payload.get("data") or {}
        publish_id = data.get("publish_id")
        upload_url = data.get("upload_url")
        if not publish_id or not upload_url:
            raise PublishError(f"TikTok не выдал ссылку для загрузки: {_error_text(payload)}")

        step(0.05, "загружаю файл")
        self._upload(path, size, upload_url, body["source_info"], on_progress=step, say=say)

        step(0.9, "жду обработки")
        status = self._await_publish(publish_id, say=say)

        post_id = status.get("publicaly_available_post_id") or []
        username = str((self.meta or {}).get("username") or "").lstrip("@")
        url = ""
        if post_id and username:
            url = f"https://www.tiktok.com/@{username}/video/{post_id[0]}"

        if not direct:
            say("готово: ролик лежит во «Входящих» TikTok, опубликуй его в приложении")
        return PublishResult(remote_id=str(post_id[0]) if post_id else publish_id, url=url, raw=status)

    def _direct_body(self, extra: dict[str, Any], caption: str, size: int) -> dict[str, Any]:
        info = self._creator_info()
        allowed = info.get("privacy_level_options") or []
        wanted = str(extra.get("privacy") or "").strip().upper()
        if wanted and wanted not in allowed:
            raise PublishError(
                f"уровень приватности {wanted} этому аккаунту недоступен. "
                f"Доступны: {', '.join(allowed) or 'ни одного'}"
            )
        if not wanted:
            # Без явного выбора берём самый закрытый из доступных: промахнуться
            # в сторону «слишком приватно» безопаснее, чем наоборот.
            wanted = "SELF_ONLY" if "SELF_ONLY" in allowed else (allowed[0] if allowed else "SELF_ONLY")

        post_info: dict[str, Any] = {
            "title": caption,
            "privacy_level": wanted,
            "disable_comment": bool(extra.get("disable_comment", False)),
            "disable_duet": bool(extra.get("disable_duet", False)),
            "disable_stitch": bool(extra.get("disable_stitch", False)),
        }
        # Автор мог запретить это в настройках профиля — тогда наше «разрешить»
        # площадка не примет, и запрос упадёт целиком.
        if info.get("comment_disabled"):
            post_info["disable_comment"] = True
        if info.get("duet_disabled"):
            post_info["disable_duet"] = True
        if info.get("stitch_disabled"):
            post_info["disable_stitch"] = True

        cover_ms = extra.get("cover_ms")
        if cover_ms is not None:
            post_info["video_cover_timestamp_ms"] = int(cover_ms)

        return {"post_info": post_info, "source_info": _source_info(size)}

    def _upload(
        self,
        path: Path,
        size: int,
        upload_url: str,
        source: dict[str, Any],
        *,
        on_progress: ProgressCallback,
        say: Callable[[str], None],
    ) -> None:
        chunk_size = int(source["chunk_size"])
        total = int(source["total_chunk_count"])

        with self._client() as client, path.open("rb") as handle:
            sent = 0
            for index in range(total):
                first = index * chunk_size
                # Последний кусок забирает остаток: у TikTok количество кусков
                # считается делением нацело, и «хвост» уезжает вместе с ним.
                last = size - 1 if index == total - 1 else first + chunk_size - 1
                block = handle.read(last - first + 1)
                response = client.put(
                    upload_url,
                    content=block,
                    headers={
                        "Content-Type": "video/mp4",
                        "Content-Length": str(len(block)),
                        "Content-Range": f"bytes {first}-{last}/{size}",
                    },
                )
                if response.status_code not in (200, 201, 206):
                    raise PublishError(
                        f"кусок {index + 1} из {total} не залился: "
                        f"{response.status_code} {response.text[:200]}",
                        retryable=response.status_code >= 500,
                    )
                sent += len(block)
                on_progress(0.05 + 0.8 * sent / size, f"загрузка {sent * 100 // size}%")
        say(f"файл загружен, {size / 2**20:.1f} МБ")

    def _await_publish(self, publish_id: str, *, say: Callable[[str], None]) -> dict[str, Any]:
        deadline = time.time() + STATUS_TIMEOUT
        seen = ""
        while time.time() < deadline:
            with self._client() as client:
                response = client.post(
                    f"{API}/post/publish/status/fetch/",
                    headers=self._headers(),
                    json={"publish_id": publish_id},
                )
            payload = _json(response)
            if response.status_code != 200:
                raise PublishError(_translate(payload, response.status_code))

            data = payload.get("data") or {}
            status = str(data.get("status") or "")
            if status != seen:
                seen = status
                say(f"TikTok: {status.lower().replace('_', ' ')}")
            if status in TERMINAL_OK:
                return data
            if status == "FAILED":
                reason = data.get("fail_reason") or _error_text(payload)
                raise PublishError(f"TikTok отклонил ролик: {reason}")
            time.sleep(STATUS_INTERVAL)

        raise PublishError(
            "TikTok не сообщил результат за десять минут. Ролик мог и загрузиться — "
            "проверь приложение, прежде чем отправлять заново",
            retryable=False,
        )


def _source_info(size: int) -> dict[str, Any]:
    """Разбивка файла на куски по правилам площадки."""
    if size < MIN_CHUNK:
        # Мелкий файл кусками резать нельзя: минимум 5 МБ на кусок.
        return {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": size,
                "total_chunk_count": 1}

    chunk = max(MIN_CHUNK, -(-size // MAX_CHUNKS))  # чтобы кусков было не больше тысячи
    chunk = min(chunk, MAX_CHUNK)
    return {
        "source": "FILE_UPLOAD",
        "video_size": size,
        "chunk_size": chunk,
        # Деление нацело — остаток уедет вместе с последним куском.
        "total_chunk_count": max(1, size // chunk),
    }


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        return response.json()
    except ValueError:
        return {"error": {"message": response.text[:300]}}


def _error_text(payload: dict[str, Any]) -> str:
    error = payload.get("error") or {}
    code = error.get("code") or ""
    message = error.get("message") or ""
    return f"{code} {message}".strip() or "ответ без объяснения"


def _translate(payload: dict[str, Any], status_code: int) -> str:
    """Переводит ответ площадки в понятную строку.

    Сообщения TikTok — коды вроде unaudited_client_can_only_post_to_private_accounts.
    Само по себе это ничего не объясняет тому, кто открыл панель.
    """
    error = payload.get("error") or {}
    code = str(error.get("code") or "")

    if code == "unaudited_client_can_only_post_to_private_accounts":
        return (
            "Приложение TikTok не прошло аудит, поэтому публиковать в профиль оно "
            "может только скрыто — видно будет одному автору. Переключи режим на "
            "«черновик»: ролик придёт во «Входящие», и его останется опубликовать "
            "одним касанием. Либо подай приложение на аудит в TikTok for Developers."
        )
    if code in ("access_token_invalid", "scope_not_authorized", "scope_permission_missed"):
        return (
            "TikTok не принял доступ: токен устарел или у приложения нет нужного "
            "разрешения (video.upload для черновиков, video.publish для публикации "
            "в профиль). Подключи аккаунт заново на странице «Аккаунты»."
        )
    if code == "rate_limit_exceeded":
        return "TikTok ограничил частоту публикаций. Подожди и попробуй снова."
    if code == "spam_risk_too_many_posts":
        return "TikTok считает, что публикаций с аккаунта слишком много за сутки."
    if code == "spam_risk_user_banned_from_posting":
        return "TikTok запретил этому аккаунту публиковать. Разбираться нужно в приложении."
    if code == "url_ownership_unverified":
        return "Домен ссылки не подтверждён в настройках приложения TikTok."
    if code == "privacy_level_option_mismatch":
        return "Выбранный уровень приватности этому аккаунту недоступен."
    if status_code == 401:
        return "TikTok не принял токен. Подключи аккаунт заново."

    return f"TikTok отказал: {_error_text(payload)}"
