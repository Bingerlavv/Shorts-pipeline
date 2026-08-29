"""Claude API. Структурированный вывод через output_config.format."""

from __future__ import annotations

import json
import logging
from typing import Any

from ...config import settings
from .base import LLMError, LLMProvider

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-5"


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, model: str = "") -> None:
        super().__init__(model or DEFAULT_MODEL)

    def is_available(self) -> tuple[bool, str]:
        if not settings.anthropic_api_key:
            return False, (
                "ANTHROPIC_API_KEY не задан. Впиши ключ в файл .env в корне проекта "
                "и перезапусти воркер, либо переключись на другого провайдера "
                "в SHORTS_LLM_PROVIDER"
            )
        try:
            import anthropic  # noqa: F401
        except ImportError as exc:
            return False, f"пакет anthropic не установлен ({exc})"
        return True, ""

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
        available, reason = self.is_available()
        if not available:
            raise LLMError(reason)

        import anthropic

        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

        # Системный промпт стабилен между кусками транскрипта — кэшируем его,
        # переменная часть (сам транскрипт) идёт после точки кэширования.
        system_blocks: list[dict[str, Any]] = [{"type": "text", "text": system}]
        if cache_system:
            system_blocks[-1]["cache_control"] = {"type": "ephemeral"}

        try:
            with client.messages.stream(
                model=self.model,
                max_tokens=max_tokens,
                system=system_blocks,
                thinking={"type": "adaptive"},
                output_config={
                    "effort": "high",
                    "format": {"type": "json_schema", "schema": schema},
                },
                messages=[{"role": "user", "content": user}],
            ) as stream:
                response = stream.get_final_message()
        except anthropic.APIStatusError as exc:
            raise LLMError(f"Claude API вернул {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError(f"нет связи с Claude API: {exc}") from exc

        if response.stop_reason == "refusal":
            detail = getattr(response.stop_details, "explanation", "") or ""
            raise LLMError(f"модель отказалась обрабатывать запрос. {detail}".strip())
        if response.stop_reason == "max_tokens":
            raise LLMError(
                "ответ обрезан лимитом max_tokens — уменьши chunk_minutes "
                "или target_count в настройках анализа"
            )

        text = next((b.text for b in response.content if b.type == "text"), "")
        if not text:
            raise LLMError("Claude вернул пустой ответ")

        usage = response.usage
        log.info(
            "claude %s: вход %s, кэш чтение/запись %s/%s, выход %s",
            self.model,
            usage.input_tokens,
            usage.cache_read_input_tokens,
            usage.cache_creation_input_tokens,
            usage.output_tokens,
        )

        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError(f"не удалось разобрать JSON от Claude: {exc}") from exc
