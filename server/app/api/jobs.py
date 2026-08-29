"""Очередь задач: просмотр, отмена, повтор."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import (
    Job,
    JobStatus,
    Project,
    ProjectStatus,
    Segment,
    SegmentStatus,
)
from ..queue import cancel as cancel_job
from ..queue import enqueue
from ..schemas import JobDetail, JobOut

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


@router.get("", response_model=list[JobOut])
def list_jobs(
    db: Session = Depends(get_db),
    status: JobStatus | None = None,
    project_id: int | None = None,
    limit: int = Query(100, ge=1, le=500),
) -> list[JobOut]:
    query = select(Job).order_by(Job.id.desc())
    if status is not None:
        query = query.where(Job.status == status)
    if project_id is not None:
        query = query.where(Job.project_id == project_id)
    return [JobOut.model_validate(j) for j in db.scalars(query.limit(limit)).all()]


@router.get("/summary")
def jobs_summary(db: Session = Depends(get_db)) -> dict[str, int]:
    rows = db.execute(select(Job.status, func.count(Job.id)).group_by(Job.status)).all()
    summary = {status.value: 0 for status in JobStatus}
    for status, count in rows:
        summary[status.value if hasattr(status, "value") else str(status)] = int(count)
    return summary


@router.get("/{job_id}", response_model=JobDetail)
def get_job(job_id: int, db: Session = Depends(get_db)) -> JobDetail:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(404, f"задача {job_id} не найдена")
    return JobDetail.model_validate(job)


def _release_publication(db: Session, job: Job) -> None:
    """Отменённая задача публикации не должна запирать ролик навсегда.

    Строка публикации живёт отдельно от задачи. Раньше она оставалась в
    PENDING, и повторная отправка того же ролика на тот же аккаунт получала
    «уже отправлен» — при том, что не ушло ничего.
    """
    if job.type != "segment.publish" or not job.publication_id:
        return
    from ..pipeline.stages.publish import release_publication

    release_publication(db, job.publication_id, "отменено вручную — ролик не отправлен")


@router.post("/{job_id}/cancel", status_code=200)
def cancel(job_id: int, db: Session = Depends(get_db)) -> dict:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(404, f"задача {job_id} не найдена")

    if cancel_job(db, job_id):
        _release_publication(db, job)
        return {"cancelled": True, "note": "задача снята из очереди"}

    if job.status == JobStatus.RUNNING:
        # Воркер проверяет статус на контрольных точках и остановится сам.
        job.status = JobStatus.CANCELLED
        db.commit()
        # Публикацию отпускаем сразу, не дожидаясь воркера: строку задачи
        # можно тут же убрать кнопкой «Очистить завершённые», и тогда
        # освобождать публикацию будет уже некому и неоткуда.
        _release_publication(db, job)
        return {"cancelled": True, "note": "задача остановится на ближайшей контрольной точке"}

    raise HTTPException(409, f"задачу в статусе {job.status.value} отменить нельзя")


@router.post("/{job_id}/retry", status_code=202)
def retry(job_id: int, db: Session = Depends(get_db)) -> dict:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(404, f"задача {job_id} не найдена")
    if job.status in (JobStatus.QUEUED, JobStatus.RUNNING):
        raise HTTPException(409, "задача ещё не завершилась")

    clone = enqueue(
        db,
        job.type,
        payload=job.payload,
        priority=job.priority,
        project_id=job.project_id,
        segment_id=job.segment_id,
        publication_id=job.publication_id,
    )
    return {"job_id": clone.id}


@router.delete("/completed", status_code=200)
def clear_completed(db: Session = Depends(get_db)) -> dict:
    result = db.query(Job).filter(
        Job.status.in_([JobStatus.SUCCEEDED, JobStatus.CANCELLED])
    ).delete(synchronize_session=False)
    db.commit()
    return {"deleted": int(result)}


@router.delete("/failed", status_code=200)
def clear_failed(db: Session = Depends(get_db)) -> dict:
    """Убирает проваленные задачи и снимает ошибки, которые они оставили.

    Одной строки в очереди мало: та же ошибка продублирована в проекте и во
    фрагменте, и без очистки она продолжает висеть в карточке.
    """
    failed = db.query(Job).filter(Job.status == JobStatus.FAILED).all()
    project_ids = {job.project_id for job in failed if job.project_id}
    segment_ids = {job.segment_id for job in failed if job.segment_id}

    for job in failed:
        db.delete(job)

    for project in db.query(Project).filter(Project.id.in_(project_ids or [-1])).all():
        project.error = ""
        if project.status == ProjectStatus.FAILED:
            # Статус пересобираем по факту: есть фрагменты — значит, дошли до ревью.
            project.status = ProjectStatus.READY if project.segments else ProjectStatus.NEW
    for segment in db.query(Segment).filter(Segment.id.in_(segment_ids or [-1])).all():
        segment.error = ""
        if segment.status == SegmentStatus.FAILED:
            segment.status = (
                SegmentStatus.RENDERED if segment.render_path else SegmentStatus.CANDIDATE
            )

    db.commit()
    return {"deleted": len(failed)}
