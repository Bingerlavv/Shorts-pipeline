"""Публикация Reels через Instagram Graph API.

Ключевое ограничение: Instagram не принимает файл напрямую — он сам скачивает
ролик по публичному URL. Поэтому нужен SHORTS_PUBLIC_BASE_URL, доступный из
интернета (реверс-прокси, туннель или объектное хранилище).
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

import httpx

from ...config import settings
from ...utils.text import merge_hashtags, truncate
from .base import ProgressCallback, PublishError, PublishRequest, PublishResult, Publisher

log = logging.getLogger(__name__)

GRAPH_VERSION = "v21.0"
GRAPH_URL = f"https://graph.facebook.com/{GRAPH_VERSION}"
CAPTION_LIMIT = 2200
CONTAINER_POLL_INTERVAL = 5.0
CONTAINER_TIMEOUT = 600.0


class InstagramPublisher(Publisher):
    platform = "instagram"
    needs_public_url = True

    @property
    def _token(self) -> str:
        token = self.credentials.get("access_token", "")
        if not token:
            raise PublishError("нет access_token — подключи аккаунт Instagram заново")
        return token

    @property
    def _ig_user_id(self) -> str:
        user_id = self.credentials.get("ig_user_id") or self.meta.get("ig_user_id")
        if not user_id:
            raise PublishError(
                "не известен ID Instagram-аккаунта. Нужен Business/Creator-аккаунт, "
                "привязанный к странице Facebook"
            )
        return str(user_id)

    def verify(self) -> tuple[bool, str]:
        try:
            response = httpx.get(
                f"{GRAPH_URL}/{self._ig_user_id}",
                params={"fields": "username,account_type", "access_token": self._token},
                timeout=20.0,
            )
        except (PublishError, httpx.HTTPError) as exc:
            return False, str(exc)

        if response.status_code >= 400:
            return False, self._error_text(response)

        data = response.json()
        account_type = data.get("account_type", "")
        if account_type not in ("BUSINESS", "MEDIA_CREATOR"):
            return False, (
                f"тип аккаунта {account_type or 'неизвестен'} — публикация Reels "
                "доступна только для Business и Creator"
            )
        return True, data.get("username", self._ig_user_id)

    def publish(
        self,
        request: PublishRequest,
        *,
        on_progress: ProgressCallback | None = None,
        on_log: Callable[[str], None] | None = None,
    ) -> PublishResult:
        video_url = request.extra.get("video_url")
        if not video_url:
            raise PublishError(
                "не задан публичный URL ролика. Заполни SHORTS_PUBLIC_BASE_URL — "
                "Instagram скачивает файл сам и не принимает загрузку напрямую"
            )

        caption = merge_hashtags(request.description, request.hashtags)

        if on_log:
            on_log(f"создаю контейнер Reels, источник: {video_url}")
        if on_progress:
            on_progress(0.1, "создаю контейнер")

        response = httpx.post(
            f"{GRAPH_URL}/{self._ig_user_id}/media",
            data={
                "media_type": "REELS",
                "video_url": video_url,
                "caption": truncate(caption, CAPTION_LIMIT),
                "share_to_feed": "true" if request.extra.get("share_to_feed", True) else "false",
                "access_token": self._token,
            },
            timeout=60.0,
        )
        if response.status_code >= 400:
            raise self._translate_error(response)

        container_id = response.json().get("id")
        if not container_id:
            raise PublishError(f"Instagram не вернул id контейнера: {response.text[:300]}")

        self._await_container(container_id, on_progress=on_progress, on_log=on_log)

        if on_log:
            on_log("публикую контейнер")
        publish_response = httpx.post(
            f"{GRAPH_URL}/{self._ig_user_id}/media_publish",
            data={"creation_id": container_id, "access_token": self._token},
            timeout=60.0,
        )
        if publish_response.status_code >= 400:
            raise self._translate_error(publish_response)

        media_id = publish_response.json().get("id", "")
        permalink = self._permalink(media_id)
        if on_progress:
            on_progress(1.0, "опубликовано")

        return PublishResult(
            remote_id=media_id,
            url=permalink,
            raw={"container_id": container_id, **publish_response.json()},
        )

    def _await_container(
        self,
        container_id: str,
        *,
        on_progress: ProgressCallback | None,
        on_log: Callable[[str], None] | None,
    ) -> None:
        """Instagram скачивает и перекодирует ролик асинхронно."""
        deadline = time.monotonic() + CONTAINER_TIMEOUT
        while time.monotonic() < deadline:
            response = httpx.get(
                f"{GRAPH_URL}/{container_id}",
                params={"fields": "status_code,status", "access_token": self._token},
                timeout=30.0,
            )
            if response.status_code >= 400:
                raise self._translate_error(response)

            data = response.json()
            status_code = data.get("status_code")
            if status_code == "FINISHED":
                if on_log:
                    on_log("контейнер готов")
                return
            if status_code == "ERROR":
                raise PublishError(
                    f"Instagram не смог обработать ролик: {data.get('status', '')}. "
                    "Проверь, что видео вертикальное, до 90 секунд и доступно по URL"
                )
            if status_code == "EXPIRED":
                raise PublishError("контейнер истёк, не дождавшись публикации")

            elapsed = CONTAINER_TIMEOUT - (deadline - time.monotonic())
            if on_progress:
                on_progress(0.1 + 0.8 * min(1.0, elapsed / CONTAINER_TIMEOUT), "обработка")
            time.sleep(CONTAINER_POLL_INTERVAL)

        raise PublishError(
            "Instagram не обработал ролик за 10 минут", retryable=True
        )

    def _permalink(self, media_id: str) -> str:
        if not media_id:
            return ""
        try:
            response = httpx.get(
                f"{GRAPH_URL}/{media_id}",
                params={"fields": "permalink", "access_token": self._token},
                timeout=20.0,
            )
            if response.status_code < 400:
                return response.json().get("permalink", "")
        except httpx.HTTPError:
            pass
        return ""

    @staticmethod
    def _error_text(response: httpx.Response) -> str:
        try:
            error = response.json().get("error", {})
            return error.get("message") or response.text[:300]
        except ValueError:
            return response.text[:300]

    def _translate_error(self, response: httpx.Response) -> PublishError:
        try:
            error = response.json().get("error", {})
        except ValueError:
            error = {}

        code = error.get("code")
        message = error.get("message", response.text[:300])

        if code == 190:
            return PublishError(
                "токен Instagram недействителен или истёк — подключи аккаунт заново"
            )
        if code == 4 or code == 17 or "limit" in str(message).lower():
            return PublishError(
                "достигнут лимит публикаций Instagram (25 постов за 24 часа)",
                retryable=True,
            )
        if response.status_code >= 500:
            return PublishError(f"Instagram временно недоступен ({response.status_code})",
                                retryable=True)
        return PublishError(f"Instagram: {message}")


def exchange_long_lived_token(short_lived_token: str) -> dict[str, Any]:
    """Меняет короткоживущий токен на 60-дневный."""
    if not settings.instagram_app_id or not settings.instagram_app_secret:
        raise PublishError("не заданы INSTAGRAM_APP_ID и INSTAGRAM_APP_SECRET")

    response = httpx.get(
        f"{GRAPH_URL}/oauth/access_token",
        params={
            "grant_type": "fb_exchange_token",
            "client_id": settings.instagram_app_id,
            "client_secret": settings.instagram_app_secret,
            "fb_exchange_token": short_lived_token,
        },
        timeout=30.0,
    )
    if response.status_code >= 400:
        raise PublishError(f"обмен токена не удался: {InstagramPublisher._error_text(response)}")
    return response.json()


def discover_instagram_accounts(access_token: str) -> list[dict[str, Any]]:
    """Находит Instagram-аккаунты, привязанные к страницам Facebook."""
    response = httpx.get(
        f"{GRAPH_URL}/me/accounts",
        params={
            "fields": "name,access_token,instagram_business_account{id,username,profile_picture_url}",
            "access_token": access_token,
        },
        timeout=30.0,
    )
    if response.status_code >= 400:
        raise PublishError(
            f"не удалось получить список страниц: {InstagramPublisher._error_text(response)}"
        )

    accounts: list[dict[str, Any]] = []
    for page in response.json().get("data", []):
        ig = page.get("instagram_business_account")
        if not ig:
            continue
        accounts.append(
            {
                "page_name": page.get("name", ""),
                "page_access_token": page.get("access_token", ""),
                "ig_user_id": ig.get("id"),
                "username": ig.get("username", ""),
                "avatar": ig.get("profile_picture_url", ""),
            }
        )
    return accounts
