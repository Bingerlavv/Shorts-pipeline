"""Публикация в TikTok через официальный Content Posting API.

Поддерживает два режима:
- «Черновик» (draft) – видео попадает во «Входящие» приложения TikTok,
  требуется ручное подтверждение. Работает без аудита приложения.
- «Прямая публикация» (direct) – видео сразу появляется в профиле.
  Требует, чтобы приложение прошло аудит TikTok (иначе видео видны только автору).
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any, Callable

import httpx

from .base import (
    PublishError,
    PublishRequest,
    PublishResult,
    Publisher,
    ProgressCallback,
    build_caption,
)

log = logging.getLogger(__name__)

# Константы API
API = "https://open.tiktokapis.com/v2"
TOKEN_URL = f"{API}/oauth/token/"

# Требования к загрузке файлов
MIN_CHUNK = 5 * 1024 * 1024      # 5 МБ
MAX_CHUNK = 64 * 1024 * 1024     # 64 МБ
MAX_CHUNKS = 1000
MAX_FILE = 4 * 1024 * 1024 * 1024  # 4 ГБ

# Лимиты текста
TITLE_LIMIT = 2200  # символов UTF-16

# Таймауты ожидания статуса
STATUS_TIMEOUT = 600        # 10 минут
STATUS_INTERVAL = 5.0       # секунд

TERMINAL_OK = {"PUBLISH_COMPLETE", "SEND_TO_USER_INBOX"}


class TikTokPublisher(Publisher):
    """Публикатор для TikTok через официальный API."""

    platform = "tiktok"
    needs_public_url = False

    def __init__(self, credentials: dict[str, Any], meta: dict[str, Any] | None = None) -> None:
        super().__init__(credentials, meta)
        self._refreshed: dict[str, Any] | None = None

        # Читаем ключи приложения из переменных окружения или из credentials
        self.client_key = credentials.get("client_key") or os.getenv("TIKTOK_CLIENT_KEY", "")
        self.client_secret = credentials.get("client_secret") or os.getenv("TIKTOK_CLIENT_SECRET", "")

    # ---------- Основные методы Publisher ----------

    def publish(
        self,
        request: PublishRequest,
        *,
        on_progress: ProgressCallback | None = None,
        on_log: Callable[[str], None] | None = None,
    ) -> PublishResult:
        say = on_log or (lambda msg: log.info(msg))
        step = on_progress or (lambda f, m: None)

        path = request.video_path
        size = path.stat().st_size
        if size <= 0:
            raise PublishError("Файл видео пуст")
        if size > MAX_FILE:
            raise PublishError(f"TikTok принимает файлы до 4 ГБ, а этот {size / 2**30:.1f} ГБ")

        # Определяем режим: черновик или прямая публикация
        extra = request.extra or {}
        mode = extra.get("mode", "draft")  # "draft" или "direct"
        direct = mode == "direct"

        # Формируем подпись (caption) из title, description и hashtags
        caption = build_caption(request, TITLE_LIMIT)

        if direct:
            # Прямая публикация – нужен запрос с post_info
            body = self._direct_body(request, caption, size)
            endpoint = f"{API}/post/publish/video/init/"
            say("Публикую сразу в профиль (direct)")
        else:
            # Черновик – минимальный запрос
            body = {"source_info": self._source_info(size)}
            endpoint = f"{API}/post/publish/inbox/video/init/"
            say("Отправляю черновик во «Входящие» TikTok")

        # Инициируем публикацию
        with self._client() as client:
            response = client.post(endpoint, headers=self._headers(), json=body)
        payload = self._json(response)
        if response.status_code != 200:
            raise PublishError(self._translate_error(payload, response.status_code))

        data = payload.get("data") or {}
        publish_id = data.get("publish_id")
        upload_url = data.get("upload_url")
        if not publish_id or not upload_url:
            raise PublishError(f"TikTok не выдал ссылку для загрузки: {self._error_text(payload)}")

        step(0.05, "Загружаю файл")
        self._upload(path, size, upload_url, body["source_info"], on_progress=step, say=say)

        step(0.9, "Ожидаю обработки")
        status = self._await_publish(publish_id, say=say)

        # Извлекаем ID опубликованного видео
        post_ids = status.get("publicaly_available_post_id") or []
        remote_id = str(post_ids[0]) if post_ids else publish_id

        # Формируем URL
        username = str((self.meta or {}).get("username") or "").lstrip("@")
        url = f"https://www.tiktok.com/@{username}/video/{remote_id}" if username and remote_id else ""

        if not direct:
            say("Готово: ролик лежит во «Входящих» TikTok, опубликуйте его в приложении")

        return PublishResult(remote_id=remote_id, url=url, raw=status)

    def verify(self) -> tuple[bool, str]:
        """Проверяет, что токен доступа жив и можно получить информацию об аккаунте."""
        try:
            info = self._creator_info()
        except PublishError as exc:
            return False, str(exc)
        name = info.get("creator_nickname") or info.get("creator_username") or "аккаунт"
        limit = info.get("max_video_post_duration_sec")
        tail = f", ролики до {limit} с" if limit else ""
        return True, f"TikTok: {name}{tail}"

    # ---------- Внутренние методы ----------

    def _client(self) -> httpx.Client:
        """Создаёт HTTP-клиент с возможностью прокси."""
        proxy = str(self.credentials.get("proxy") or "").strip() or None
        return httpx.Client(timeout=120, trust_env=False, proxy=proxy)

    def _token(self) -> str:
        """Возвращает действующий access_token, при необходимости обновляя его."""
        token = str(self.credentials.get("access_token") or "")
        expires_at = float(self.credentials.get("expires_at") or 0)
        if token and expires_at - time.time() > 120:
            return token

        refresh = str(self.credentials.get("refresh_token") or "")
        if not refresh:
            raise PublishError(
                "У аккаунта TikTok нет refresh_token – подключите его заново "
                "и получите новый refresh_token",
                retryable=False,
            )
        if not self.client_key or not self.client_secret:
            raise PublishError(
                "Не заданы client_key и client_secret – укажите их в credentials "
                "или в переменных окружения TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET",
                retryable=False,
            )

        with self._client() as client:
            response = client.post(
                TOKEN_URL,
                data={
                    "client_key": self.client_key,
                    "client_secret": self.client_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        payload = self._json(response)
        if response.status_code != 200 or not payload.get("access_token"):
            raise PublishError(
                f"TikTok не обновил доступ: {self._error_text(payload)}. "
                "Скорее всего, refresh_token истёк – подключите аккаунт заново",
                retryable=False,
            )

        # Обновляем учётные данные
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

    def _creator_info(self) -> dict[str, Any]:
        """Запрашивает информацию об аккаунте (доступные уровни приватности и т.д.)."""
        with self._client() as client:
            response = client.post(
                f"{API}/post/publish/creator_info/query/", headers=self._headers()
            )
        payload = self._json(response)
        if response.status_code != 200:
            raise PublishError(self._translate_error(payload, response.status_code))
        return payload.get("data") or {}

    def _direct_body(self, request: PublishRequest, caption: str, size: int) -> dict[str, Any]:
        """Формирует тело запроса для прямой публикации."""
        # Получаем допустимые уровни приватности
        info = self._creator_info()
        allowed = info.get("privacy_level_options") or []
        privacy = request.privacy or "private"
        if privacy and privacy not in allowed:
            # Если запрошенный уровень недоступен – берём самый закрытый
            privacy = "SELF_ONLY" if "SELF_ONLY" in allowed else (allowed[0] if allowed else "SELF_ONLY")

        post_info: dict[str, Any] = {
            "title": caption,
            "privacy_level": privacy,
            "disable_comment": bool(request.extra.get("disable_comment", False)),
            "disable_duet": bool(request.extra.get("disable_duet", False)),
            "disable_stitch": bool(request.extra.get("disable_stitch", False)),
        }
        # Если автор запретил комментарии и т.д. в настройках профиля, подстраиваемся
        if info.get("comment_disabled"):
            post_info["disable_comment"] = True
        if info.get("duet_disabled"):
            post_info["disable_duet"] = True
        if info.get("stitch_disabled"):
            post_info["disable_stitch"] = True

        # Обложка (если указана)
        cover_ms = request.extra.get("cover_timestamp_ms")
        if cover_ms is not None:
            post_info["video_cover_timestamp_ms"] = int(cover_ms)

        return {"post_info": post_info, "source_info": self._source_info(size)}

    def _source_info(self, size: int) -> dict[str, Any]:
        """Вычисляет параметры разбивки файла на чанки."""
        if size < MIN_CHUNK:
            return {
                "source": "FILE_UPLOAD",
                "video_size": size,
                "chunk_size": size,
                "total_chunk_count": 1,
            }
        chunk = max(MIN_CHUNK, -(-size // MAX_CHUNKS))  # округление вверх
        chunk = min(chunk, MAX_CHUNK)
        return {
            "source": "FILE_UPLOAD",
            "video_size": size,
            "chunk_size": chunk,
            "total_chunk_count": max(1, size // chunk),
        }

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
        """Загружает видео чанками через PUT-запросы."""
        chunk_size = int(source["chunk_size"])
        total = int(source["total_chunk_count"])

        with self._client() as client, path.open("rb") as handle:
            sent = 0
            for index in range(total):
                first = index * chunk_size
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
                        f"Чанк {index + 1} из {total} не загружен: "
                        f"{response.status_code} {response.text[:200]}",
                        retryable=response.status_code >= 500,
                    )
                sent += len(block)
                on_progress(0.05 + 0.8 * sent / size, f"Загрузка {sent * 100 // size}%")
        say(f"Файл загружен, {size / 2**20:.1f} МБ")

    def _await_publish(self, publish_id: str, *, say: Callable[[str], None]) -> dict[str, Any]:
        """Ожидает завершения обработки видео на стороне TikTok."""
        deadline = time.time() + STATUS_TIMEOUT
        seen = ""
        while time.time() < deadline:
            with self._client() as client:
                response = client.post(
                    f"{API}/post/publish/status/fetch/",
                    headers=self._headers(),
                    json={"publish_id": publish_id},
                )
            payload = self._json(response)
            if response.status_code != 200:
                raise PublishError(self._translate_error(payload, response.status_code))

            data = payload.get("data") or {}
            status = str(data.get("status") or "")
            if status != seen:
                seen = status
                say(f"TikTok: {status.lower().replace('_', ' ')}")
            if status in TERMINAL_OK:
                return data
            if status == "FAILED":
                reason = data.get("fail_reason") or self._error_text(payload)
                raise PublishError(f"TikTok отклонил ролик: {reason}")
            time.sleep(STATUS_INTERVAL)

        raise PublishError(
            "TikTok не сообщил результат за 10 минут. Ролик мог загрузиться – "
            "проверьте приложение, прежде чем отправлять повторно",
            retryable=False,
        )

    def refreshed_credentials(self) -> dict[str, Any] | None:
        """Возвращает обновлённые учётные данные (после обновления токена)."""
        return self._refreshed

    # ---------- Вспомогательные утилиты ----------

    @staticmethod
    def _json(response: httpx.Response) -> dict[str, Any]:
        try:
            return response.json()
        except ValueError:
            return {"error": {"message": response.text[:300]}}

    @staticmethod
    def _error_text(payload: dict[str, Any]) -> str:
        error = payload.get("error") or {}
        code = error.get("code") or ""
        message = error.get("message") or ""
        return f"{code} {message}".strip() or "ответ без объяснения"

    @staticmethod
    def _translate_error(payload: dict[str, Any], status_code: int) -> str:
        """Превращает ответ TikTok в читаемое сообщение об ошибке."""
        error = payload.get("error") or {}
        code = str(error.get("code") or "")

        if code == "unaudited_client_can_only_post_to_private_accounts":
            return (
                "Приложение не прошло аудит TikTok, поэтому публикация в профиль "
                "возможна только с видимостью 'только я'. Используйте режим 'черновик' "
                "или подайте приложение на аудит в TikTok for Developers."
            )
        if code in ("access_token_invalid", "scope_not_authorized", "scope_permission_missed"):
            return (
                "TikTok не принял токен: он устарел или у приложения нет нужного "
                "разрешения (video.upload для черновиков, video.publish для публикации). "
                "Подключите аккаунт заново."
            )
        if code == "rate_limit_exceeded":
            return "Слишком много запросов к TikTok – подождите."
        if code == "spam_risk_too_many_posts":
            return "Слишком много публикаций за сутки – TikTok ограничил."
        if code == "spam_risk_user_banned_from_posting":
            return "Этот аккаунт заблокирован для публикаций – разбирайтесь в приложении."
        if code == "url_ownership_unverified":
            return "Домен ссылки не подтверждён в настройках приложения TikTok."
        if code == "privacy_level_option_mismatch":
            return "Выбранный уровень приватности недоступен для этого аккаунта."
        if status_code == 401:
            return "Ошибка авторизации – подключите аккаунт заново."

        return f"TikTok отказал: {TikTokPublisher._error_text(payload)}"