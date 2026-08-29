"""Нарезка и монтаж одного фрагмента."""

from __future__ import annotations

import logging
from pathlib import Path

from ...config import settings
from ...media import (
    build_ass,
    build_render_command,
    cut_ranges,
    extract_thumbnail,
    probe_media,
    slice_words,
    snap_to_word_boundaries,
)
from ...media.filtergraph import RenderInputs
from ...media.fonts import resolve_font
from ...media.runner import run_ffmpeg
from ...models import Project, ProjectStatus, Segment, SegmentStatus
from ...queue import enqueue
from ...utils.text import safe_filename
from ..context import JobContext
from ..registry import handler
from ..resolve import config_for_segment, edit_asset_paths, pick_background

log = logging.getLogger(__name__)


class RenderError(RuntimeError):
    pass


def _resolve_ranges(segment: Segment, config: dict, words: list[dict]) -> list[tuple[float, float]]:
    ranges = [(float(a), float(b)) for a, b in (segment.source_ranges or [])]
    if not ranges:
        ranges = [(segment.start, segment.end)]

    if config.get("snap_to_words", True) and words:
        ranges = [snap_to_word_boundaries(start, end, words) for start, end in ranges]
    return ranges


@handler("segment.render")
def run_render(ctx: JobContext) -> None:
    segment = ctx.db.get(Segment, ctx.job.segment_id)
    if segment is None:
        raise RenderError(f"фрагмент {ctx.job.segment_id} не найден")

    project: Project = segment.project
    if not project.video_path or not Path(project.video_path).exists():
        raise RenderError("исходное видео отсутствует на диске — загрузи проект заново")

    config = config_for_segment(ctx.db, segment)
    assets = edit_asset_paths(ctx.db, config)

    # Ассет мог быть удалён, а ссылка на него в пресете осталась. Раньше монтаж
    # в таком случае молча собирал ролик без наложения — для баннера это прямая
    # потеря денег: видео выглядит готовым, а рекламы в нём нет.
    edit_config = config["edit"]
    for name, label, critical in (
        ("banner", "баннер", True),
        ("mask", "маска", False),
        ("lut", "цветовой профиль (LUT)", False),
    ):
        section = edit_config.get("banner" if name == "banner" else name, {})
        if name == "lut":
            section = edit_config.get("color", {})
            asset_id = section.get("lut_asset_id")
            enabled = bool(section.get("enabled")) and asset_id is not None
        else:
            asset_id = section.get("asset_id")
            enabled = bool(section.get("enabled")) and asset_id is not None
        if not enabled or assets.get(name) is not None:
            continue
        message = (
            f"{label} включён в настройках, но материал №{asset_id} не найден — "
            "он удалён из «Материалов» или ссылка устарела"
        )
        if critical:
            raise RenderError(
                message + ". Загрузи файл заново и выбери его в пресете: "
                "монтировать ролик без баннера бессмысленно"
            )
        ctx.info(message + ", монтирую без него")

    # Фон выбирается по номеру фрагмента: пересборка даст тот же ролик, иначе
    # уже одобренный шортс менялся бы при каждом повторном монтаже.
    background = pick_background(ctx.db, config, seed=segment.id)
    background_has_audio = False
    if background is not None:
        ctx.info(f"фон: {background.name}")
        try:
            background_has_audio = probe_media(background).has_audio
        except Exception as exc:  # noqa: BLE001 — без звука фон всё равно годится
            ctx.info(f"не удалось прочитать фон {background.name}: {exc}")
    elif config.get("edit", {}).get("background", {}).get("enabled"):
        ctx.info("фон включён, но ни один файл не выбран — монтирую без него")

    segment.status = SegmentStatus.RENDERING
    segment.error = ""
    project.status = ProjectStatus.RENDERING
    ctx.db.commit()

    words = project.transcript.words if project.transcript else []
    ranges = _resolve_ranges(segment, config["cut"], words)
    ctx.info(
        "диапазоны: "
        + ", ".join(f"{start:.1f}–{end:.1f}" for start, end in ranges)
    )

    stem = f"seg_{segment.id}_{safe_filename(segment.title_de or 'clip', 40)}"
    clip_path = settings.clips_dir / f"{stem}.mp4"

    ctx.info("режу фрагмент из исходника")
    cut_ranges(
        Path(project.video_path),
        clip_path,
        ranges,
        config["cut"],
        total_duration=project.duration,
        on_progress=lambda fraction, message: ctx.stage(0.0, 0.35).progress(fraction, message),
    )
    segment.clip_path = str(clip_path)
    ctx.db.commit()

    clip_info = probe_media(clip_path)
    if clip_info.duration <= 0:
        raise RenderError("нарезка дала пустой файл — проверь таймкоды фрагмента")

    # --- субтитры ---
    subtitles_path: Path | None = None
    subtitles_config = config["edit"]["subtitles"]
    if subtitles_config.get("enabled"):
        if not words:
            ctx.info("субтитры включены, но в транскрипте нет пословных таймкодов — пропускаю")
        else:
            speed_config = config["edit"]["speed"]
            # Скорость учитываем при сдвиге реплик: рендер ускоряет и картинку, и звук.
            speed = 1.0
            if speed_config.get("enabled") and not speed_config.get("randomize"):
                speed = float(speed_config.get("factor", 1.0))
            font = resolve_font(assets["subtitle_font"])
            subtitles_path = build_ass(
                slice_words(words, ranges),
                settings.clips_dir / f"{stem}.ass",
                subtitles_config,
                offset=0.0,
                speed=speed,
                width=config["edit"]["output"]["width"],
                height=config["edit"]["output"]["height"],
                font_name=font.stem,
            )
            if subtitles_path:
                ctx.info(f"субтитры собраны: {subtitles_path.name}")

    # --- монтаж ---
    render_path = settings.renders_dir / f"{stem}.mp4"
    detection = (project.source_meta or {}).get("text_detection", {})
    source_has_title = bool((detection.get("title") or {}).get("present"))

    inputs = RenderInputs(
        source=clip_path,
        output=render_path,
        duration=clip_info.duration,
        title_text=segment.title_de,
        source_has_title=source_has_title,
        source_has_subtitles=bool(project.has_burned_subtitles),
        subtitles_path=subtitles_path,
        mask_path=assets["mask"],
        banner_path=assets["banner"],
        background_path=background,
        background_has_audio=background_has_audio,
        lut_path=assets["lut"],
        font_path=assets["font"],
        seed=segment.id,
    )
    plan = build_render_command(config["edit"], inputs)
    ctx.info(
        f"монтаж: скорость {plan.applied['speed']}, кадрирование "
        f"{plan.applied['framing']}, зеркало "
        f"{'да' if plan.applied['mirrored'] else 'нет'}"
    )

    run_ffmpeg(
        plan.args,
        total_duration=plan.duration,
        on_progress=lambda fraction, message: ctx.stage(0.35, 0.95).progress(fraction, message),
        cancel_check=ctx.check_cancelled,
        description="монтаж",
    )

    thumb_path = settings.thumbs_dir / f"{stem}.jpg"
    try:
        extract_thumbnail(render_path, thumb_path, at_second=min(1.5, plan.duration / 3))
        segment.thumb_path = str(thumb_path)
    except Exception as exc:  # noqa: BLE001 — превью не критично для результата
        ctx.info(f"не удалось снять превью: {exc}")

    segment.render_path = str(render_path)
    segment.render_meta = {
        "duration": round(plan.duration, 3),
        "applied": plan.applied,
        "subtitles": bool(subtitles_path),
    }
    segment.status = SegmentStatus.RENDERED
    ctx.db.commit()
    ctx.progress(1.0, "монтаж завершён")
    ctx.info(f"готово: {render_path.name} ({plan.duration:.1f} с)")

    remaining = [
        s for s in project.segments
        if s.status in (SegmentStatus.APPROVED, SegmentStatus.RENDERING)
    ]
    if not remaining:
        project.status = ProjectStatus.DONE
        project.stage_message = "все одобренные фрагменты смонтированы"
        ctx.db.commit()

    # Публикацию включает либо галочка проекта, либо publish.auto в пресете.
    # Раньше учитывался только пресет, и проект с включённым автопрогоном
    # доходил до смонтированных роликов и там замирал без объяснений.
    auto_publish = bool(config["publish"].get("auto")) or bool(project.auto_publish)
    if ctx.payload.get("auto") and auto_publish:
        from .publish import needs_caption, queue_publications

        if needs_caption(config, segment):
            # Тексты пишем до публикации, а не параллельно: иначе ролик уйдёт
            # со старым заголовком. Публикации поставит сама стадия текстов.
            ctx.info("сначала напишу тексты, публикация пойдёт после них")
            enqueue(
                ctx.db,
                "segment.caption",
                project_id=project.id,
                segment_id=segment.id,
                priority=ctx.job.priority + 5,
                payload={"auto": True},
            )
        else:
            queue_publications(ctx, segment, config["publish"])
