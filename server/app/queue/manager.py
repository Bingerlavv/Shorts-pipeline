"""Очередь задач поверх основной БД.

Redis намеренно не используется. На одной машине хватает SQLite в режиме WAL:
атомарный UPDATE ... WHERE status='queued' корректно разводит воркеров. Когда
воркеры разъезжаются по машинам, та же схема переезжает в Postgres — там захват
идёт через SELECT ... FOR UPDATE SKIP LOCKED, чтобы десяток воркеров не толкался
за одну и ту же строку.

У задачи есть привязка к воркеру: worker_id пусто — возьмёт любой; заполнено —
только этот. Так задачи проекта остаются на машине, где лежат его файлы, а
публикация уходит туда, где стоит профиль браузера.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from ..models import Job, JobStatus, utcnow
from ..utils.text import strip_ansi

log = logging.getLogger(__name__)

MAX_LOG_CHARS = 60_000


def enqueue(
    db: Session,
    job_type: str,
    payload: dict[str, Any] | None = None,
    *,
    priority: int = 100,
    project_id: int | None = None,
    segment_id: int | None = None,
    publication_id: int | None = None,
    worker_id: int | None = None,
    run_after: datetime | None = None,
    max_attempts: int = 2,
    commit: bool = True,
) -> Job:
    if worker_id is None:
        worker_id = _affinity(db, project_id=project_id, publication_id=publication_id)

    job = Job(
        type=job_type,
        payload=payload or {},
        priority=priority,
        project_id=project_id,
        segment_id=segment_id,
        publication_id=publication_id,
        worker_id=worker_id,
        run_after=run_after or utcnow(),
        max_attempts=max_attempts,
    )
    db.add(job)
    if commit:
        db.commit()
        db.refresh(job)
    else:
        db.flush()
    return job


def _affinity(
    db: Session, *, project_id: int | None, publication_id: int | None
) -> int | None:
    """К какому воркеру привязать задачу, если явно не сказали.

    Публикация идёт туда, где профиль аккаунта; всё остальное по проекту — туда,
    где лежат его файлы. Загрузка исходника ещё ни к кому не привязана: проект
    получает воркера как раз на ней.
    """
    from ..models import Account, Project, Publication

    if publication_id is not None:
        publication = db.get(Publication, publication_id)
        if publication is not None:
            account = db.get(Account, publication.account_id)
            if account is not None and account.worker_id:
                return account.worker_id

    if project_id is not None:
        project = db.get(Project, project_id)
        if project is not None and project.worker_id:
            return project.worker_id
    return None


def claim_next(db: Session, process_tag: str, worker_id: int | None = None) -> Job | None:
    """Атомарно забирает следующую задачу. None, если брать нечего.

    ``process_tag`` — человекочитаемая метка потока для журнала, ``worker_id`` —
    запись воркера в реестре: по ней отсеиваются чужие привязанные задачи.
    """
    now = utcnow()
    # Ничья задача достаётся любому; привязанная — только своему воркеру.
    affinity = Job.worker_id.is_(None)
    if worker_id is not None:
        affinity = or_(affinity, Job.worker_id == worker_id)

    ready = (
        Job.status == JobStatus.QUEUED,
        Job.run_after <= now,
        affinity,
    )
    taken = {
        "status": JobStatus.RUNNING,
        "claimed_by": process_tag,
        "started_at": now,
        "attempts": Job.attempts + 1,
    }

    if db.bind is not None and db.bind.dialect.name == "postgresql":
        # SKIP LOCKED пропускает строки, которые уже держит другой воркер, —
        # иначе на общей БД все толкаются за одну и ту же верхнюю задачу.
        candidate = (
            select(Job.id)
            .where(*ready)
            .order_by(Job.priority.asc(), Job.id.asc())
            .limit(1)
            .with_for_update(skip_locked=True)
            .scalar_subquery()
        )
        job_id = db.execute(
            update(Job).where(Job.id == candidate).values(**taken).returning(Job.id)
        ).scalar_one_or_none()
        db.commit()
        return db.get(Job, job_id) if job_id is not None else None

    # SQLite: одна пишущая транзакция за раз, хватает проверки статуса в WHERE.
    candidate_ids = db.scalars(
        select(Job.id).where(*ready).order_by(Job.priority.asc(), Job.id.asc()).limit(5)
    ).all()
    for job_id in candidate_ids:
        result = db.execute(
            update(Job)
            .where(Job.id == job_id, Job.status == JobStatus.QUEUED)
            .values(**taken)
        )
        db.commit()
        if result.rowcount == 1:
            return db.get(Job, job_id)
    return None


def report_progress(db: Session, job_id: int, progress: float, message: str = "") -> None:
    values: dict[str, Any] = {"progress": max(0.0, min(1.0, progress))}
    if message:
        values["message"] = message[:500]
    db.execute(update(Job).where(Job.id == job_id).values(**values))
    db.commit()


def append_log(db: Session, job_id: int, line: str) -> None:
    job = db.get(Job, job_id)
    if job is None:
        return
    stamped = f"[{datetime.now(timezone.utc):%H:%M:%S}] {line.rstrip()}\n"
    combined = (job.log or "") + stamped
    if len(combined) > MAX_LOG_CHARS:
        combined = "…\n" + combined[-MAX_LOG_CHARS:]
    job.log = combined
    db.commit()


def finish(db: Session, job_id: int, *, error: str | None = None) -> None:
    job = db.get(Job, job_id)
    if job is None:
        return
    if error is not None:
        error = strip_ansi(error)
    if error is None:
        job.status = JobStatus.SUCCEEDED
        job.progress = 1.0
        job.error = ""
    elif job.attempts < job.max_attempts:
        # Повтор с нарастающей задержкой.
        job.status = JobStatus.QUEUED
        job.error = error[:4000]
        job.run_after = utcnow() + timedelta(seconds=30 * job.attempts)
        job.claimed_by = ""
    else:
        job.status = JobStatus.FAILED
        job.error = error[:4000]
    job.finished_at = utcnow()
    db.commit()


def cancel(db: Session, job_id: int) -> bool:
    """Отменяет задачу, если она ещё не начала выполняться."""
    result = db.execute(
        update(Job)
        .where(Job.id == job_id, Job.status == JobStatus.QUEUED)
        .values(status=JobStatus.CANCELLED, finished_at=utcnow())
    )
    db.commit()
    return result.rowcount == 1


def requeue_stale(db: Session, older_than_minutes: int = 120) -> int:
    """Возвращает в очередь задачи, зависшие после падения воркера."""
    cutoff = utcnow() - timedelta(minutes=older_than_minutes)
    result = db.execute(
        update(Job)
        .where(Job.status == JobStatus.RUNNING, Job.started_at < cutoff)
        .values(status=JobStatus.QUEUED, claimed_by="", message="перезапуск после сбоя воркера")
    )
    db.commit()
    return result.rowcount


def reset_running_on_boot(db: Session, claimed_prefix: str) -> int:
    """Воркер стартует: его собственные RUNNING остались от прошлого процесса.

    Чужие не трогаем — в общей БД это задачи живых воркеров на других машинах,
    и сброс отобрал бы у них работу на середине.
    """
    result = db.execute(
        update(Job)
        .where(
            Job.status == JobStatus.RUNNING,
            Job.claimed_by.like(f"{claimed_prefix}%"),
        )
        .values(status=JobStatus.QUEUED, claimed_by="", message="возвращено в очередь при старте")
    )
    db.commit()
    return result.rowcount
