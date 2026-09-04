"""Парк воркеров: кто на связи, что делает, кого выключить.

Панель ничего не исполняет сама — она смотрит на реестр и управляет им.
Записи заводят сами воркеры при старте, отсюда их можно только включить,
выключить или удалить отвалившийся.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Account, Job, JobStatus, Project, Worker
from ..queue.fleet import is_online
from ..schemas import WorkerOut

router = APIRouter(prefix="/api/workers", tags=["workers"])


def _out(db: Session, worker: Worker) -> WorkerOut:
    data = WorkerOut.model_validate(worker)
    data.online = is_online(worker)
    data.projects = db.scalar(
        select(func.count(Project.id)).where(Project.worker_id == worker.id)
    ) or 0
    data.accounts = db.scalar(
        select(func.count(Account.id)).where(Account.worker_id == worker.id)
    ) or 0
    data.queued = db.scalar(
        select(func.count(Job.id)).where(
            Job.worker_id == worker.id, Job.status == JobStatus.QUEUED
        )
    ) or 0
    return data


@router.get("", response_model=list[WorkerOut])
def list_workers(db: Session = Depends(get_db)) -> list[WorkerOut]:
    workers = db.scalars(select(Worker).order_by(Worker.name)).all()
    return [_out(db, worker) for worker in workers]


@router.post("/{worker_id}/toggle", response_model=WorkerOut)
def toggle_worker(worker_id: int, db: Session = Depends(get_db)) -> WorkerOut:
    """Выключенный воркер доделывает текущее и больше ничего не берёт."""
    worker = _get(db, worker_id)
    worker.is_enabled = not worker.is_enabled
    db.commit()
    db.refresh(worker)
    return _out(db, worker)


@router.delete("/{worker_id}", status_code=204, response_class=Response, response_model=None)
def delete_worker(worker_id: int, db: Session = Depends(get_db), force: bool = False) -> None:
    """Убирает воркера из реестра.

    Живого удалять нечего: он зарегистрируется снова на следующей отметке.
    А вот у отвалившегося на руках могут остаться проекты — их файлы лежат
    только на нём, поэтому без force не отпускаем.
    """
    worker = _get(db, worker_id)
    if is_online(worker) and not force:
        raise HTTPException(
            409,
            f"воркер «{worker.name}» на связи — он просто зарегистрируется заново. "
            "Сначала выключи его кнопкой или останови процесс.",
        )

    holding = db.scalar(
        select(func.count(Project.id)).where(Project.worker_id == worker_id)
    ) or 0
    if holding and not force:
        raise HTTPException(
            409,
            f"за воркером «{worker.name}» числится проектов: {holding}. Их файлы лежат "
            "только на этой машине — после удаления панель перестанет их показывать, "
            "а задачи по ним смогут уйти на любой воркер.",
        )

    db.delete(worker)
    db.commit()


@router.get("/{worker_id}/jobs")
def worker_jobs(worker_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Что этот воркер сейчас тянет — для карточки в панели."""
    worker = _get(db, worker_id)
    running = db.scalars(
        select(Job)
        .where(Job.status == JobStatus.RUNNING, Job.claimed_by.like(f"{worker.name}:%"))
        .order_by(Job.id.desc())
        .limit(20)
    ).all()
    return {
        "running": [
            {
                "id": job.id,
                "type": job.type,
                "progress": job.progress,
                "message": job.message,
                "project_id": job.project_id,
            }
            for job in running
        ]
    }


def _get(db: Session, worker_id: int) -> Worker:
    worker = db.get(Worker, worker_id)
    if worker is None:
        raise HTTPException(404, f"воркер {worker_id} не найден")
    return worker
