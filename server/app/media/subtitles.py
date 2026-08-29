"""Генерация ASS-субтитров из пословной разметки транскрипта."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..utils.text import normalize

ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Main,{fontname},{fontsize},{primary},{primary},{outline_colour},&H80000000,-1,0,0,0,100,100,0,0,1,{outline},1,{alignment},60,60,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

_ALIGNMENT = {"bottom": 2, "center": 5, "top": 8}


def _timestamp(seconds: float) -> str:
    seconds = max(0.0, seconds)
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    return f"{hours}:{minutes:02d}:{secs:05.2f}"


def group_words_into_cues(
    words: list[dict[str, Any]],
    *,
    max_chars: int = 24,
    max_duration: float = 2.5,
    max_gap: float = 0.6,
) -> list[dict[str, Any]]:
    """Складывает слова в реплики: по длине строки, паузе и длительности."""
    cues: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []

    def flush() -> None:
        if not current:
            return
        cues.append(
            {
                "start": current[0]["start"],
                "end": current[-1]["end"],
                "text": " ".join(w["word"].strip() for w in current).strip(),
            }
        )
        current.clear()

    for word in words:
        if current:
            gap = word["start"] - current[-1]["end"]
            length = sum(len(w["word"]) + 1 for w in current) + len(word["word"])
            span = word["end"] - current[0]["start"]
            if gap > max_gap or length > max_chars or span > max_duration:
                flush()
        current.append(word)
    flush()
    return cues


def build_ass(
    words: list[dict[str, Any]],
    output: Path,
    config: dict[str, Any],
    *,
    offset: float = 0.0,
    speed: float = 1.0,
    width: int = 1080,
    height: int = 1920,
    font_name: str = "Arial",
) -> Path | None:
    """Пишет .ass для фрагмента. offset — время начала фрагмента в исходнике."""
    if not words:
        return None

    max_chars = int(config.get("max_chars_per_line", 24))
    cues = group_words_into_cues(words, max_chars=max_chars)
    if not cues:
        return None

    uppercase = bool(config.get("uppercase"))
    header = ASS_HEADER.format(
        width=width,
        height=height,
        fontname=font_name,
        fontsize=int(config.get("font_size", 56)),
        primary=config.get("color", "&H00FFFFFF"),
        outline_colour=config.get("outline_color", "&H00000000"),
        outline=int(config.get("outline", 3)),
        alignment=_ALIGNMENT.get(config.get("position", "bottom"), 2),
        margin_v=int(config.get("margin", 320)),
    )

    lines: list[str] = []
    for cue in cues:
        # Приводим ко времени фрагмента и учитываем изменение скорости
        start = (cue["start"] - offset) / speed
        end = (cue["end"] - offset) / speed
        if end <= 0:
            continue
        text = normalize(cue["text"])
        if uppercase:
            text = text.upper()
        text = text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")
        lines.append(
            f"Dialogue: 0,{_timestamp(max(0.0, start))},{_timestamp(end)},"
            f"Main,,0,0,0,,{text}"
        )

    if not lines:
        return None

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(header + "\n".join(lines) + "\n", encoding="utf-8")
    return output


def slice_words(
    words: list[dict[str, Any]], ranges: list[tuple[float, float]]
) -> list[dict[str, Any]]:
    """Отбирает слова, попавшие в диапазоны, и сдвигает их на склеенную шкалу."""
    result: list[dict[str, Any]] = []
    elapsed = 0.0
    for start, end in ranges:
        for word in words:
            if word["start"] >= start and word["end"] <= end:
                result.append(
                    {
                        "start": elapsed + (word["start"] - start),
                        "end": elapsed + (word["end"] - start),
                        "word": word["word"],
                    }
                )
        elapsed += end - start
    return result
