"""Процесс бота: опрос Телеграма плюс отчёты о ходе работы.

Запуск:  python -m app.bot
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
import time

from sqlalchemy import select

from ..config import settings
from ..db import SessionLocal, init_db
from ..models import Project, ProjectStatus, Publication, PublicationStatus, Segment, SegmentStatus
from .telegram import STATUS_RU, TelegramBot

log = logging.getLogger("bot")

WATCH_INTERVAL = 8.0
_shutdown = threading.Event()

# О чём вообще стоит писать: промежуточные стадии сыплются часто и только мешают.
REPORTABLE = {ProjectStatus.READY, ProjectStatus.DONE, ProjectStatus.FAILED}


def _summary(db, project: Project) -> str:  # noqa: ANN001
    segments = db.scalars(select(Segment).where(Segment.project_id == project.id)).all()
    published = db.scalars(
        select(Publication)
        .join(Segment, Segment.id == Publication.segment_id)
        .where(Segment.project_id == project.id, Publication.status == PublicationStatus.PUBLISHED)
    ).all()
    scheduled = db.scalars(
        select(Publication)
        .join(Segment, Segment.id == Publication.segment_id)
        .where(Segment.project_id == project.id, Publication.status == PublicationStatus.SCHEDULED)
    ).all()

    rendered = [s for s in segments if s.render_path]
    lines = [
        f"Проект №{project.id}: {STATUS_RU.get(project.status.value, project.status.value)}",
        (project.title or project.source_url)[:80],
        "",
        f"Фрагментов найдено: {len(segments)}",
        f"Смонтировано: {len(rendered)}",
    ]
    if published:
        lines.append(f"Опубликовано: {len(published)}")
    if scheduled:
        nearest = min(p.scheduled_at for p in scheduled if p.scheduled_at)
        lines.append(
            f"Ждут расписания: {len(scheduled)}, ближайшая "
            f"{nearest.astimezone().strftime('%d.%m %H:%M')}"
        )
    if project.error:
        lines += ["", f"Ошибка: {project.error[:400]}"]
    elif project.status == ProjectStatus.READY:
        failed = [s for s in segments if s.status == SegmentStatus.FAILED]
        lines += ["", "Фрагменты ждут ревью в панели."]
        if failed:
            lines.append(f"С ошибкой: {len(failed)}")
    return "\n".join(lines)


def _watch(bot: TelegramBot) -> None:
    """Следит за проектами из Телеграма и пишет в чат при смене состояния."""
    # Стартовое состояние запоминаем молча: иначе после перезапуска бот
    # пересказал бы всё, что уже давно закончилось.
    seen: dict[int, str] = {}
    with SessionLocal() as db:
        for project in db.scalars(select(Project).where(Project.telegram_chat_id != 0)).all():
            seen[project.id] = project.status.value

    while not _shutdown.wait(WATCH_INTERVAL):
        try:
            with SessionLocal() as db:
                projects = db.scalars(
                    select(Project).where(Project.telegram_chat_id != 0)
                ).all()
                for project in projects:
                    current = project.status.value
                    if seen.get(project.id) == current:
                        continue
                    seen[project.id] = current
                    if project.status in REPORTABLE:
                        bot.send(project.telegram_chat_id, _summary(db, project))
        except Exception:  # noqa: BLE001 — наблюдатель не должен ронять бота
            log.exception("сбой наблюдателя за проектами")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stdout,
    )

    token = settings.telegram_bot_token.strip()
    if not token:
        log.error("SHORTS_TELEGRAM_BOT_TOKEN не задан — боту нечем подключаться")
        return 1

    allowed: set[int] = set()
    junk: list[str] = []
    for part in settings.telegram_allowed_chats.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if part.lstrip("-").isdigit():
            allowed.add(int(part))
        else:
            junk.append(part)

    if junk:
        # Частая ошибка: вписывают @username вместо номера. Telegram отдаёт
        # chat.id числом, и по имени сверять не с чем.
        log.error(
            "SHORTS_TELEGRAM_ALLOWED_CHATS: %s — это не номер чата. "
            "Нужно число вроде 123456789, а не имя пользователя. "
            "Узнать номер: напиши боту /id",
            ", ".join(junk),
        )
        return 1

    if not allowed:
        log.error(
            "SHORTS_TELEGRAM_ALLOWED_CHATS пуст — бот никого не пустит. "
            "Напиши боту /id, он подскажет номер этого чата"
        )
        return 1

    init_db()
    bot = TelegramBot(token, allowed)
    me = bot.call("getMe")
    if not me:
        log.error("телеграм не принял токен")
        return 1
    log.info("бот @%s запущен, разрешённых чатов: %s", me.get("username"), len(allowed))

    def _stop(_signum, _frame):  # noqa: ANN001
        log.info("останавливаюсь…")
        _shutdown.set()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    watcher = threading.Thread(target=_watch, args=(bot,), name="bot-watch", daemon=True)
    watcher.start()

    while not _shutdown.is_set():
        try:
            bot.poll_once()
        except Exception:  # noqa: BLE001 — сеть отвалилась, ждём и пробуем снова
            log.exception("сбой опроса телеграма")
            time.sleep(5)

    watcher.join(timeout=10)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
