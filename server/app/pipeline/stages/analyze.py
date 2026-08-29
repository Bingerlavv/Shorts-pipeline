"""Поиск самостоятельных фрагментов в транскрипте с помощью LLM.

Транскрипт режется на куски с нахлёстом, каждый уходит модели отдельно.
Модель возвращает моменты со списком диапазонов — так один шортс может быть
склеен из нескольких последовательных кусков разговора, если по отдельности
они короче нужной длительности.
"""

from __future__ import annotations

import logging
from typing import Any

from ...models import Project, ProjectStatus, Segment, SegmentStatus
from ...providers.llm import build_provider
from ...queue import enqueue
from ...utils.text import clean_hashtags, normalize, truncate
from ..context import JobContext
from ..registry import handler
from ..resolve import config_for_project

log = logging.getLogger(__name__)

CHUNK_OVERLAP_SECONDS = 90.0

MOMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "moments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ranges": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "start": {"type": "number"},
                                "end": {"type": "number"},
                            },
                            "required": ["start", "end"],
                            "additionalProperties": False,
                        },
                    },
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "hashtags": {"type": "array", "items": {"type": "string"}},
                    "hook": {"type": "string"},
                    "score": {"type": "number"},
                    "reason": {"type": "string"},
                    "self_contained": {"type": "boolean"},
                },
                "required": [
                    "ranges", "title", "description", "hashtags",
                    "hook", "score", "reason", "self_contained",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["moments"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You select self-contained moments from a video transcript to publish as vertical short videos.

A moment qualifies only if a viewer who has seen nothing else understands it completely. It must contain its own setup and its own payoff: a claim and its justification, a question and its answer, a story with a beginning and an end, or a complete practical instruction. Reject anything that depends on earlier context — references to "as I said", unexplained names, or a punchline whose setup happened minutes ago.

DURATION
- Each moment must last between {min_duration:.0f} and {max_duration:.0f} seconds in total.
- Aim for the upper half of that range: a moment that stops early feels abrupt, and longer moments hold the viewer once they are already watching. Extend to the natural end of the thought rather than cutting at the first acceptable point. Never pad with unrelated material just to reach the upper bound.
- If a single passage is too short on its own, you may merge it with immediately adjacent passages by returning several entries in `ranges`. Merge only where the result still plays as one continuous thought; never stitch together unrelated topics to pad the length.
- Ranges must be in chronological order and must not overlap.

TIMESTAMPS
- Use the timestamps shown in the transcript, in seconds.
- Start on a sentence boundary and end on a sentence boundary. Never cut mid-sentence.

LANGUAGE
Write `title`, `description` and `hashtags` in {output_language} and nothing else. This overrides any habit you have from the transcript's own language or from field names. If the transcript is in another language, still write the metadata in {output_language}.

METADATA
- `title`: the on-screen title, at most 60 characters. Make it concrete about what this specific moment delivers. No clickbait formulas, no "Du wirst nicht glauben", no emoji.
- `description`: two or three sentences for the video description.
- `hashtags`: 5 to 10 topical tags, no leading '#', no generic filler like "viral" or "fyp".
- `hook`: the single strongest sentence from the moment, quoted as spoken.

SCORING
- `score` from 0 to 1: how well this stands alone and holds attention. Reserve values above 0.8 for moments that are genuinely complete and compelling.
- `reason`: one sentence on why this works as a standalone short.
- `self_contained`: false if the moment needs outside context — return it anyway with a low score rather than silently dropping it.

Return at most {target_count} moments, best first. Returning fewer is correct when the material does not contain more that qualify. Do not lower your standard to reach the count."""


def _build_chunks(
    segments: list[dict[str, Any]], chunk_seconds: float
) -> list[list[dict[str, Any]]]:
    """Режет транскрипт на куски с нахлёстом, чтобы момент не разорвало границей."""
    if not segments:
        return []

    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    chunk_start = segments[0]["start"]

    for segment in segments:
        if current and segment["end"] - chunk_start > chunk_seconds:
            chunks.append(current)
            overlap_from = segment["start"] - CHUNK_OVERLAP_SECONDS
            current = [s for s in current if s["end"] >= overlap_from]
            chunk_start = current[0]["start"] if current else segment["start"]
        current.append(segment)

    if current:
        chunks.append(current)
    return chunks


def _render_transcript(segments: list[dict[str, Any]]) -> str:
    return "\n".join(
        f"[{s['start']:.1f} - {s['end']:.1f}] {normalize(s['text'])}" for s in segments
    )


def _total_duration(ranges: list[tuple[float, float]]) -> float:
    return sum(max(0.0, end - start) for start, end in ranges)


def _extend_to_minimum(
    ranges: list[tuple[float, float]],
    segments: list[dict[str, Any]],
    min_duration: float,
    max_duration: float,
) -> list[tuple[float, float]]:
    """Дотягивает короткий фрагмент соседними репликами транскрипта."""
    if not ranges or _total_duration(ranges) >= min_duration:
        return ranges

    start, end = ranges[0][0], ranges[-1][1]
    after = [s for s in segments if s["start"] >= end]
    before = [s for s in segments if s["end"] <= start]

    # Расширяем сначала вперёд: продолжение мысли информативнее, чем разбег до неё.
    while _total_duration(ranges) < min_duration and (after or before):
        if after and (after[0]["end"] - start) <= max_duration:
            end = after.pop(0)["end"]
        elif before and (end - before[-1]["start"]) <= max_duration:
            start = before.pop()["start"]
        else:
            break
        ranges = [(start, end)]
    return ranges


def _overlaps(a: tuple[float, float], b: tuple[float, float]) -> float:
    return max(0.0, min(a[1], b[1]) - max(a[0], b[0]))


def _dedupe(moments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Куски идут с нахлёстом, поэтому один момент может прийти дважды."""
    kept: list[dict[str, Any]] = []
    for moment in sorted(moments, key=lambda m: -m["score"]):
        span = (moment["start"], moment["end"])
        duplicate = False
        for existing in kept:
            other = (existing["start"], existing["end"])
            shared = _overlaps(span, other)
            shortest = min(span[1] - span[0], other[1] - other[0])
            if shortest > 0 and shared / shortest > 0.5:
                duplicate = True
                break
        if not duplicate:
            kept.append(moment)
    return kept


def _parse_moments(
    payload: dict[str, Any],
    segments: list[dict[str, Any]],
    config: dict[str, Any],
    project_duration: float,
    ctx: JobContext,
) -> list[dict[str, Any]]:
    min_duration = float(config["min_duration"])
    max_duration = float(config["max_duration"])
    results: list[dict[str, Any]] = []

    for raw in payload.get("moments", []):
        ranges = [
            (float(r["start"]), float(r["end"]))
            for r in raw.get("ranges", [])
            if float(r.get("end", 0)) > float(r.get("start", 0))
        ]
        if not ranges:
            continue

        ranges.sort()
        if project_duration:
            ranges = [
                (max(0.0, s), min(project_duration, e))
                for s, e in ranges
                if s < project_duration
            ]
            ranges = [(s, e) for s, e in ranges if e > s]
        if not ranges:
            continue

        ranges = _extend_to_minimum(ranges, segments, min_duration, max_duration)
        duration = _total_duration(ranges)

        if duration < min_duration:
            ctx.info(
                f"пропускаю «{truncate(raw.get('title_de', ''), 40)}»: "
                f"{duration:.1f} с — короче минимума {min_duration:.0f} с"
            )
            continue
        if duration > max_duration:
            # Подрезаем с конца, сохраняя начало и склейку.
            trimmed: list[tuple[float, float]] = []
            budget = max_duration
            for start, end in ranges:
                span = end - start
                if span <= budget:
                    trimmed.append((start, end))
                    budget -= span
                else:
                    if budget > 1.0:
                        trimmed.append((start, start + budget))
                    break
            ranges = trimmed
            duration = _total_duration(ranges)
            if duration < min_duration:
                continue

        results.append(
            {
                "ranges": ranges,
                "start": ranges[0][0],
                "end": ranges[-1][1],
                "duration": duration,
                # Ключи без языкового суффикса: имя title_de склоняло модель
                # писать по-немецки независимо от настройки языка. Старые имена
                # читаем тоже — вдруг модель ответит по прежней схеме.
                "title_de": truncate(
                    normalize(raw.get("title") or raw.get("title_de", "")), 200
                ),
                "description_de": normalize(
                    raw.get("description") or raw.get("description_de", "")
                ),
                "hashtags": clean_hashtags(raw.get("hashtags")),
                "hook": normalize(raw.get("hook", "")),
                "score": max(0.0, min(1.0, float(raw.get("score", 0.0)))),
                "reason": normalize(raw.get("reason", "")),
                "self_contained": bool(raw.get("self_contained", True)),
            }
        )

    return results


@handler("project.analyze")
def run_analyze(ctx: JobContext) -> None:
    project = ctx.db.get(Project, ctx.job.project_id)
    if project is None:
        raise RuntimeError(f"проект {ctx.job.project_id} не найден")
    if project.transcript is None or not project.transcript.segments:
        raise RuntimeError("нет транскрипта — сначала выполни распознавание речи")

    full_config = config_for_project(ctx.db, project)
    config = full_config["analyze"]

    project.status = ProjectStatus.ANALYZING
    project.stage_message = "ищу самостоятельные фрагменты"
    ctx.db.commit()

    provider = build_provider(config.get("provider") or "", config.get("model") or "")
    available, reason = provider.is_available()
    if not available:
        raise RuntimeError(f"LLM-провайдер {provider.name} недоступен: {reason}")
    ctx.info(f"анализирую через {provider.name}/{provider.model}")

    segments = project.transcript.segments
    chunks = _build_chunks(segments, float(config.get("chunk_minutes", 25)) * 60)
    ctx.info(f"транскрипт разбит на {len(chunks)} кусок(ов)")

    system = SYSTEM_PROMPT.format(
        min_duration=float(config["min_duration"]),
        max_duration=float(config["max_duration"]),
        target_count=int(config["target_count"]),
        output_language={"de": "German", "en": "English", "ru": "Russian"}.get(
            config.get("output_language", "de"), config.get("output_language", "de")
        ),
    )
    extra = normalize(config.get("extra_instructions", ""))
    if extra:
        system += f"\n\nADDITIONAL CONTEXT FROM THE OPERATOR\n{extra}"

    collected: list[dict[str, Any]] = []
    for index, chunk in enumerate(chunks):
        ctx.check_cancelled()
        ctx.progress(index / len(chunks), f"кусок {index + 1} из {len(chunks)}")

        user = (
            f"Video title: {project.title or 'unknown'}\n"
            f"Total duration: {project.duration:.0f} seconds\n"
            f"Transcript language: {project.transcript.language or 'unknown'}\n\n"
            f"TRANSCRIPT (timestamps in seconds, absolute within the full video):\n"
            f"{_render_transcript(chunk)}"
        )

        payload = provider.generate_json(
            system=system,
            user=user,
            schema=MOMENT_SCHEMA,
            schema_name="moments",
        )
        parsed = _parse_moments(payload, segments, config, project.duration, ctx)
        ctx.info(f"кусок {index + 1}: принято {len(parsed)} момент(ов)")
        collected.extend(parsed)

    min_score = float(config.get("min_score", 0.0))
    collected = [m for m in collected if m["score"] >= min_score]
    collected = _dedupe(collected)
    collected.sort(key=lambda m: -m["score"])
    collected = collected[: int(config.get("target_count", 10))]

    if not collected:
        project.status = ProjectStatus.READY
        project.stage_message = (
            "подходящих фрагментов не найдено — снизь min_score или расширь "
            "диапазон длительности"
        )
        ctx.db.commit()
        ctx.info(project.stage_message)
        return

    # Прошлые кандидаты, которых человек ещё не трогал, заменяем новыми.
    for existing in list(project.segments):
        if existing.status == SegmentStatus.CANDIDATE:
            ctx.db.delete(existing)
    ctx.db.flush()

    for moment in collected:
        ctx.db.add(
            Segment(
                project_id=project.id,
                start=moment["start"],
                end=moment["end"],
                source_ranges=[[s, e] for s, e in moment["ranges"]],
                title_de=moment["title_de"],
                description_de=moment["description_de"],
                hashtags=moment["hashtags"],
                hook=moment["hook"],
                score=moment["score"],
                reason=moment["reason"],
                transcript_text=" ".join(
                    normalize(s["text"])
                    for s in segments
                    if any(s["start"] >= a and s["end"] <= b for a, b in moment["ranges"])
                ),
                status=SegmentStatus.CANDIDATE,
            )
        )

    project.status = ProjectStatus.READY
    project.stage_message = f"найдено фрагментов: {len(collected)}"
    ctx.db.commit()
    ctx.progress(1.0, project.stage_message)
    ctx.info(project.stage_message)

    # Молчаливая остановка на ревью — самая частая жалоба: непонятно, автопрогон
    # сломался или так и задумано. Пишем причину в лог задачи.
    if not ctx.payload.get("auto"):
        ctx.info("стадия запущена вручную без продолжения — фрагменты ждут ревью")
    elif not project.auto_publish:
        ctx.info(
            "автопрогон у проекта выключен — фрагменты ждут ревью. "
            "Включить: Настройки проекта → Автопрогон"
        )
    else:
        ctx.info("включён автопрогон — ставлю фрагменты в рендер")
        for segment in project.segments:
            if segment.status == SegmentStatus.CANDIDATE:
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
