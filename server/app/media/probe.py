"""Чтение параметров медиафайла через ffprobe."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import settings


class MediaError(RuntimeError):
    pass


@dataclass
class MediaInfo:
    path: Path
    duration: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    has_video: bool = False
    has_audio: bool = False
    video_codec: str = ""
    audio_codec: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def aspect(self) -> float:
        return (self.width / self.height) if self.height else 0.0

    @property
    def is_vertical(self) -> bool:
        return self.height > self.width


def _parse_fraction(value: str | None) -> float:
    if not value:
        return 0.0
    if "/" in value:
        num, _, den = value.partition("/")
        try:
            denominator = float(den)
            return float(num) / denominator if denominator else 0.0
        except ValueError:
            return 0.0
    try:
        return float(value)
    except ValueError:
        return 0.0


def probe_media(path: str | Path) -> MediaInfo:
    path = Path(path)
    if not path.exists():
        raise MediaError(f"файл не найден: {path}")

    command = [
        settings.resolve_ffprobe(),
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        result = subprocess.run(command, capture_output=True, check=True, text=False)
    except FileNotFoundError as exc:
        raise MediaError(
            "ffprobe не найден. Запусти scripts/bootstrap.ps1 или задай SHORTS_FFPROBE_PATH"
        ) from exc
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or b"").decode("utf-8", "replace")
        raise MediaError(f"ffprobe не смог прочитать {path.name}: {stderr[:500]}") from exc

    data = json.loads(result.stdout.decode("utf-8", "replace"))
    info = MediaInfo(path=path, raw=data)
    info.duration = float(data.get("format", {}).get("duration") or 0.0)

    for stream in data.get("streams", []):
        kind = stream.get("codec_type")
        if kind == "video" and not info.has_video:
            info.has_video = True
            info.video_codec = stream.get("codec_name", "")
            info.width = int(stream.get("width") or 0)
            info.height = int(stream.get("height") or 0)
            info.fps = _parse_fraction(
                stream.get("avg_frame_rate") or stream.get("r_frame_rate")
            )
            if not info.duration:
                info.duration = float(stream.get("duration") or 0.0)
        elif kind == "audio" and not info.has_audio:
            info.has_audio = True
            info.audio_codec = stream.get("codec_name", "")

    return info
