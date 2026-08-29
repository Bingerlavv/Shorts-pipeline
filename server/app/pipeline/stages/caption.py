"""Тексты для площадок: заголовок, описание, хэштеги.

Отдельная стадия, а не часть поиска фрагментов. Причины две. Первая: у поиска
другая задача — отобрать материал, и текст там побочный продукт. Вторая:
переписать подпись хочется без повторного анализа и без перемонтажа, особенно
после ревью.
"""

from __future__ import annotations

import logging
from typing import Any

from ...models import Segment
from ...providers.llm import build_provider
from ...utils.text import clean_hashtags, normalize, truncate
from ..context import JobContext
from ..registry import handler
from ..resolve import config_for_segment

log = logging.getLogger(__name__)


class CaptionError(RuntimeError):
    pass


SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "description": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "description", "hashtags"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """Ты ведёшь Instagram и разбираешься в том, что заставляет людей смотреть короткие видео до конца и пересылать их друзьям. Тебе дают расшифровку одного шортса. Твоя задача — заголовок, описание и хэштеги, которые дадут максимальный охват.

ЯЗЫК
Всё пишешь на {language}. Никаких вставок на других языках.

ЗАГОЛОВОК
- До {title_max} символов, он же текст на обложке.
- Обещай то, что в видео действительно есть. Обманутый зритель уходит в первые секунды, и охват падает.
- Конкретика вместо интриги: «Три вопроса на собеседовании» работает лучше, чем «Ты не поверишь, что он спросил».
- Без КАПСА, без эмодзи, без «шок», «срочно», «это изменит вашу жизнь».

ОПИСАНИЕ
- Две-четыре строки. Первая строка видна до кнопки «ещё» — в неё выноси главное.
- Дальше можно добавить контекст или задать вопрос зрителю: комментарии тянут охват сильнее лайков.
- Без ссылок и упоминаний чужих аккаунтов.

ХЭШТЕГИ
- Ровно {hashtag_count} штук, без символа #.
- Смешивай по частотности: несколько широких по теме, остальные узкие и конкретные. Из одних популярных охвата не будет — видео утонет.
- Только по существу видео. «viral», «fyp», «рекомендации», «подписка» не работают и выглядят как спам.

{extra}"""

USER_PROMPT = """Расшифровка шортса ({duration:.0f} секунд):

{transcript}

{hook_line}"""


@handler("segment.caption")
def run_caption(ctx: JobContext) -> None:
    segment = ctx.db.get(Segment, ctx.job.segment_id)
    if segment is None:
        raise CaptionError(f"фрагмент {ctx.job.segment_id} не найден")

    config = config_for_segment(ctx.db, segment).get("caption", {})
    transcript = normalize(segment.transcript_text or "")
    if not transcript:
        raise CaptionError(
            "у фрагмента нет расшифровки — генерировать текст не из чего. "
            "Запусти распознавание речи и поиск фрагментов заново"
        )

    provider = build_provider(config.get("provider") or "", config.get("model") or "")
    available, reason = provider.is_available()
    if not available:
        raise CaptionError(f"LLM-провайдер {provider.name} недоступен: {reason}")

    hashtag_count = max(1, int(config.get("hashtag_count", 12) or 12))
    title_max = max(20, int(config.get("title_max_chars", 80) or 80))
    extra = normalize(config.get("extra_instructions", ""))

    ctx.info(f"пишу тексты через {provider.name}/{provider.model}")
    ranges = segment.source_ranges or [[segment.start, segment.end]]
    duration = sum(end - start for start, end in ranges)

    result = provider.generate_json(
        system=SYSTEM_PROMPT.format(
            language=config.get("language") or "de",
            title_max=title_max,
            hashtag_count=hashtag_count,
            extra=("ДОПОЛНИТЕЛЬНО\n" + extra) if extra else "",
        ),
        user=USER_PROMPT.format(
            duration=duration,
            transcript=transcript,
            hook_line=f"Самая сильная фраза: {segment.hook}" if segment.hook else "",
        ),
        schema=SCHEMA,
        schema_name="caption",
        max_tokens=2000,
    )

    title = truncate(normalize(result.get("title", "")), title_max)
    description = normalize(result.get("description", ""))
    hashtags = clean_hashtags(result.get("hashtags") or [])[:hashtag_count]

    if not title:
        raise CaptionError("модель не вернула заголовок")

    # Прежние тексты не выбрасываем молча: если новый вариант хуже, старый
    # видно в логе задачи и его можно вернуть руками.
    ctx.info(f"было: {segment.title_de or '—'}")
    ctx.info(f"стало: {title}")

    segment.title_de = title
    if description:
        segment.description_de = description
    if hashtags:
        segment.hashtags = hashtags
    ctx.db.commit()

    ctx.progress(1.0, "тексты готовы")
    ctx.info(f"хэштегов: {len(hashtags)}")

    # В автопрогоне монтаж передаёт публикацию сюда: ролик должен уйти уже с
    # новым заголовком, а не с тем, что дал поиск фрагментов.
    if ctx.payload.get("auto"):
        from .publish import queue_publications

        full_config = config_for_segment(ctx.db, segment)
        queue_publications(ctx, segment, full_config["publish"])
