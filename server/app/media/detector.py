"""Эвристика: есть ли в кадре вшитый текст (субтитры или заголовок).

Нужна для правила «зеркалить, только если субтитров нет» — зеркальный текст
в кадре мгновенно выдаёт переработанное видео.

Метод: снимаем кадры, берём полосу кадра, считаем плотность вертикальных
границ (у букв их много) и то, насколько полоса меняется между кадрами.
Статичный логотип даёт высокую плотность, но низкую изменчивость; субтитры —
высокую и ту, и другую. Это оценка, а не факт: результат отдаётся с
уверенностью и всегда переопределяем вручную из панели.
"""

from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .runner import extract_frames

log = logging.getLogger(__name__)

FRAME_COUNT = 12
# Полосы кадра долями высоты: субтитры почти всегда внизу, заголовки — сверху.
# Кадры снимаются целиком один раз, полосы вырезаются из массива — так ffmpeg
# дёргается вдвое реже.
BANDS = {
    "subtitles": (0.66, 0.94),
    "title": (0.04, 0.28),
}


@dataclass
class TextDetection:
    present: bool | None
    confidence: float
    edge_density: float
    temporal_change: float
    note: str = ""


def _analyze_band(arrays: list, top: float, bottom: float) -> TextDetection:
    """Оценивает одну горизонтальную полосу по уже снятым кадрам."""
    import numpy as np

    bands = []
    densities = []
    for gray in arrays:
        height = gray.shape[0]
        band = gray[int(height * top) : int(height * bottom), :]
        if band.size == 0:
            continue
        bands.append(band)
        # Вертикальные перепады яркости: у текста их существенно больше,
        # чем у плавных градиентов реального кадра.
        gradient = np.abs(np.diff(band, axis=1))
        densities.append(float((gradient > 40).mean()))

    if len(bands) < 3:
        return TextDetection(None, 0.0, 0.0, 0.0, "слишком мало кадров для анализа")

    edge_density = float(np.mean(densities))
    diffs = [float(np.abs(bands[i] - bands[i - 1]).mean()) for i in range(1, len(bands))]
    temporal_change = float(np.mean(diffs))

    # Пороги подобраны на 1080p-исходниках; полоса с субтитрами обычно даёт
    # плотность 0.05–0.20 при заметной смене между репликами.
    dense = edge_density > 0.035
    changing = temporal_change > 3.0

    if dense and changing:
        present, confidence = True, min(0.95, 0.5 + edge_density * 4)
    elif dense and not changing:
        present, confidence = True, 0.55  # статичная плашка/логотип — текст всё же есть
    elif not dense:
        present, confidence = False, min(0.9, 0.5 + (0.035 - edge_density) * 10)
    else:
        present, confidence = None, 0.3

    return TextDetection(present, round(confidence, 2), round(edge_density, 4),
                         round(temporal_change, 2))


def detect_burned_text(video: Path, duration: float) -> dict[str, TextDetection]:
    """Проверяет обе полосы кадра. Пустой словарь — анализ не удался."""
    try:
        import numpy as np
        from PIL import Image
    except ImportError as exc:
        note = f"определение текста выключено: не установлены numpy/Pillow ({exc})"
        return {name: TextDetection(None, 0.0, 0.0, 0.0, note) for name in BANDS}

    try:
        with tempfile.TemporaryDirectory(prefix="shorts_detect_") as tmp:
            frames = extract_frames(
                video, Path(tmp), count=FRAME_COUNT, duration=duration, width=480
            )
            arrays = []
            for frame in frames:
                with Image.open(frame) as image:
                    arrays.append(np.asarray(image.convert("L"), dtype=np.float32))
    except Exception as exc:  # noqa: BLE001 — эвристика не должна валить конвейер
        log.warning("не удалось снять кадры для анализа текста: %s", exc)
        return {name: TextDetection(None, 0.0, 0.0, 0.0, str(exc)) for name in BANDS}

    results: dict[str, TextDetection] = {}
    for name, (top, bottom) in BANDS.items():
        try:
            results[name] = _analyze_band(arrays, top, bottom)
        except Exception as exc:  # noqa: BLE001
            log.warning("не удалось проанализировать полосу %s: %s", name, exc)
            results[name] = TextDetection(None, 0.0, 0.0, 0.0, str(exc))
    return results


def has_burned_subtitles(video: Path, duration: float) -> tuple[bool | None, dict]:
    results = detect_burned_text(video, duration)
    subtitles = results.get("subtitles")
    details = {
        name: {
            "present": item.present,
            "confidence": item.confidence,
            "edge_density": item.edge_density,
            "temporal_change": item.temporal_change,
            "note": item.note,
        }
        for name, item in results.items()
    }
    if subtitles is None:
        return None, details
    # Ниже 0.6 доверия не берём на себя решение — пусть выбирает человек.
    if subtitles.confidence < 0.6:
        return None, details
    return subtitles.present, details
