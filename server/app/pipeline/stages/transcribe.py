"""Транскрипция: локальный whisper с переходом на облако при неудаче."""

from __future__ import annotations

import logging
from pathlib import Path

from ...media import extract_audio
from ...config import settings
from ...models import Project, ProjectStatus, Transcript
from ...providers.stt import transcribe_with_fallback
from ...queue import enqueue
from ..context import JobContext
from ..registry import handler
from ..resolve import config_for_project

log = logging.getLogger(__name__)


class TranscribeError(RuntimeError):
    pass


@handler("project.transcribe")
def run_transcribe(ctx: JobContext) -> None:
    project = ctx.db.get(Project, ctx.job.project_id)
    if project is None:
        raise TranscribeError(f"проект {ctx.job.project_id} не найден")

    config = config_for_project(ctx.db, project)["transcribe"]

    audio_path = Path(project.audio_path) if project.audio_path else None
    if audio_path is None or not audio_path.exists():
        if not project.video_path or not Path(project.video_path).exists():
            raise TranscribeError("нет ни аудио, ни исходного видео — сначала загрузи файл")
        ctx.info("аудиодорожка отсутствует, извлекаю заново")
        audio_path = settings.audio_dir / f"{Path(project.video_path).stem}.wav"
        extract_audio(Path(project.video_path), audio_path)
        project.audio_path = str(audio_path)
        ctx.db.commit()

    project.status = ProjectStatus.TRANSCRIBING
    project.stage_message = "распознаю речь"
    ctx.db.commit()

    result = transcribe_with_fallback(
        audio_path,
        requested=config.get("provider", "auto"),
        model=config.get("model", ""),
        language=config.get("language", ""),
        word_timestamps=bool(config.get("word_timestamps", True)),
        options={
            "beam_size": config.get("beam_size", 5),
            "vad_filter": config.get("vad_filter", True),
        },
        on_progress=lambda fraction, message: ctx.progress(fraction, message),
        on_notice=ctx.info,
    )

    ctx.info(
        f"{result.provider}/{result.model}: язык {result.language or 'не определён'}, "
        f"{len(result.segments)} реплик, {len(result.words)} слов"
    )

    if project.transcript is not None:
        ctx.db.delete(project.transcript)
        ctx.db.flush()

    transcript = Transcript(
        project_id=project.id,
        language=result.language,
        provider=result.provider,
        model=result.model,
        segments=[s.to_dict() for s in result.segments],
        words=[w.to_dict() for w in result.words],
        full_text=result.full_text,
    )
    ctx.db.add(transcript)
    project.stage_message = "транскрипт готов"
    ctx.db.commit()

    # Следом идёт поиск фрагментов, и если LLM локальная, ей нужна та же
    # видеокарта. Держать whisper загруженным дальше незачем: расшифровка
    # готова, а освободившиеся ~3 ГБ решают, поместится модель целиком или
    # будет читать веса с диска.
    from ...providers.stt.local_whisper import release_models

    if release_models():
        ctx.info("видеопамять освобождена для следующей стадии")

    ctx.progress(1.0, "транскрипция завершена")

    enqueue(
        ctx.db,
        "project.analyze",
        project_id=project.id,
        priority=ctx.job.priority,
        payload={"auto": ctx.payload.get("auto", True)},
    )
