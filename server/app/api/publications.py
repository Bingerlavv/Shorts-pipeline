"""Публикации: постановка в очередь и статусы."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from ..db import get_db
from ..models import (
    Account,
    Job,
    JobStatus,
    Project,
    Publication,
    PublicationStatus,
    Segment,
    SegmentStatus,
    utcnow,
)
from ..pipeline.scheduling import ACTIVE_STATUSES, preview_slots
from ..queue import enqueue
from ..schemas import PublicationCreate, PublicationOut, SchedulePreview
from ..utils.text import truncate

router = APIRouter(prefix="/api/publications", tags=["publications"])


@router.post("/schedule-preview", response_model=list[datetime])
def schedule_preview(payload: SchedulePreview, db: Session = Depends(get_db)) -> list[datetime]:
    """Ближайшие времена выхода по этим настройкам.

    Считается той же функцией, что и настоящее расписание, — иначе панель
    показывала бы одно, а конвейер делал другое. Если указан аккаунт, в расчёт
    берётся то, что на него уже поставлено: сетка не начинается с чистого листа.
    """
    taken: list[datetime] = []
    if payload.account_id:
        taken = [
            moment
            for moment in db.scalars(
                select(Publication.scheduled_at).where(
                    Publication.account_id == payload.account_id,
                    Publication.status.in_(ACTIVE_STATUSES),
                    Publication.scheduled_at.is_not(None),
                )
            ).all()
            if moment is not None
        ]
    return preview_slots(payload.schedule, payload.count, taken=taken)


def _to_out(publication: Publication) -> PublicationOut:
    data = PublicationOut.model_validate(publication)
    # Номер проекта нужен календарю, чтобы из клетки можно было провалиться к
    # фрагменту. В самой публикации его нет — она знает только фрагмент.
    data.project_id = publication.segment.project_id if publication.segment else None
    return data


@router.get("", response_model=list[PublicationOut])
def list_publications(
    db: Session = Depends(get_db),
    segment_id: int | None = None,
    account_id: int | None = None,
    status: PublicationStatus | None = None,
    # Границы по времени выхода — для календаря. Публикации без времени
    # (уходящие сразу) в такую выборку не попадают: им не место в сетке.
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = Query(200, ge=1, le=1000),
) -> list[PublicationOut]:
    query = select(Publication).options(selectinload(Publication.segment))
    if segment_id is not None:
        query = query.where(Publication.segment_id == segment_id)
    if account_id is not None:
        query = query.where(Publication.account_id == account_id)
    if status is not None:
        query = query.where(Publication.status == status)

    if since is not None or until is not None:
        # Для прошедших публикаций смотрим на фактическое время: запланировано
        # было одно, вышло в другое, а в календаре интереснее правда.
        moment = func.coalesce(Publication.published_at, Publication.scheduled_at)
        query = query.where(moment.is_not(None))
        if since is not None:
            query = query.where(moment >= since)
        if until is not None:
            query = query.where(moment < until)
        query = query.order_by(moment.asc())
    else:
        query = query.order_by(Publication.created_at.desc())

    return [_to_out(p) for p in db.scalars(query.limit(limit)).all()]


@router.post("", response_model=PublicationOut, status_code=201)
def create_publication(
    payload: PublicationCreate, db: Session = Depends(get_db)
) -> PublicationOut:
    segment = _publishable_segment(db, payload.segment_id)

    account = db.get(Account, payload.account_id)
    if account is None:
        raise HTTPException(404, f"аккаунт {payload.account_id} не найден")
    if not account.is_active:
        raise HTTPException(409, f"аккаунт «{account.name}» отключён")

    publication, outcome = send_to_account(
        db,
        segment,
        account,
        title=payload.title,
        description=payload.description,
        privacy=payload.privacy,
        scheduled_at=payload.scheduled_at,
        start_now=payload.start_now,
    )
    if outcome in ("published", "partial", "queued"):
        raise HTTPException(409, _refusal(outcome, publication, account))
    return PublicationOut.model_validate(publication)


@router.post("/segment/{segment_id}", status_code=202)
def publish_segment(segment_id: int, db: Session = Depends(get_db)) -> dict:
    """Отправляет ролик во все аккаунты, привязанные к его проекту.

    Ровно та кнопка, которой не хватало: раньше на каждый ролик приходилось
    тыкать по аккаунту отдельно, а их у проекта может быть шесть.
    """
    segment = _publishable_segment(db, segment_id)
    return send_many(db, segment.project, [segment])


def _publishable_segment(db: Session, segment_id: int) -> Segment:
    segment = db.get(Segment, segment_id)
    if segment is None:
        raise HTTPException(404, f"фрагмент {segment_id} не найден")
    if not segment.render_path or not Path(segment.render_path).exists():
        raise HTTPException(409, "готовый ролик отсутствует — сначала выполни монтаж")
    return segment


def send_many(db: Session, project: Project, segments: list[Segment]) -> dict:
    """Рассылает пачку роликов по привязанным аккаунтам и объясняет итог.

    Пропуски — не ошибка: повторный клик по кнопке не должен ни падать, ни
    отправлять то же самое дважды. Время берётся у общего скедулера, чтобы
    пакет не сел на одну минуту, — интервал и суточный лимит по-прежнему
    настраиваются в пресете.
    """
    from ..pipeline.resolve import config_for_project
    from ..pipeline.scheduling import next_slot

    schedule = config_for_project(db, project).get("publish", {}).get("schedule", {})
    accounts = [a for a in project.accounts if a.is_active]
    if not accounts:
        raise HTTPException(
            409,
            "к проекту не привязан ни один активный аккаунт. Открой «Аккаунты» и "
            "отметь этот проект — или добавь аккаунты в карточке проекта.",
        )

    queued = 0
    skipped: dict[str, int] = {}
    for segment in segments:
        for account in accounts:
            _, outcome = send_to_account(
                db, segment, account, scheduled_at=next_slot(db, account.id, schedule)
            )
            if outcome in ("created", "revived"):
                queued += 1
            else:
                skipped[outcome] = skipped.get(outcome, 0) + 1

    note = f"поставлено в очередь: {queued}"
    labels = {
        "published": "уже опубликовано",
        "queued": "уже в очереди",
        "partial": "оборвалось на площадке, нужна проверка",
    }
    if skipped:
        note += ", пропущено — " + ", ".join(
            f"{labels.get(key, key)}: {value}" for key, value in sorted(skipped.items())
        )
    return {
        "queued": queued,
        "skipped": skipped,
        "accounts": [a.name for a in accounts],
        "note": note,
    }


@router.post("/{publication_id}/retry", status_code=202)
def retry_publication(publication_id: int, db: Session = Depends(get_db)) -> dict:
    publication = _get_publication(db, publication_id)
    if publication.status == PublicationStatus.PUBLISHED:
        raise HTTPException(409, "публикация уже прошла успешно")

    publication.status = PublicationStatus.PENDING
    publication.error = ""
    db.commit()
    job = enqueue(
        db,
        "segment.publish",
        project_id=publication.segment.project_id,
        segment_id=publication.segment_id,
        publication_id=publication.id,
        priority=70,
    )
    return {"job_id": job.id}


@router.delete("/{publication_id}", status_code=204, response_class=Response, response_model=None)
def delete_publication(publication_id: int, db: Session = Depends(get_db)) -> None:
    publication = _get_publication(db, publication_id)
    if publication.status == PublicationStatus.UPLOADING:
        raise HTTPException(409, "публикация выполняется прямо сейчас")
    db.delete(publication)
    db.commit()


def send_to_account(
    db: Session,
    segment: Segment,
    account: Account,
    *,
    title: str = "",
    description: str = "",
    privacy: str = "",
    scheduled_at=None,  # noqa: ANN001 — datetime | None, но Query его не видит
    start_now: bool = True,
) -> tuple[Publication | None, str]:
    """Отправляет один ролик в один аккаунт. Возвращает (публикация, что вышло).

    Одна точка на все пути: кнопка у ролика, «опубликовать всё смонтированное»
    и автопрогон. Раньше правила дублировались, и поведение расходилось —
    например, пакетная отправка не умела оживлять застрявшие строки.

    Что вышло: created | revived | published | partial | queued.
    """
    title = title or truncate(segment.title_de or "Short", 100)
    description = description or "\n\n".join(
        part for part in (segment.description_de, " ".join(segment.hashtags or [])) if part
    )

    existing = db.scalars(
        select(Publication)
        .where(
            Publication.segment_id == segment.id,
            Publication.account_id == account.id,
            Publication.status != PublicationStatus.FAILED,
        )
        .order_by(Publication.id.desc())
    ).first()

    if existing is not None:
        if existing.status == PublicationStatus.PUBLISHED:
            return existing, "published"
        if existing.remote_id:
            return existing, "partial"
        live = db.scalar(
            select(Job.id)
            .where(
                Job.publication_id == existing.id,
                Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
            )
            .limit(1)
        )
        if live:
            return existing, "queued"

        # Задачи нет — строка осиротела. Ставим заново, обновив тексты: с
        # прошлой попытки заголовок могли переписать руками.
        existing.status = PublicationStatus.PENDING
        existing.error = ""
        existing.scheduled_at = scheduled_at
        existing.title = title
        existing.description = description
        segment.status = SegmentStatus.PUBLISHING
        db.commit()
        enqueue(
            db,
            "segment.publish",
            project_id=segment.project_id,
            segment_id=segment.id,
            publication_id=existing.id,
            priority=70,
            run_after=scheduled_at or utcnow(),
        )
        db.refresh(existing)
        return existing, "revived"

    publication = Publication(
        segment_id=segment.id,
        account_id=account.id,
        platform=account.platform,
        title=title,
        description=description,
        privacy=privacy or ("private" if account.platform == "youtube" else "public"),
        scheduled_at=scheduled_at,
        status=(PublicationStatus.SCHEDULED if scheduled_at else PublicationStatus.PENDING),
    )
    db.add(publication)
    db.commit()
    db.refresh(publication)

    if start_now or scheduled_at:
        segment.status = SegmentStatus.PUBLISHING
        enqueue(
            db,
            "segment.publish",
            project_id=segment.project_id,
            segment_id=segment.id,
            publication_id=publication.id,
            priority=70,
            run_after=scheduled_at or utcnow(),
        )
    return publication, "created"


def _refusal(outcome: str, publication: Publication, account: Account) -> str:
    """Человеческое объяснение, почему отправка не состоялась."""
    if outcome == "published":
        where = publication.remote_url or publication.remote_id or "площадке"
        return (
            f"Этот ролик уже опубликован на «{account.name}»: {where}. "
            "На другие аккаунты его можно публиковать как обычно."
        )
    if outcome == "partial":
        return (
            f"Публикация {publication.id} на «{account.name}» оборвалась после того, как "
            f"площадка уже завела запись ({publication.remote_id}). Проверь аккаунт: если "
            "ролика там нет, удали публикацию и отправь заново."
        )
    when = (
        f", отправка назначена на {publication.scheduled_at:%d.%m %H:%M}"
        if publication.scheduled_at
        else ""
    )
    return (
        f"Этот ролик уже стоит в очереди на «{account.name}» "
        f"(публикация {publication.id}{when})."
    )


def _get_publication(db: Session, publication_id: int) -> Publication:
    publication = db.get(Publication, publication_id)
    if publication is None:
        raise HTTPException(404, f"публикация {publication_id} не найдена")
    return publication
