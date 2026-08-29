"""Фабрика провайдеров транскрипции с автоматическим переходом на облако."""

from __future__ import annotations

import logging

from ...config import settings
from .base import (
    STTError,
    STTProvider,
    TranscriptionResult,
    TranscriptSegment,
    TranscriptWord,
)
from .cloud import AITunnelWhisperProvider, DeepgramProvider, OpenAIWhisperProvider
from .local_whisper import LocalWhisperProvider

log = logging.getLogger(__name__)

_BUILDERS = {
    "local": LocalWhisperProvider,
    "openai": OpenAIWhisperProvider,
    "deepgram": DeepgramProvider,
    "aitunnel": AITunnelWhisperProvider,
}


def build_provider(name: str, model: str = "") -> STTProvider:
    builder = _BUILDERS.get(name)
    if builder is None:
        raise STTError(f"неизвестный провайдер транскрипции: {name!r}")
    if name == "local":
        return LocalWhisperProvider(model_size=model or None)
    return builder(model=model) if model else builder()


def resolve_chain(requested: str, model: str = "") -> list[STTProvider]:
    """Порядок попыток. 'auto' = локально, затем облачный fallback из настроек."""
    if requested and requested != "auto":
        return [build_provider(requested, model)]

    chain: list[STTProvider] = [LocalWhisperProvider(model_size=model or None)]
    fallback = (settings.stt_fallback or "none").lower()
    if fallback in _BUILDERS and fallback != "local":
        chain.append(build_provider(fallback))
    return chain


def transcribe_with_fallback(
    audio_path,
    *,
    requested: str = "auto",
    model: str = "",
    language: str = "",
    word_timestamps: bool = True,
    options: dict | None = None,
    on_progress=None,
    on_notice=None,
) -> TranscriptionResult:
    """Пробует провайдеров по очереди, пока один не отдаст результат."""
    chain = resolve_chain(requested, model)
    errors: list[str] = []

    for provider in chain:
        available, reason = provider.is_available()
        if not available:
            errors.append(f"{provider.name}: {reason}")
            if on_notice:
                on_notice(f"провайдер {provider.name} недоступен — {reason}")
            continue
        try:
            if on_notice:
                on_notice(f"транскрибирую через {provider.name}")
            return provider.transcribe(
                audio_path,
                language=language or None,
                word_timestamps=word_timestamps,
                options=options,
                on_progress=on_progress,
            )
        except Exception as exc:  # noqa: BLE001 — пробуем следующего в цепочке
            log.warning("провайдер %s не справился: %s", provider.name, exc)
            errors.append(f"{provider.name}: {exc}")
            if on_notice:
                on_notice(f"{provider.name} не справился: {exc}")

    raise STTError("ни один провайдер транскрипции не сработал:\n" + "\n".join(errors))


def provider_status() -> list[dict]:
    """Для страницы настроек: кто доступен, а кто нет и почему."""
    statuses = []
    for name in _BUILDERS:
        provider = build_provider(name)
        available, reason = provider.is_available()
        statuses.append({"name": name, "available": available, "reason": reason})
    return statuses


__all__ = [
    "AITunnelWhisperProvider",
    "DeepgramProvider",
    "LocalWhisperProvider",
    "OpenAIWhisperProvider",
    "STTError",
    "STTProvider",
    "TranscriptSegment",
    "TranscriptWord",
    "TranscriptionResult",
    "build_provider",
    "provider_status",
    "resolve_chain",
    "transcribe_with_fallback",
]
