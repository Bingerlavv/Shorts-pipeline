"""Реестр воркеров: кто в парке, что умеет, когда был на связи.

Панель сама задач не выполняет — она видит парк и им управляет. Воркер при
старте называет себя (SHORTS_WORKER_NAME или имя хоста), затем раз в несколько
секунд отмечается: сколько задач тянет, сколько места на диске. Панель по
времени последней отметки решает, живой воркер или отвалился.

Имя — это ключ. За ним закрепляются проекты (на воркере лежат их файлы) и
аккаунты (там профиль браузера), поэтому переименование живого воркера
оторвёт его от собственных данных.
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import socket
from datetime import timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import __version__
from ..config import settings
from ..models import Worker, utcnow

log = logging.getLogger(__name__)

# Идентификатор этого процесса в реестре. В API остаётся None — там ничего не
# исполняется, и привязывать нечего.
_current_id: int | None = None


def current_id() -> int | None:
    return _current_id

# Не отметился дольше — считаем офлайном. Берём с запасом к периоду отметки,
# чтобы одна пропущенная не гасила воркер в панели.
OFFLINE_AFTER = timedelta(seconds=90)
HEARTBEAT_INTERVAL = 20.0


def resolve_name() -> str:
    return (settings.worker_name or socket.gethostname() or "worker").strip()


def resolve_labels() -> list[str]:
    """Что этот воркер умеет. Явный список из .env бьёт автоопределение."""
    manual = [item.strip() for item in settings.worker_labels.split(",") if item.strip()]
    if manual:
        return manual

    labels: list[str] = [platform.system().lower()]
    try:
        import torch  # noqa: PLC0415

        if torch.cuda.is_available():
            labels.append("gpu")
    except Exception:  # noqa: BLE001 — torch может не стоять вовсе
        pass
    try:
        import faster_whisper  # noqa: F401,PLC0415

        labels.append("whisper")
    except Exception:  # noqa: BLE001
        pass
    try:
        import patchright  # noqa: F401,PLC0415

        labels.append("browser")
    except Exception:  # noqa: BLE001
        pass
    return labels


def _disk_free() -> int:
    try:
        return int(shutil.disk_usage(settings.storage_dir).free)
    except Exception:  # noqa: BLE001
        return 0


def register(db: Session) -> Worker:
    """Заводит (или обновляет) запись этого воркера и возвращает её."""
    name = resolve_name()
    worker = db.scalars(select(Worker).where(Worker.name == name)).first()
    if worker is None:
        worker = Worker(name=name)
        db.add(worker)
        log.info("воркер %s регистрируется впервые", name)

    worker.hostname = socket.gethostname()
    worker.public_url = settings.worker_public_url.strip().rstrip("/")
    worker.labels = resolve_labels()
    worker.version = __version__
    worker.concurrency = max(1, settings.worker_concurrency)
    worker.running_jobs = 0
    worker.disk_free = _disk_free()
    worker.last_seen_at = utcnow()
    worker.last_error = ""
    db.commit()
    db.refresh(worker)

    global _current_id
    _current_id = worker.id
    log.info(
        "воркер %s (id %s), умеет: %s, файлы: %s",
        worker.name,
        worker.id,
        ", ".join(worker.labels) or "—",
        worker.public_url or "не отдаёт",
    )
    return worker


def heartbeat(db: Session, worker_id: int, running_jobs: int) -> bool:
    """Отметка «я на связи». False — запись пропала, воркеру пора перерегистрироваться."""
    worker = db.get(Worker, worker_id)
    if worker is None:
        return False
    worker.last_seen_at = utcnow()
    worker.running_jobs = running_jobs
    worker.disk_free = _disk_free()
    db.commit()
    return True


def is_online(worker: Worker) -> bool:
    if worker.last_seen_at is None:
        return False
    seen = worker.last_seen_at
    # SQLite отдаёт время без зоны (типа timestamptz у него нет), Postgres — с
    # зоной. Считаем наивное за UTC: пишем мы всегда utcnow().
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    return utcnow() - seen < OFFLINE_AFTER


def process_tag(index: int) -> str:
    """Строка для Job.claimed_by — чтобы в логе было видно поток и процесс."""
    return f"{resolve_name()}:{os.getpid()}:{index}"
