"""Интерфейс провайдера транскрипции."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


@dataclass
class TranscriptWord:
    start: float
    end: float
    word: str

    def to_dict(self) -> dict[str, Any]:
        return {"start": round(self.start, 3), "end": round(self.end, 3), "word": self.word}


@dataclass
class TranscriptSegment:
    start: float
    end: float
    text: str

    def to_dict(self) -> dict[str, Any]:
        return {"start": round(self.start, 3), "end": round(self.end, 3), "text": self.text}


@dataclass
class TranscriptionResult:
    language: str
    provider: str
    model: str
    segments: list[TranscriptSegment] = field(default_factory=list)
    words: list[TranscriptWord] = field(default_factory=list)

    @property
    def full_text(self) -> str:
        return " ".join(s.text.strip() for s in self.segments if s.text.strip())

    @property
    def duration(self) -> float:
        return self.segments[-1].end if self.segments else 0.0


ProgressCallback = Callable[[float, str], None]


class STTProvider(ABC):
    name: str = "base"

    @abstractmethod
    def transcribe(
        self,
        audio_path: Path,
        *,
        language: str | None = None,
        word_timestamps: bool = True,
        options: dict[str, Any] | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> TranscriptionResult: ...

    @abstractmethod
    def is_available(self) -> tuple[bool, str]:
        """(доступен, причина недоступности)."""


class STTError(RuntimeError):
    pass
