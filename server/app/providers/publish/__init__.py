"""Фабрика паблишеров."""

from __future__ import annotations

from typing import Any

from ...models import Account
from ...utils.crypto import decrypt_json
from .base import (
    PublishError,
    PublishRequest,
    PublishResult,
    Publisher,
)
from .instagram import InstagramPublisher
from .instagram_login import InstagramLoginPublisher
from .tiktok import TikTokPublisher
from .youtube import YouTubePublisher

_PUBLISHERS: dict[str, type[Publisher]] = {
    "youtube": YouTubePublisher,
    "instagram": InstagramPublisher,
    "tiktok": TikTokPublisher,
}

PLATFORMS = tuple(_PUBLISHERS)


def _instagram_builder(
    credentials: dict[str, Any], meta: dict[str, Any] | None
) -> type[Publisher]:
    """У Instagram два способа входа, площадка одна.

    graph — официальный API (Business-аккаунт + публичный URL),
    login — вход логином и паролем как из мобильного приложения.
    """
    auth = (meta or {}).get("auth")
    if not auth:
        auth = "login" if credentials.get("password") or credentials.get("session") else "graph"
    return InstagramLoginPublisher if auth == "login" else InstagramPublisher


def build_publisher(platform: str, credentials: dict[str, Any],
                    meta: dict[str, Any] | None = None) -> Publisher:
    if platform not in _PUBLISHERS:
        raise PublishError(
            f"неизвестная площадка: {platform!r}. Доступны: {', '.join(_PUBLISHERS)}"
        )
    builder = (
        _instagram_builder(credentials, meta)
        if platform == "instagram"
        else _PUBLISHERS[platform]
    )
    return builder(credentials, meta)


def publisher_for_account(account: Account) -> Publisher:
    return build_publisher(
        account.platform, decrypt_json(account.credentials_enc), account.meta
    )


__all__ = [
    "PLATFORMS",
    "InstagramLoginPublisher",
    "InstagramPublisher",
    "TikTokPublisher",
    "PublishError",
    "PublishRequest",
    "PublishResult",
    "Publisher",
    "YouTubePublisher",
    "build_publisher",
    "publisher_for_account",
]
