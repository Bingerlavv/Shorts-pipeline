"""Фабрика LLM-провайдеров."""

from __future__ import annotations

from ...config import settings
from .anthropic_provider import AnthropicProvider
from .base import LLMError, LLMProvider
from .ollama_provider import OllamaProvider
from .openai_provider import AITunnelProvider, OpenAIProvider

_BUILDERS: dict[str, type[LLMProvider]] = {
    "anthropic": AnthropicProvider,
    "openai": OpenAIProvider,
    "ollama": OllamaProvider,
    "aitunnel": AITunnelProvider,
}


def build_provider(name: str = "", model: str = "") -> LLMProvider:
    configured = (settings.llm_provider or "anthropic").lower()
    key = (name or configured).lower()
    builder = _BUILDERS.get(key)
    if builder is None:
        raise LLMError(
            f"неизвестный LLM-провайдер: {key!r}. Доступны: {', '.join(_BUILDERS)}"
        )
    # SHORTS_LLM_MODEL относится к выбранному провайдеру, а не ко всем сразу:
    # иначе имя модели Ollama утекало в Anthropic и наоборот, а на странице
    # диагностики провайдеры показывали чужие модели.
    if not model and key == configured:
        model = settings.llm_model
    return builder(model=model)


def provider_status() -> list[dict]:
    """Для страницы настроек панели."""
    rows = []
    for name in _BUILDERS:
        provider = build_provider(name)
        row = provider.describe()
        row["selected"] = name == (settings.llm_provider or "anthropic").lower()
        # Для Ollama полезно видеть, что вообще скачано на этой машине.
        if hasattr(provider, "installed_models"):
            row["installed"] = provider.installed_models()
        rows.append(row)
    return rows


__all__ = [
    "AITunnelProvider",
    "AnthropicProvider",
    "LLMError",
    "LLMProvider",
    "OllamaProvider",
    "OpenAIProvider",
    "build_provider",
    "provider_status",
]
