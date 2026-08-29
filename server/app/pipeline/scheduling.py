"""Расчёт времени публикации.

Ключевое правило: интервал соблюдается **на аккаунт**, а не на ролик. Один и
тот же шортс может уйти на три площадки одновременно — это нормально. А вот два
разных ролика на один аккаунт подряд выглядят как спам, и площадки это тоже не
любят.

Расписание бывает двух видов. «Интервал» разносит ролики на N минут друг от
друга внутри окна суток. «По часам» ставит их в названные часы — 10:00, 15:00,
20:00, — и именно так люди обычно думают о сетке выхода.

Расчёт вынесен в чистую функцию compute_slot: она не ходит в базу, поэтому одним
и тем же кодом считается и настоящее время публикации, и предпросмотр в панели.
Иначе предпросмотр показывал бы одно, а конвейер делал другое.
"""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Publication, PublicationStatus, utcnow

log = logging.getLogger(__name__)

# Публикации, которые занимают слот: провалившиеся не в счёт, их переотправляют.
ACTIVE_STATUSES = (
    PublicationStatus.PENDING,
    PublicationStatus.SCHEDULED,
    PublicationStatus.UPLOADING,
    PublicationStatus.PUBLISHED,
)

# Дальше года вперёд не заглядываем: столько дней подряд занятыми быть не может,
# а если оказались — настройка бессмысленна, и об этом стоит сказать в журнал.
SEARCH_DAYS = 366


def parse_clock(raw: str) -> time | None:
    """Читает «10:00». Пустая строка или мусор — времени нет."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        hours, _, minutes = raw.partition(":")
        return time(int(hours), int(minutes or 0))
    except (TypeError, ValueError):
        log.warning("не разобрал время в расписании: %r", raw)
        return None


def _aware(moment: datetime, reference) -> datetime:  # noqa: ANN001
    """SQLite отдаёт наивные значения — приводим их к той же зоне, что и остальные."""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=reference)


def _local_midnight(moment: datetime) -> datetime:
    return moment.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)


def _next_local_day(day: datetime) -> datetime:
    """Следующие местные сутки.

    Через UTC — чтобы на переходе на летнее время смещение пересчиталось, а не
    осталось от вчерашнего дня.
    """
    return (day + timedelta(days=1)).astimezone(timezone.utc).astimezone()


def _at(day: datetime, moment: time) -> datetime:
    return day.replace(hour=moment.hour, minute=moment.minute, second=0, microsecond=0)


def _same_local_day(moment: datetime, day: datetime) -> bool:
    return moment.astimezone().date() == day.date()


def _apply_window(moment: datetime, start: time | None, end: time | None) -> datetime:
    """Двигает момент внутрь разрешённого окна суток.

    Время местное: панель работает на машине пользователя, и «постить с 10 до 22»
    он имеет в виду по своим часам, а не по UTC.
    """
    if start is None and end is None:
        return moment

    local = moment.astimezone()
    day_start = _at(local, start or time(0, 0))

    if start is not None and local < day_start:
        local = day_start

    if end is not None:
        day_end = _at(local, end)
        # Окно через полночь (22:00–02:00) не поддерживаем: слишком легко
        # ошибиться в настройке, а выигрыш сомнительный.
        if end > (start or time(0, 0)) and local > day_end:
            local = _next_local_day(day_start) if start is not None else day_end
    return local


def compute_slot(
    taken: list[datetime], schedule: dict, *, now: datetime | None = None
) -> datetime | None:
    """Когда уйдёт следующая публикация, если моменты из taken уже заняты.

    None — расписание выключено, публикуем сразу.
    """
    if not schedule.get("enabled"):
        return None

    now = now or utcnow()
    offset = max(0, int(schedule.get("start_offset_minutes", 0) or 0))
    daily_limit = max(0, int(schedule.get("daily_limit", 0) or 0))
    weekdays = {
        int(day) for day in (schedule.get("weekdays") or []) if str(day).strip().isdigit()
    } & set(range(1, 8))

    busy = sorted(_aware(moment, now.tzinfo) for moment in taken if moment is not None)
    earliest = now + timedelta(minutes=offset)

    times = sorted(filter(None, (parse_clock(raw) for raw in (schedule.get("times") or []))))
    mode = (schedule.get("mode") or "").strip().lower()
    if mode not in ("times", "spacing"):
        mode = "times" if times else "spacing"
    if mode == "times" and not times:
        # Режим «по часам» без единого часа — это не расписание, а пустота.
        # Откатиться к интервалу честнее, чем молча publish сразу пачкой.
        log.warning("расписание по часам без времён — считаю по интервалу")
        mode = "spacing"

    def day_is_full(day: datetime) -> bool:
        if not daily_limit:
            return False
        return sum(1 for moment in busy if _same_local_day(moment, day)) >= daily_limit

    if mode == "spacing":
        spacing = max(0, int(schedule.get("spacing_minutes", 0) or 0))
        if busy:
            earliest = max(earliest, busy[-1] + timedelta(minutes=spacing))

        window_start = parse_clock(schedule.get("window_start", ""))
        window_end = parse_clock(schedule.get("window_end", ""))

        moment = earliest
        for _ in range(SEARCH_DAYS):
            moment = _apply_window(moment, window_start, window_end)
            day = _local_midnight(moment)
            if (not weekdays or day.isoweekday() in weekdays) and not day_is_full(day):
                return moment.astimezone(now.tzinfo)
            moment = _at(_next_local_day(day), window_start or time(0, 0))
        log.warning("не нашёл свободный день за год вперёд")
        return earliest.astimezone(now.tzinfo)

    day = _local_midnight(earliest)
    for _ in range(SEARCH_DAYS):
        if (not weekdays or day.isoweekday() in weekdays) and not day_is_full(day):
            used = sum(1 for moment in busy if _same_local_day(moment, day))
            for clock in times:
                if daily_limit and used >= daily_limit:
                    break
                moment = _at(day, clock)
                if moment < earliest.astimezone():
                    continue
                # Час уже занят другой публикацией этого аккаунта — берём следующий.
                if any(abs((moment - other).total_seconds()) < 60 for other in busy):
                    continue
                return moment.astimezone(now.tzinfo)
        day = _next_local_day(day)

    log.warning("не нашёл свободный час за год вперёд")
    return earliest.astimezone(now.tzinfo)


def next_slot(db: Session, account_id: int, schedule: dict) -> datetime | None:
    """То же самое, но занятые моменты берутся из базы по конкретному аккаунту."""
    if not schedule.get("enabled"):
        return None

    taken = db.scalars(
        select(Publication.scheduled_at).where(
            Publication.account_id == account_id,
            Publication.status.in_(ACTIVE_STATUSES),
            Publication.scheduled_at.is_not(None),
        )
    ).all()
    return compute_slot(list(taken), schedule)


def preview_slots(
    schedule: dict, count: int = 8, *, taken: list[datetime] | None = None
) -> list[datetime]:
    """Ближайшие count моментов подряд — для предпросмотра в панели."""
    busy = list(taken or [])
    result: list[datetime] = []
    for _ in range(max(0, count)):
        moment = compute_slot(busy, schedule)
        if moment is None:
            break
        result.append(moment)
        busy.append(moment)
    return result
