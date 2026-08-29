"""Поток событий для панели (SSE).

Полноценной шины сообщений здесь нет и не нужно: воркер и API пишут в одну
БД, поэтому опрос раз в секунду с отправкой только изменившихся строк даёт
живые индикаторы прогресса без лишней инфраструктуры.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from ..db import SessionLocal
from ..models import Job, JobStatus, Project, Segment

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/events", tags=["events"])

POLL_SECONDS = 1.0
HEARTBEAT_EVERY = 20  # тактов опроса между keep-alive


def _snapshot() -> dict[str, Any]:
    db = SessionLocal()
    try:
        active_jobs = db.scalars(
            select(Job)
            .where(Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]))
            .order_by(Job.id.desc())
            .limit(50)
        ).all()
        recent_jobs = db.scalars(
            select(Job)
            .where(Job.status.in_([JobStatus.FAILED, JobStatus.SUCCEEDED]))
            .order_by(Job.finished_at.desc().nullslast())
            .limit(15)
        ).all()
        projects = db.scalars(
            select(Project).order_by(Project.updated_at.desc()).limit(30)
        ).all()
        segments = db.scalars(
            select(Segment).order_by(Segment.updated_at.desc()).limit(60)
        ).all()

        return {
            "jobs": [
                {
                    "id": job.id,
                    "type": job.type,
                    "status": job.status.value,
                    "progress": round(job.progress, 3),
                    "message": job.message,
                    "error": job.error,
                    "project_id": job.project_id,
                    "segment_id": job.segment_id,
                }
                for job in [*active_jobs, *recent_jobs]
            ],
            "projects": [
                {
                    "id": project.id,
                    "status": project.status.value,
                    "stage_message": project.stage_message,
                    "error": project.error,
                    "title": project.title,
                }
                for project in projects
            ],
            "segments": [
                {
                    "id": segment.id,
                    "project_id": segment.project_id,
                    "status": segment.status.value,
                    "error": segment.error,
                    "has_render": bool(segment.render_path),
                }
                for segment in segments
            ],
        }
    finally:
        db.close()


@router.get("/stream")
async def stream(request: Request) -> StreamingResponse:
    async def generate():
        previous = ""
        ticks = 0
        yield "retry: 3000\n\n"
        while True:
            if await request.is_disconnected():
                break
            try:
                snapshot = await run_in_threadpool(_snapshot)
                payload = json.dumps(snapshot, ensure_ascii=False)
                if payload != previous:
                    previous = payload
                    yield f"event: state\ndata: {payload}\n\n"
                    ticks = 0
                else:
                    ticks += 1
                    if ticks >= HEARTBEAT_EVERY:
                        ticks = 0
                        yield ": keep-alive\n\n"
            except Exception as exc:  # noqa: BLE001 — поток не должен обрываться
                log.exception("сбой в потоке событий")
                yield f"event: error\ndata: {json.dumps({'message': str(exc)})}\n\n"
            await asyncio.sleep(POLL_SECONDS)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
