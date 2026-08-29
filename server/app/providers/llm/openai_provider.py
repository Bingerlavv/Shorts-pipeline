"""OpenAI и совместимые шлюзы. Структурированный вывод через response_format."""

from __future__ import annotations

import json
import logging
from typing import Any

from ...config import settings
from .base import LLMError, LLMProvider

log = logging.getLogger(__name__)

DEFAULT_MODEL = "gpt-4o"

# Признаки того, что шлюз не понимает строгую схему. У совместимых сервисов
# json_schema поддержан не всеми моделями, и отказ приходит обычным текстом.
SCHEMA_UNSUPPORTED = (
    "json_schema",
    "response_format",
    "strict",
    "not supported",
    "unsupported",
    "invalid_request",
)


class OpenAIProvider(LLMProvider):
    name = "openai"
    key_name = "OPENAI_API_KEY"

    def __init__(self, model: str = "") -> None:
        super().__init__(model or DEFAULT_MODEL)

    # Переопределяется совместимыми шлюзами: адрес и ключ у них свои,
    # а протокол тот же.
    @property
    def api_key(self) -> str:
        return settings.openai_api_key

    @property
    def base_url(self) -> str | None:
        return None

    def is_available(self) -> tuple[bool, str]:
        if not self.api_key:
            return False, (
                f"{self.key_name} не задан. Впиши ключ в файл .env в корне проекта "
                "и перезапусти воркер, либо переключись на другого провайдера "
                "в SHORTS_LLM_PROVIDER"
            )
        try:
            import openai  # noqa: F401
        except ImportError as exc:
            return False, f"пакет openai не установлен ({exc})"
        return True, ""

    def _client(self):  # noqa: ANN202
        from openai import OpenAI

        if self.base_url:
            return OpenAI(api_key=self.api_key, base_url=self.base_url)
        return OpenAI(api_key=self.api_key)

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

        client = self._client()
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

        def ask(response_format: dict[str, Any], extra_system: str = ""):  # noqa: ANN202
            body = messages
            if extra_system:
                body = [{"role": "system", "content": system + extra_system}, messages[1]]
            return client.chat.completions.create(
                model=self.model,
                max_tokens=max_tokens,
                messages=body,
                response_format=response_format,
            )

        strict = {
            "type": "json_schema",
            "json_schema": {"name": schema_name, "schema": schema, "strict": True},
        }
        try:
            response = ask(strict)
        except Exception as exc:  # noqa: BLE001 — SDK бросает разные типы
            text = str(exc).lower()
            if not any(marker in text for marker in SCHEMA_UNSUPPORTED):
                raise LLMError(f"{self.name}: {exc}") from exc
            # Шлюз не умеет строгую схему — просим просто корректный JSON, а саму
            # схему кладём в системный текст. Иначе провайдер вообще неприменим.
            log.info("%s не принял json_schema (%s), пробую json_object", self.name, exc)
            try:
                response = ask(
                    {"type": "json_object"},
                    "\n\nОтвечай строго одним объектом JSON по этой схеме, без пояснений:\n"
                    + json.dumps(schema, ensure_ascii=False),
                )
            except Exception as second:  # noqa: BLE001
                raise LLMError(f"{self.name}: {second}") from second

        choice = response.choices[0]
        if choice.finish_reason == "length":
            raise LLMError("ответ обрезан лимитом токенов — уменьши размер куска транскрипта")

        content = choice.message.content or ""
        if not content:
            raise LLMError(f"{self.name} вернул пустой ответ")

        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            raise LLMError(f"не удалось разобрать JSON от {self.name}: {exc}") from exc


class AITunnelProvider(OpenAIProvider):
    """AITunnel: один ключ на GPT, Claude, DeepSeek и прочих.

    Протокол тот же, отличаются только адрес, ключ и названия моделей —
    поэтому это надстройка над OpenAI-провайдером, а не отдельная реализация.
    Каталог моделей: https://aitunnel.ru/docs/models
    """

    name = "aitunnel"
    key_name = "AITUNNEL_API_KEY"

    def __init__(self, model: str = "") -> None:
        LLMProvider.__init__(self, model or settings.aitunnel_model or DEFAULT_MODEL)

    @property
    def api_key(self) -> str:
        return settings.aitunnel_api_key

    @property
    def base_url(self) -> str | None:
        return settings.aitunnel_base_url.rstrip("/") or None
