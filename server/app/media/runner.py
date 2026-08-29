"""Запуск ffmpeg с разбором прогресса."""

from __future__ import annotations

import logging
import re
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from ..config import settings
from .probe import MediaError

log = logging.getLogger(__name__)

_OUT_TIME = re.compile(r"out_time_ms=(\d+)")
_ERROR_HINTS = {
    "No such file or directory": "ffmpeg не нашёл один из входных файлов",
    "Invalid data found": "повреждённый или неподдерживаемый файл",
    "Unknown encoder": "запрошенный кодек недоступен в этой сборке ffmpeg",
    "Cannot load": "не удалось загрузить шрифт или библиотеку фильтра",
    "Unable to parse option value": "ошибка в параметрах фильтра",
}

ProgressCallback = Callable[[float, str], None]


def run_ffmpeg(
    args: Sequence[str],
    *,
    total_duration: float = 0.0,
    on_progress: ProgressCallback | None = None,
    cancel_check: Callable[[], None] | None = None,
    description: str = "ffmpeg",
    timeout: float = 0.0,
) -> None:
    """Выполняет ffmpeg. args — без имени бинарника.

    Прогресс читается из `-progress pipe:1`, stderr собирается для диагностики.
    timeout в секундах (0 — без ограничения) страхует от зависания: любой
    подвисший вызов убивается, а не держит воркер бесконечно.
    """
    command = [
        settings.resolve_ffmpeg(),
        "-hide_banner",
        "-loglevel", "error",
        "-nostdin",
        "-y",
        *args,
        "-progress", "pipe:1",
        "-stats_period", "0.5",
    ]
    log.debug("%s: %s", description, " ".join(command))

    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except FileNotFoundError as exc:
        raise MediaError(
            "ffmpeg не найден. Запусти scripts/bootstrap.ps1 или задай SHORTS_FFMPEG_PATH"
        ) from exc

    stderr_lines: list[str] = []

    def _drain_stderr() -> None:
        assert process.stderr is not None
        for line in process.stderr:
            stderr_lines.append(line.rstrip())

    stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
    stderr_thread.start()

    assert process.stdout is not None
    deadline = time.monotonic() + timeout if timeout > 0 else None
    timed_out = False
    try:
        for line in process.stdout:
            match = _OUT_TIME.match(line.strip())
            if match and total_duration > 0 and on_progress:
                seconds = int(match.group(1)) / 1_000_000
                on_progress(min(0.99, seconds / total_duration), f"{seconds:.1f} с")
            if deadline is not None and time.monotonic() > deadline:
                timed_out = True
                process.kill()
                break
            if cancel_check is not None:
                try:
                    cancel_check()
                except Exception:
                    process.kill()
                    raise
    finally:
        process.wait()
        stderr_thread.join(timeout=5)

    if timed_out:
        raise MediaError(f"{description}: не уложился в {timeout:.0f} с и был остановлен")

    if process.returncode != 0:
        stderr = "\n".join(stderr_lines[-40:])
        hint = next((h for marker, h in _ERROR_HINTS.items() if marker in stderr), "")
        message = f"{description} завершился с кодом {process.returncode}"
        if hint:
            message += f" — {hint}"
        raise MediaError(f"{message}\n{stderr}")

    if on_progress:
        on_progress(1.0, "готово")


def extract_audio(video_path: Path, out_path: Path) -> Path:
    """Моно 16 кГц WAV — то, что ждут все модели распознавания речи."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(
        [
            "-i", str(video_path),
            "-vn", "-ac", "1", "-ar", "16000",
            "-c:a", "pcm_s16le",
            str(out_path),
        ],
        description="извлечение аудио",
    )
    return out_path


def extract_thumbnail(video_path: Path, out_path: Path, at_second: float = 1.0) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(
        [
            "-ss", f"{max(0.0, at_second):.3f}",
            "-i", str(video_path),
            "-frames:v", "1",
            "-q:v", "3",
            str(out_path),
        ],
        description="кадр превью",
    )
    return out_path


def extract_frames(
    video_path: Path,
    out_dir: Path,
    *,
    count: int,
    duration: float,
    crop: str = "",
    width: int = 640,
) -> list[Path]:
    """Равномерно снимает count кадров.

    Каждый кадр берётся отдельным вызовом с перемоткой `-ss` до `-i`: ffmpeg
    прыгает по индексу файла и декодирует доли секунды. Фильтр `fps=1/N` за
    один вызов выглядит экономнее, но заставляет декодировать видео целиком —
    на часовом исходнике это десятки минут вместо секунд.

    Сбойный кадр пропускается: для эвристики важна выборка, а не полный набор.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    count = max(1, count)

    # Первые и последние 4% пропускаем: там заставки и титры, а не типичный кадр.
    span_start, span_end = duration * 0.04, duration * 0.96
    if span_end - span_start < 1.0:  # совсем короткий ролик — берём как есть
        span_start, span_end = 0.0, max(duration, 1.0)
    step = (span_end - span_start) / count

    filters = []
    if crop:
        filters.append(f"crop={crop}")
    filters.append(f"scale={width}:-2")

    frames: list[Path] = []
    for index in range(count):
        target = out_dir / f"frame_{index:03d}.jpg"
        at = span_start + step * (index + 0.5)
        try:
            run_ffmpeg(
                [
                    "-ss", f"{at:.3f}",
                    "-i", str(video_path),
                    "-vf", ",".join(filters),
                    "-frames:v", "1",
                    "-q:v", "4",
                    str(target),
                ],
                description=f"кадр на {at:.0f} с",
                timeout=20.0,
            )
        except MediaError as exc:
            log.warning("кадр на %.0f с не снят: %s", at, exc)
            continue
        if target.exists():
            frames.append(target)
    return frames
