"""Фрагменты: ревью, правка, рендер, отдача файлов."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Job, JobStatus, Segment, SegmentStatus
from ..pipeline.stages.publish import verify_download_token
from ..queue import enqueue
from ..schemas import SegmentCreate, SegmentOut, SegmentUpdate
from ..utils.text import clean_hashtags

router = APIRouter(prefix="/api/segments", tags=["segments"])


def _get_segment(db: Session, segment_id: int) -> Segment:
    segment = db.get(Segment, segment_id)
    if segment is None:
        raise HTTPException(404, f"фрагмент {segment_id} не найден")
    return segment


@router.get("", response_model=list[SegmentOut])
def list_segments(
    db: Session = Depends(get_db),
    status: SegmentStatus | None = None,
    limit: int = Query(200, ge=1, le=1000),
) -> list[SegmentOut]:
    query = select(Segment).order_by(Segment.updated_at.desc())
    if status is not None:
        query = query.where(Segment.status == status)
    return [SegmentOut.model_validate(s) for s in db.scalars(query.limit(limit)).all()]


@router.post("", response_model=SegmentOut, status_code=201)
def create_segment(payload: SegmentCreate, db: Session = Depends(get_db)) -> SegmentOut:
    """Ручное добавление фрагмента — когда модель пропустила нужный момент."""
    if payload.end <= payload.start:
        raise HTTPException(400, "конец фрагмента должен быть позже начала")

    segment = Segment(
        project_id=payload.project_id,
        start=payload.start,
        end=payload.end,
        source_ranges=[[payload.start, payload.end]],
        title_de=payload.title_de,
        description_de=payload.description_de,
        status=SegmentStatus.APPROVED,
        score=1.0,
        reason="добавлен вручную",
    )
    db.add(segment)
    db.commit()
    db.refresh(segment)
    return SegmentOut.model_validate(segment)


@router.get("/{segment_id}", response_model=SegmentOut)
def get_segment(segment_id: int, db: Session = Depends(get_db)) -> SegmentOut:
    return SegmentOut.model_validate(_get_segment(db, segment_id))


@router.patch("/{segment_id}", response_model=SegmentOut)
def update_segment(
    segment_id: int, payload: SegmentUpdate, db: Session = Depends(get_db)
) -> SegmentOut:
    segment = _get_segment(db, segment_id)
    data = payload.model_dump(exclude_unset=True)

    if "hashtags" in data:
        data["hashtags"] = clean_hashtags(data["hashtags"])
    if "source_ranges" in data and data["source_ranges"]:
        ranges = sorted((float(a), float(b)) for a, b in data["source_ranges"])
        if any(end <= start for start, end in ranges):
            raise HTTPException(400, "в диапазонах конец должен быть позже начала")
        data["source_ranges"] = [[s, e] for s, e in ranges]
        data.setdefault("start", ranges[0][0])
        data.setdefault("end", ranges[-1][1])

    # Сброс времени выхода отдельным флагом: пустое значение в JSON не отличить
    # от «поле не прислали», а перепутать это значит молча потерять настройку.
    if data.pop("clear_publish_at", False):
        data["publish_at"] = None

    for field, value in data.items():
        setattr(segment, field, value)

    if segment.end <= segment.start:
        raise HTTPException(400, "конец фрагмента должен быть позже начала")

    db.commit()
    return SegmentOut.model_validate(segment)


@router.delete("/{segment_id}", status_code=204, response_class=Response, response_model=None)
def delete_segment(segment_id: int, db: Session = Depends(get_db)) -> None:
    db.delete(_get_segment(db, segment_id))
    db.commit()


@router.post("/{segment_id}/approve", response_model=SegmentOut)
def approve_segment(segment_id: int, db: Session = Depends(get_db)) -> SegmentOut:
    segment = _get_segment(db, segment_id)
    segment.status = SegmentStatus.APPROVED
    segment.error = ""
    db.commit()
    return SegmentOut.model_validate(segment)


@router.post("/{segment_id}/reject", response_model=SegmentOut)
def reject_segment(segment_id: int, db: Session = Depends(get_db)) -> SegmentOut:
    segment = _get_segment(db, segment_id)
    segment.status = SegmentStatus.REJECTED
    db.commit()
    return SegmentOut.model_validate(segment)


@router.post("/{segment_id}/caption", status_code=202)
def caption_segment(segment_id: int, db: Session = Depends(get_db)) -> dict:
    """Переписывает заголовок, описание и хэштеги по расшифровке фрагмента."""
    segment = db.get(Segment, segment_id)
    if segment is None:
        raise HTTPException(404, f"фрагмент {segment_id} не найден")
    if not (segment.transcript_text or "").strip():
        raise HTTPException(
            409,
            "У фрагмента нет расшифровки — генерировать текст не из чего.",
        )
    job = enqueue(
        db,
        "segment.caption",
        project_id=segment.project_id,
        segment_id=segment.id,
        priority=60,
    )
    return {"job_id": job.id}


@router.post("/{segment_id}/render", status_code=202)
def render_segment(
    segment_id: int,
    db: Session = Depends(get_db),
    auto_publish: bool = Query(False),
) -> dict:
    segment = _get_segment(db, segment_id)

    running = db.scalar(
        select(Job.id).where(
            Job.segment_id == segment_id,
            Job.type == "segment.render",
            Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
        ).limit(1)
    )
    if running:
        raise HTTPException(409, f"монтаж уже выполняется (задача {running})")

    segment.status = SegmentStatus.APPROVED
    segment.error = ""
    db.commit()
    job = enqueue(
        db,
        "segment.render",
        project_id=segment.project_id,
        segment_id=segment_id,
        priority=60,
        payload={"auto": auto_publish},
    )
    return {"job_id": job.id}


def _serve(path_value: str, media_type: str, filename: str) -> FileResponse:
    if not path_value:
        raise HTTPException(404, "файл ещё не создан")
    path = Path(path_value)
    if not path.exists():
        raise HTTPException(410, "файл был удалён с диска")
    return FileResponse(path, media_type=media_type, filename=filename)


@router.get("/{segment_id}/render")
def download_render(
    segment_id: int,
    db: Session = Depends(get_db),
    token: str = Query("", description="подпись для публичного доступа (Instagram)"),
) -> FileResponse:
    segment = _get_segment(db, segment_id)
    # Панель ходит из локальной сети; Instagram — из интернета, ему нужна подпись.
    if token and not verify_download_token(segment, token):
        raise HTTPException(403, "неверная подпись ссылки")
    return _serve(segment.render_path, "video/mp4", f"short_{segment_id}.mp4")


@router.get("/{segment_id}/clip")
def download_clip(segment_id: int, db: Session = Depends(get_db)) -> FileResponse:
    segment = _get_segment(db, segment_id)
    return _serve(segment.clip_path, "video/mp4", f"clip_{segment_id}.mp4")


@router.get("/{segment_id}/thumb")
def download_thumb(segment_id: int, db: Session = Depends(get_db)) -> FileResponse:
    segment = _get_segment(db, segment_id)
    return _serve(segment.thumb_path, "image/jpeg", f"thumb_{segment_id}.jpg")
