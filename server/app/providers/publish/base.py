"""Интерфейс площадки публикации."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


class PublishError(RuntimeError):
    """Ошибка публикации.

    retryable=True означает, что имеет смысл повторить позже (квота, сеть).
    """

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass
class PublishRequest:
    video_path: Path
    title: str
    description: str
    hashtags: list[str] = field(default_factory=list)
    privacy: str = "private"
    tags: list[str] = field(default_factory=list)
    thumbnail_path: Path | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class PublishResult:
    remote_id: str
    url: str
    raw: dict[str, Any] = field(default_factory=dict)


ProgressCallback = Callable[[float, str], None]


class Publisher(ABC):
    platform: str = "base"
    # True — площадка не принимает файл, а скачивает его сама по ссылке,
    # значит серверу нужен адрес, доступный из интернета.
    needs_public_url: bool = False

    def __init__(self, credentials: dict[str, Any], meta: dict[str, Any] | None = None) -> None:
        self.credentials = credentials
        self.meta = meta or {}

    @abstractmethod
    def publish(
        self,
        request: PublishRequest,
        *,
        on_progress: ProgressCallback | None = None,
        on_log: Callable[[str], None] | None = None,
    ) -> PublishResult: ...

    @abstractmethod
    def verify(self) -> tuple[bool, str]:
        """Проверяет, что токены живы. (ок, сообщение)."""

    def refreshed_credentials(self) -> dict[str, Any] | None:
        """Возвращает обновлённые учётные данные, если они изменились."""
        return None
