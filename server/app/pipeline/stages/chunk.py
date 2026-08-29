"""Плоская нарезка: куски равной длины без транскрипции и поиска моментов.

Заменяет собой обе умные стадии сразу. Нужна там, где исходник и так состоит
из самостоятельных частей — запись эфира, летсплей, длинное интервью, — и
разбирать его смыслово незачем: ни звук, ни модель здесь не участвуют, поэтому
час видео размечается мгновенно и не занимает видеокарту.

Границы кусков здесь честно произвольные: слово на стыке будет разрезано.
Подрезка по паузам (cut.snap_to_words) умеет это чинить, но только если у
проекта есть транскрипт, — в плоском режиме его нет, и стыки остаются как есть.
"""

from __future__ import annotations

import logging
from typing import Any

from ...models import Project, ProjectStatus, Segment, SegmentStatus
from ...queue import enqueue
from ...utils.text import normalize, truncate
from ..context import JobContext
from ..registry import handler
from ..resolve import config_for_project

log = logging.getLogger(__name__)

# Ниже этого куска не бывает: полсекунды видео — не шортс, а сбой в настройках.
MIN_CHUNK = 1.0


class ChunkError(RuntimeError):
    pass


def plan_chunks(duration: float, config: dict[str, Any]) -> list[tuple[float, float]]:
    """Считает границы кусков. Чистая функция — её удобно проверять отдельно.

    Длина 0 — особый случай: весь ролик одним куском.
    """
    length = float(config.get("duration", 60.0) or 0)
    # Ноль означает «не резать»: весь ролик одним куском. Так это и подписано в
    # панели, и это единственный внятный способ сказать «просто перекадрируй
    # целиком, без нарезки». Раньше ноль был ошибкой, и человек, поверивший
    # подписи, получал упавшую стадию.
    if length == 0:
        length = max(MIN_CHUNK, duration)
    if length < MIN_CHUNK:
        raise ChunkError(
            f"длина куска {length:g} с — слишком мало, поставь хотя бы {MIN_CHUNK:g} с"
        )

    # Нахлёст обязан быть меньше куска. Молча его подрезать нельзя: опечатка
    # («нахлёст 90» вместо 9 при куске в 60) дала бы шаг в полсекунды и пять
    # сотен почти одинаковых роликов, каждый со своим монтажом на минуту счёта.
    overlap = max(0.0, float(config.get("overlap", 0.0) or 0))
    if overlap >= length:
        raise ChunkError(
            f"нахлёст {overlap:g} с не меньше самого куска ({length:g} с) — "
            "куски не сдвинутся с места. Сделай нахлёст меньше длины куска."
        )
    skip_start = max(0.0, float(config.get("skip_start", 0.0) or 0))
    skip_end = max(0.0, float(config.get("skip_end", 0.0) or 0))

    begin = skip_start
    finish = duration - skip_end
    if finish - begin < MIN_CHUNK:
        raise ChunkError(
            f"после пропуска начала ({skip_start:g} с) и конца ({skip_end:g} с) "
            f"от видео длиной {duration:.1f} с не осталось ничего для нарезки"
        )

    step = length - overlap
    ranges: list[tuple[float, float]] = []
    position = begin
    while position < finish - 0.05:
        end = min(position + length, finish)
        ranges.append((position, end))
        if end >= finish - 0.05:
            break
        position += step

    # Короткий хвост приклеиваем к предыдущему куску: отдельным роликом он
    # всё равно не годится, а выбрасывать материал незачем.
    min_tail = max(0.0, float(config.get("min_tail", 0.0) or 0))
    if len(ranges) > 1 and ranges[-1][1] - ranges[-1][0] < min_tail:
        tail = ranges.pop()
        ranges[-1] = (ranges[-1][0], tail[1])

    limit = int(config.get("limit", 0) or 0)
    if limit > 0:
        ranges = ranges[:limit]
    return ranges


