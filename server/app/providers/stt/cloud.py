"""Облачные провайдеры транскрипции: OpenAI Whisper и Deepgram."""

from __future__ import annotations

import logging
import math
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import httpx

from ...config import settings
from .base import (
    ProgressCallback,
    STTError,
    STTProvider,
    TranscriptionResult,
    TranscriptSegment,
    TranscriptWord,
)

log = logging.getLogger(__name__)

# OpenAI не принимает файлы больше 25 МБ, поэтому длинные записи режем на куски.
OPENAI_CHUNK_SECONDS = 900
OPENAI_MAX_BYTES = 24 * 1024 * 1024


def _split_audio(audio_path: Path, chunk_seconds: int, work_dir: Path) -> list[tuple[Path, float]]:
    """Режет аудио на куски. Возвращает [(путь, смещение в исходнике)]."""
    from ...media.probe import probe_media

    info = probe_media(audio_path)
    duration = info.duration
    if duration <= chunk_seconds:
        return [(audio_path, 0.0)]

    ffmpeg = settings.resolve_ffmpeg()
    chunks: list[tuple[Path, float]] = []
    for index in range(math.ceil(duration / chunk_seconds)):
        offset = index * chunk_seconds
        out = work_dir / f"chunk_{index:03d}.mp3"
        subprocess.run(
            [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-ss", str(offset), "-t", str(chunk_seconds),
                "-i", str(audio_path),
                "-vn", "-ac", "1", "-ar", "16000", "-b:a", "64k",
                str(out),
            ],
            check=True,
            capture_output=True,
        )
        chunks.append((out, float(offset)))
    return chunks


class OpenAIWhisperProvider(STTProvider):
    name = "openai"
    key_name = "OPENAI_API_KEY"

    def __init__(self, model: str = "whisper-1") -> None:
        self.model = model

    # Совместимые шлюзы переопределяют адрес и ключ — протокол у них тот же.
    @property
    def api_key(self) -> str:
        return settings.openai_api_key

    @property
    def base_url(self) -> str | None:
        return None

    def is_available(self) -> tuple[bool, str]:
        if not self.api_key:
            return False, f"{self.key_name} не задан"
        return True, ""

    def transcribe(
        self,
        audio_path: Path,
        *,
        language: str | None = None,
        word_timestamps: bool = True,
        options: dict[str, Any] | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> TranscriptionResult:
        available, reason = self.is_available()
        if not available:
            raise STTError(reason)

        from openai import OpenAI

        client = (
            OpenAI(api_key=self.api_key, base_url=self.base_url)
            if self.base_url
            else OpenAI(api_key=self.api_key)
        )
        result = TranscriptionResult(
            language=language or "", provider=self.name, model=self.model
        )

        with tempfile.TemporaryDirectory(prefix="shorts_stt_") as tmp:
            chunks = _split_audio(audio_path, OPENAI_CHUNK_SECONDS, Path(tmp))
            for index, (chunk_path, offset) in enumerate(chunks):
                if chunk_path.stat().st_size > OPENAI_MAX_BYTES:
                    raise STTError(
                        f"кусок {chunk_path.name} больше лимита OpenAI в 25 МБ — "
                        "уменьши OPENAI_CHUNK_SECONDS"
                    )
                granularities = ["segment"] + (["word"] if word_timestamps else [])
                with chunk_path.open("rb") as handle:
                    response = client.audio.transcriptions.create(
                        file=handle,
                        model=self.model,
                        language=language or None,
                        response_format="verbose_json",
                        timestamp_granularities=granularities,
                    )

                data = response.model_dump() if hasattr(response, "model_dump") else dict(response)
                result.language = result.language or data.get("language", "")
                for seg in data.get("segments") or []:
                    text = (seg.get("text") or "").strip()
                    if text:
                        result.segments.append(
                            TranscriptSegment(
                                float(seg["start"]) + offset, float(seg["end"]) + offset, text
                            )
                        )
                for word in data.get("words") or []:
                    token = (word.get("word") or "").strip()
                    if token:
                        result.words.append(
                            TranscriptWord(
                                float(word["start"]) + offset, float(word["end"]) + offset, token
                            )
                        )
                if on_progress:
                    on_progress((index + 1) / len(chunks), f"кусок {index + 1} из {len(chunks)}")

        if not result.segments:
            raise STTError("OpenAI вернул пустую транскрипцию")
        return result


class AITunnelWhisperProvider(OpenAIWhisperProvider):
    """Распознавание через AITunnel: тот же /audio/transcriptions."""

    name = "aitunnel"
    key_name = "AITUNNEL_API_KEY"

    def __init__(self, model: str = "") -> None:
        self.model = model or settings.aitunnel_stt_model or "whisper-1"

    @property
    def api_key(self) -> str:
        return settings.aitunnel_api_key

    @property
    def base_url(self) -> str | None:
        return settings.aitunnel_base_url.rstrip("/") or None


class DeepgramProvider(STTProvider):
    name = "deepgram"

    def __init__(self, model: str = "nova-2") -> None:
        self.model = model

    def is_available(self) -> tuple[bool, str]:
        if not settings.deepgram_api_key:
            return False, "DEEPGRAM_API_KEY не задан"
        return True, ""

    def transcribe(
        self,
        audio_path: Path,
        *,
        language: str | None = None,
        word_timestamps: bool = True,
        options: dict[str, Any] | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> TranscriptionResult:
        available, reason = self.is_available()
        if not available:
            raise STTError(reason)

        params: dict[str, Any] = {
            "model": self.model,
            "smart_format": "true",
            "punctuate": "true",
            "utterances": "true",
        }
        if language:
            params["language"] = language
        else:
            params["detect_language"] = "true"

        if on_progress:
            on_progress(0.1, "отправляю аудио в Deepgram")

        with audio_path.open("rb") as handle:
            response = httpx.post(
                "https://api.deepgram.com/v1/listen",
                params=params,
                content=handle.read(),
                headers={
                    "Authorization": f"Token {settings.deepgram_api_key}",
                    "Content-Type": "audio/*",
                },
                timeout=httpx.Timeout(900.0, connect=30.0),
            )
        if response.status_code >= 400:
            raise STTError(f"Deepgram вернул {response.status_code}: {response.text[:500]}")

        data = response.json()
        channel = (data.get("results", {}).get("channels") or [{}])[0]
        alternative = (channel.get("alternatives") or [{}])[0]

        result = TranscriptionResult(
            language=channel.get("detected_language") or language or "",
            provider=self.name,
            model=self.model,
        )
        for utterance in data.get("results", {}).get("utterances") or []:
            text = (utterance.get("transcript") or "").strip()
            if text:
                result.segments.append(
                    TranscriptSegment(float(utterance["start"]), float(utterance["end"]), text)
                )
        for word in alternative.get("words") or []:
            token = (word.get("punctuated_word") or word.get("word") or "").strip()
            if token:
                result.words.append(
                    TranscriptWord(float(word["start"]), float(word["end"]), token)
                )

        if not result.segments and alternative.get("transcript"):
            # utterances выключены — собираем один сегмент из слов
            end = result.words[-1].end if result.words else 0.0
            result.segments.append(TranscriptSegment(0.0, end, alternative["transcript"].strip()))

        if not result.segments:
            raise STTError("Deepgram вернул пустую транскрипцию")
        if on_progress:
            on_progress(1.0, "транскрипция готова")
        return result
