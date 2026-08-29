"""Нарезка исходника: один диапазон или склейка нескольких.

Границы подрезаются по паузам между словами — иначе фрагмент начинается
с обрубленного слога, что сразу выдаёт автонарезку.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .probe import MediaError
from .runner import ProgressCallback, run_ffmpeg

log = logging.getLogger(__name__)


def snap_to_word_boundaries(
    start: float,
    end: float,
    words: list[dict[str, Any]],
    *,
    max_shift: float = 0.6,
) -> tuple[float, float]:
    """Двигает границы к ближайшей паузе, но не дальше max_shift секунд."""
    if not words:
        return start, end

    # Начало: ищем слово, которое начинается сразу после start
    candidates = [w["start"] for w in words if abs(w["start"] - start) <= max_shift]
    new_start = min(candidates, key=lambda t: abs(t - start)) if candidates else start

    candidates = [w["end"] for w in words if abs(w["end"] - end) <= max_shift]
    new_end = min(candidates, key=lambda t: abs(t - end)) if candidates else end

    if new_end - new_start < 1.0:
        return start, end
    return new_start, new_end


def cut_ranges(
    source: Path,
    output: Path,
    ranges: list[tuple[float, float]],
    config: dict[str, Any],
    *,
    total_duration: float = 0.0,
    on_progress: ProgressCallback | None = None,
) -> Path:
    """Вырезает диапазоны и склеивает их в один файл.

    Перекодируем всегда: нарезка по ключевым кадрам без перекодирования
    промахивается по границам на секунду и больше.
    """
    if not ranges:
        raise MediaError("не задан ни один диапазон для нарезки")

    output.parent.mkdir(parents=True, exist_ok=True)
    padding_before = float(config.get("padding_before", 0.0))
    padding_after = float(config.get("padding_after", 0.0))

    padded: list[tuple[float, float]] = []
    for start, end in ranges:
        adjusted_start = max(0.0, start - padding_before)
        adjusted_end = end + padding_after
        if total_duration:
            adjusted_end = min(adjusted_end, total_duration)
        if adjusted_end > adjusted_start:
            padded.append((adjusted_start, adjusted_end))

    if not padded:
        raise MediaError("после применения отступов не осталось ни одного диапазона")

    expected = sum(end - start for start, end in padded)

    if len(padded) == 1:
        start, end = padded[0]
        run_ffmpeg(
            [
                "-ss", f"{start:.3f}",
                "-to", f"{end:.3f}",
                "-i", str(source),
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "16",
                "-c:a", "aac", "-b:a", "256k",
                "-avoid_negative_ts", "make_zero",
                str(output),
            ],
            total_duration=expected,
            on_progress=on_progress,
            description="нарезка фрагмента",
        )
        return output

    # Несколько кусков: собираем через concat-фильтр, с кроссфейдом на стыках
    crossfade = float(config.get("crossfade", 0.0))
    args: list[str] = []
    for start, end in padded:
        args += ["-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", str(source)]

    graph_parts: list[str] = []
    for index in range(len(padded)):
        graph_parts.append(f"[{index}:v]setpts=PTS-STARTPTS[v{index}]")
        graph_parts.append(f"[{index}:a]asetpts=PTS-STARTPTS[a{index}]")

    if crossfade > 0.01:
        video_label, audio_label = "v0", "a0"
        offset = padded[0][1] - padded[0][0]
        for index in range(1, len(padded)):
            transition_at = max(0.0, offset - crossfade)
            graph_parts.append(
                f"[{video_label}][v{index}]xfade=transition=fade"
                f":duration={crossfade:.3f}:offset={transition_at:.3f}[vx{index}]"
            )
            graph_parts.append(
                f"[{audio_label}][a{index}]acrossfade=d={crossfade:.3f}[ax{index}]"
            )
            video_label, audio_label = f"vx{index}", f"ax{index}"
            offset += (padded[index][1] - padded[index][0]) - crossfade
        graph_parts.append(f"[{video_label}]null[vout]")
        graph_parts.append(f"[{audio_label}]anull[aout]")
        expected = offset
    else:
        streams = "".join(f"[v{i}][a{i}]" for i in range(len(padded)))
        graph_parts.append(f"{streams}concat=n={len(padded)}:v=1:a=1[vout][aout]")

    args += [
        "-filter_complex", ";".join(graph_parts),
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "16",
        "-c:a", "aac", "-b:a", "256k",
        str(output),
    ]

    run_ffmpeg(
        args,
        total_duration=expected,
        on_progress=on_progress,
        description=f"склейка {len(padded)} кусков",
    )
    return output
