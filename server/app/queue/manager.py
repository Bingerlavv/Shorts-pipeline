"""Очередь задач поверх основной БД.

Redis намеренно не используется: одна машина, десятки задач в час, а SQLite в
режиме WAL с атомарным UPDATE ... WHERE status='queued' даёт корректный захват
задачи несколькими воркерами.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, update
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
    run_after: datetime | None = None,
    max_attempts: int = 2,
    commit: bool = True,
) -> Job:
    job = Job(
        type=job_type,
        payload=payload or {},
        priority=priority,
        project_id=project_id,
        segment_id=segment_id,
        publication_id=publication_id,
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


def claim_next(db: Session, worker_id: str) -> Job | None:
    """Атомарно забирает следующую задачу. None, если очередь пуста."""
    now = utcnow()
    candidate_ids = db.scalars(
        select(Job.id)
        .where(Job.status == JobStatus.QUEUED, Job.run_after <= now)
        .order_by(Job.priority.asc(), Job.id.asc())
        .limit(5)
    ).all()

    for job_id in candidate_ids:
        result = db.execute(
            update(Job)
            .where(Job.id == job_id, Job.status == JobStatus.QUEUED)
            .values(
                status=JobStatus.RUNNING,
                claimed_by=worker_id,
                started_at=now,
                attempts=Job.attempts + 1,
            )
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


def reset_running_on_boot(db: Session) -> int:
    """Воркер стартует: всё, что числится RUNNING, осталось от прошлого процесса."""
    result = db.execute(
        update(Job)
        .where(Job.status == JobStatus.RUNNING)
        .values(status=JobStatus.QUEUED, claimed_by="", message="возвращено в очередь при старте")
    )
    db.commit()
    return result.rowcount
