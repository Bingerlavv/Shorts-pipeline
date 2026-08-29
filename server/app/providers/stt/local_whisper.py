"""Локальная транскрипция через faster-whisper (CTranslate2)."""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any

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

# Модель весит гигабайты и грузится секунды — держим один экземпляр на процесс.
_model_lock = threading.Lock()
_model_cache: dict[tuple[str, str, str], Any] = {}

_dll_dirs_registered = False


def _register_cuda_dlls() -> None:
    """Показывает CTranslate2, где лежат cuBLAS и cuDNN.

    Колёса nvidia-* кладут DLL внутрь site-packages, куда Windows сама не
    смотрит. Без этого CTranslate2 загружает модель, а на первом же сегменте
    падает с «Library cublas64_12.dll is not found», хотя файл на месте.

    Каталоги добавляются в PATH, а не только через add_dll_directory: библиотеки
    подгружаются не при импорте расширения, а лениво во время счёта, обычным
    LoadLibrary — тот ищет именно по PATH. На Linux и macOS ничего не нужно,
    там отрабатывает rpath.
    """
    global _dll_dirs_registered
    if _dll_dirs_registered or os.name != "nt":
        return

    import site

    roots = {*site.getsitepackages(), site.getusersitepackages()}
    bin_dirs: list[str] = []
    for packages in roots:
        nvidia = Path(packages) / "nvidia"
        if nvidia.exists():
            bin_dirs.extend(str(d) for d in nvidia.glob("*/bin"))

    if not bin_dirs:
        log.debug("каталоги CUDA не найдены — счёт пойдёт на процессоре")
        _dll_dirs_registered = True
        return

    for bin_dir in bin_dirs:
        try:
            os.add_dll_directory(bin_dir)
        except OSError as exc:  # noqa: PERF203 — каталог мог исчезнуть
            log.debug("не удалось зарегистрировать %s: %s", bin_dir, exc)

    current = os.environ.get("PATH", "")
    missing = [d for d in bin_dirs if d not in current]
    if missing:
        os.environ["PATH"] = os.pathsep.join(missing) + os.pathsep + current
    log.debug("каталоги CUDA добавлены в PATH: %s", len(bin_dirs))
    _dll_dirs_registered = True


# Выполняется при импорте: PATH должен быть готов раньше, чем CTranslate2
# впервые полезет за библиотеками.
_register_cuda_dlls()


def release_models() -> int:
    """Выгружает модели распознавания из видеопамяти.

    Видеокарта одна, а желающих двое: whisper и локальная LLM. Кэш модели
    экономит ~17 с на повторной загрузке, но пока whisper держит ~3 ГБ,
    модели крупнее 8B в оставшееся не помещаются и Ollama начинает читать
    веса с диска — запрос растягивается с минуты до получаса.
    Поэтому после распознавания память отдаём.
    """
    import gc

    with _model_lock:
        count = len(_model_cache)
        for model in _model_cache.values():
            # У CTranslate2 выгрузка явная; без неё память держится до сборки мусора.
            inner = getattr(model, "model", None)
            if inner is not None and hasattr(inner, "unload_model"):
                try:
                    inner.unload_model()
                except Exception as exc:  # noqa: BLE001 — освобождение не критично
                    log.debug("не удалось выгрузить модель: %s", exc)
        _model_cache.clear()
    gc.collect()
    if count:
        log.info("видеопамять освобождена: выгружено моделей — %d", count)
    return count


class LocalWhisperProvider(STTProvider):
    name = "local"

    def __init__(
        self,
        model_size: str | None = None,
        device: str | None = None,
        compute_type: str | None = None,
    ) -> None:
        self.model_size = model_size or settings.whisper_model
        self.device = device or settings.whisper_device
        self.compute_type = compute_type or settings.whisper_compute_type

    def is_available(self) -> tuple[bool, str]:
        try:
            import faster_whisper  # noqa: F401
        except ImportError as exc:
            return False, (
                f"faster-whisper не установлен ({exc}). "
                "Поставь: pip install -r requirements-gpu.txt на Python 3.11/3.12"
            )
        return True, ""

    def _load_model(self):  # noqa: ANN202
        _register_cuda_dlls()
        from faster_whisper import WhisperModel

        key = (self.model_size, self.device, self.compute_type)
        with _model_lock:
            if key not in _model_cache:
                log.info(
                    "загружаю whisper %s на %s (%s)", self.model_size, self.device, self.compute_type
                )
                try:
                    _model_cache[key] = WhisperModel(
                        self.model_size,
                        device=self.device,
                        compute_type=self.compute_type,
                        download_root=str(settings.models_dir),
                    )
                except Exception as exc:  # noqa: BLE001
                    if self.device == "cuda":
                        log.warning("CUDA недоступна (%s), падаю на CPU int8", exc)
                        _model_cache[key] = self._cpu_model()
                    else:
                        raise STTError(f"не удалось загрузить модель whisper: {exc}") from exc
            return _model_cache[key]

    def _cpu_model(self):  # noqa: ANN202
        from faster_whisper import WhisperModel

        return WhisperModel(
            self.model_size,
            device="cpu",
            compute_type="int8",
            download_root=str(settings.models_dir),
        )

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

        options = options or {}
        model = self._load_model()

        def start(active_model):  # noqa: ANN001, ANN202
            return active_model.transcribe(
                str(audio_path),
                language=language or None,
                beam_size=int(options.get("beam_size", 5)),
                word_timestamps=word_timestamps,
                vad_filter=bool(options.get("vad_filter", True)),
                vad_parameters={"min_silence_duration_ms": 400},
                condition_on_previous_text=False,
            )

        try:
            segments_iter, info = start(model)
        except Exception as exc:  # noqa: BLE001
            # Нехватка cuBLAS/cuDNN всплывает не при загрузке модели, а при
            # первом счёте — поэтому CPU-фолбэк нужен и здесь.
            if self.device != "cuda":
                raise STTError(f"транскрипция не удалась: {exc}") from exc
            log.warning("CUDA отвалилась на счёте (%s), повторяю на CPU int8", exc)
            model = self._cpu_model()
            _model_cache[(self.model_size, self.device, self.compute_type)] = model
            segments_iter, info = start(model)

        total = float(getattr(info, "duration", 0.0)) or 0.0
        result = TranscriptionResult(
            language=getattr(info, "language", "") or (language or ""),
            provider=self.name,
            model=self.model_size,
        )

        for segment in segments_iter:
            text = (segment.text or "").strip()
            if text:
                result.segments.append(TranscriptSegment(segment.start, segment.end, text))
            for word in getattr(segment, "words", None) or []:
                token = (word.word or "").strip()
                if token:
                    result.words.append(TranscriptWord(word.start, word.end, token))
            if on_progress and total > 0:
                on_progress(min(0.99, segment.end / total), f"{segment.end:.0f} из {total:.0f} с")

        if not result.segments:
            raise STTError("транскрипция пустая — в файле нет распознаваемой речи")
        if on_progress:
            on_progress(1.0, "транскрипция готова")
        return result
