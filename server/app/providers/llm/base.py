"""Интерфейс LLM-провайдера.

Всё, что нужно конвейеру, — получить структурированный JSON по схеме.
Провайдеры отличаются способом принуждения к схеме, но не контрактом.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class LLMError(RuntimeError):
    pass


class LLMProvider(ABC):
    name: str = "base"

    def __init__(self, model: str = "") -> None:
        self.model = model

    @abstractmethod
    def generate_json(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any],
        schema_name: str = "result",
        max_tokens: int = 16000,
        cache_system: bool = True,
    ) -> dict[str, Any]:
        """Возвращает объект, валидный по schema. Бросает LLMError при неудаче."""

    @abstractmethod
    def is_available(self) -> tuple[bool, str]:
        """(доступен, причина недоступности)."""

    def describe(self) -> dict[str, Any]:
        available, reason = self.is_available()
        return {
            "name": self.name,
            "model": self.model,
            "available": available,
            "reason": reason,
        }