def _timecode(seconds: float) -> str:
    total = int(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _title_for(template: str, project: Project, index: int, total: int,
               start: float) -> str:
    """Собирает заголовок куска. Кривой шаблон не должен ронять стадию."""
    fallback = f"Часть {index}"
    try:
        title = template.format(
            title=project.title or "Без названия",
            index=index,
            total=total,
            start=_timecode(start),
        )
    except (KeyError, IndexError, ValueError):
        # Опечатка в шаблоне («{чась}») — не повод терять всю нарезку.
        return fallback
    return truncate(normalize(title), 200) or fallback


def _transcript_text(project: Project, start: float, end: float) -> str:
    """Расшифровка куска, если она вдруг есть.

    В плоском режиме транскрипта обычно нет, но проект мог быть распознан
    раньше или руками. Тогда текст пригодится стадии подписей, и терять его
    из-за смены режима нарезки незачем.
    """
    if project.transcript is None:
        return ""
    parts = [
        normalize(item.get("text", ""))
        for item in project.transcript.segments
        if float(item.get("start", 0.0)) < end and float(item.get("end", 0.0)) > start
    ]
    return truncate(" ".join(part for part in parts if part), 4000)


@handler("project.chunk")
def run_chunk(ctx: JobContext) -> None:
    project = ctx.db.get(Project, ctx.job.project_id)
    if project is None:
        raise ChunkError(f"проект {ctx.job.project_id} не найден")
    if project.duration <= 0:
        raise ChunkError(
            "длительность исходника неизвестна — сначала загрузи файл (стадия «Скачать заново»)"
        )

    config = config_for_project(ctx.db, project)["chunks"]

    project.status = ProjectStatus.ANALYZING
    project.stage_message = "режу на куски"
    project.error = ""
    ctx.db.commit()

    ranges = plan_chunks(project.duration, config)
    if not ranges:
        raise ChunkError("не получилось ни одного куска — проверь длину и пропуски")

    lengths = [end - start for start, end in ranges]
    ctx.info(
        f"кусков: {len(ranges)}, длина от {min(lengths):.0f} до {max(lengths):.0f} с"
    )

    # Прошлых кандидатов, которых человек ещё не трогал, заменяем новыми — так
    # же, как это делает поиск фрагментов. Одобренные и смонтированные не
    # трогаем: перезапуск стадии не должен стирать чужую работу.
    for existing in list(project.segments):
        if existing.status == SegmentStatus.CANDIDATE:
            ctx.db.delete(existing)
    ctx.db.flush()

    template = str(config.get("title_template") or "{title} — часть {index}")
    created: list[Segment] = []
    for index, (start, end) in enumerate(ranges, start=1):
        segment = Segment(
            project_id=project.id,
            start=start,
            end=end,
            source_ranges=[[start, end]],
            title_de=_title_for(template, project, index, len(ranges), start),
            transcript_text=_transcript_text(project, start, end),
            reason=f"плоская нарезка: {_timecode(start)}–{_timecode(end)}",
            status=SegmentStatus.CANDIDATE,
        )
        ctx.db.add(segment)
        created.append(segment)
        ctx.progress(index / len(ranges), f"кусок {index} из {len(ranges)}")

    project.status = ProjectStatus.READY
    project.stage_message = f"нарезано кусков: {len(created)}"
    ctx.db.commit()
    ctx.progress(1.0, project.stage_message)
    ctx.info(project.stage_message)

    # Дальше — ровно та же развилка, что и после поиска фрагментов: молча
    # встать на ревью или уехать в монтаж целиком.
    if not ctx.payload.get("auto"):
        ctx.info("стадия запущена вручную без продолжения — куски ждут ревью")
        return
    if not project.auto_publish:
        ctx.info(
            "автопрогон у проекта выключен — куски ждут ревью. "
            "Включить: Настройки проекта → Автопрогон"
        )
        return

    ctx.info("включён автопрогон — ставлю куски в рендер")
    for segment in created:
        segment.status = SegmentStatus.APPROVED
        enqueue(
            ctx.db,
            "segment.render",
            project_id=project.id,
            segment_id=segment.id,
            priority=ctx.job.priority + 10,
            payload={"auto": True},
            commit=False,
        )
    ctx.db.commit()
