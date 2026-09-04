"""Проекты: создание из ссылки, просмотр, перезапуск стадий."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..models import Account, Job, JobStatus, Project, ProjectStatus, Segment, SegmentStatus
from ..queue import enqueue
from ..schemas import (
    LinkAccounts,
    ProjectCreate,
    ProjectOut,
    ProjectUpdate,
    SegmentOut,
    TranscriptOut,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/projects", tags=["projects"])

STAGE_JOBS = {
    "ingest": "project.ingest",
    "transcribe": "project.transcribe",
    "analyze": "project.analyze",
    "chunks": "project.chunk",
}

VIDEO_SUFFIXES = {
    ".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".mpg", ".mpeg",
    ".wmv", ".flv", ".ts", ".m2ts",
}


def _classify_source(raw: str) -> tuple[str, str]:
    """Ссылка это или файл на диске. Возвращает (вид источника, источник).

    Проверяем здесь, а не в воркере: человек нажал «Добавить» и смотрит на
    экран — сейчас самое время сказать ему про опечатку в пути. Через минуту
    он уже занят другим, а ошибка будет лежать в журнале задачи.
    """
    if raw.startswith(("http://", "https://")):
        return "url", raw

    path = Path(raw).expanduser()
    if path.is_dir():
        raise HTTPException(
            400,
            f"{path} — это папка. Укажи конкретный файл: проект собирается "
            "из одного исходника.",
        )
    if path.is_file():
        if path.suffix.lower() not in VIDEO_SUFFIXES:
            raise HTTPException(
                400,
                f"{path.name} не похож на видео (расширение {path.suffix or 'отсутствует'}). "
                f"Понимаю такие: {', '.join(sorted(VIDEO_SUFFIXES))}.",
            )
        return "file", str(path.resolve())

    if raw.startswith(("www.", "youtube.com", "youtu.be")):
        raise HTTPException(
            400, f"ссылка должна начинаться с http:// или https:// — попробуй https://{raw}"
        )
    raise HTTPException(
        400,
        f"не пойму, что такое {raw!r}: на ссылку не похоже (нужен http:// или https://), "
        "а файла с таким путём на диске нет",
    )


def _to_out(db: Session, project: Project) -> ProjectOut:
    count = db.scalar(
        select(func.count(Segment.id)).where(Segment.project_id == project.id)
    )
    data = ProjectOut.model_validate(project)
    data.segment_count = int(count or 0)
    data.has_transcript = project.transcript is not None
    data.account_ids = [account.id for account in project.accounts]
    data.worker_name = project.worker.name if project.worker else ""
    return data


def _get_project(db: Session, project_id: int) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, f"проект {project_id} не найден")
    return project


@router.get("", response_model=list[ProjectOut])
def list_projects(
    db: Session = Depends(get_db),
    status: ProjectStatus | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> list[ProjectOut]:
    query = select(Project).order_by(Project.created_at.desc())
    if status is not None:
        query = query.where(Project.status == status)
    projects = db.scalars(query.limit(limit).offset(offset)).all()
    return [_to_out(db, project) for project in projects]


@router.post("", response_model=ProjectOut, status_code=201)
def create_project(payload: ProjectCreate, db: Session = Depends(get_db)) -> ProjectOut:
    source_kind, source = _classify_source(payload.source_url)
    # У файла название взять неоткуда, кроме имени: у ссылки его принесёт yt-dlp.
    title = payload.title or (Path(source).stem if source_kind == "file" else "")

    project = Project(
        source_url=source,
        source_kind=source_kind,
        title=title,
        preset_id=payload.preset_id,
        auto_publish=payload.auto_publish,
        config_overrides=payload.config_overrides,
        status=ProjectStatus.NEW,
    )
    db.add(project)
    db.commit()
    db.refresh(project)

    if payload.start_now:
        enqueue(
            db,
            "project.ingest",
            project_id=project.id,
            payload={"auto": True},
            priority=50,
        )
    return _to_out(db, project)


@router.get("/{project_id}", response_model=ProjectOut)
def get_project(project_id: int, db: Session = Depends(get_db)) -> ProjectOut:
    return _to_out(db, _get_project(db, project_id))


@router.patch("/{project_id}", response_model=ProjectOut)
def update_project(
    project_id: int, payload: ProjectUpdate, db: Session = Depends(get_db)
) -> ProjectOut:
    project = _get_project(db, project_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(project, field, value)
    db.commit()
    return _to_out(db, project)


def _remove_project_files(project: Project) -> int:
    """Стирает всё, что проект оставил на диске.

    Части путей в базе нет: превью проекта называется по его номеру, а рядом с
    каждым роликом монтаж кладёт файл с текстом заголовка и, если включены
    субтитры, .ass. Раньше удалялось только то, что записано в модели, и папки
    копили мусор от давно удалённых проектов.
    """
    targets: set[Path] = set()

    # Скачанный исходник наш и уходит вместе с проектом. Файл, который человек
    # указал сам, лежит где-то у него и нам не принадлежит: удалить его вместе
    # с проектом — потерять чужой материал, которого больше нигде нет.
    if project.video_path and project.source_kind != "file":
        targets.add(Path(project.video_path))
    if project.audio_path:
        targets.add(Path(project.audio_path))

    # Превью проекта: имя собирается из номера, ссылки на него в базе нет.
    targets.add(settings.thumbs_dir / f"project_{project.id}.jpg")

    for segment in project.segments:
        for raw in (segment.clip_path, segment.render_path, segment.thumb_path):
            if not raw:
                continue
            path = Path(raw)
            targets.add(path)
            # Спутники ролика: текст заголовка и субтитры лежат рядом и
            # отличаются только расширением.
            for suffix in (".title.txt", ".ass"):
                targets.add(path.with_suffix("").with_suffix(suffix))
                targets.add(path.parent / f"{path.stem}{suffix}")
            targets.add(settings.clips_dir / f"{path.stem}.ass")

    removed = 0
    for path in targets:
        try:
            if path.is_file():
                path.unlink()
                removed += 1
        except OSError as exc:  # noqa: PERF203 — файл мог быть занят проигрывателем
            log.warning("не удалось удалить %s: %s", path, exc)
    return removed


@router.delete("/{project_id}", status_code=204, response_class=Response, response_model=None)
def delete_project(
    project_id: int,
    db: Session = Depends(get_db),
    delete_files: bool = Query(False, description="удалить и файлы с диска"),
) -> None:
    project = _get_project(db, project_id)

    # Пока воркер держит задачу этого проекта, удалять нельзя: обработчик
    # рухнет на пропавших строках, а файл останется недописанным.
    running = db.scalar(
        select(func.count())
        .select_from(Job)
        .where(Job.project_id == project_id, Job.status == JobStatus.RUNNING)
    )
    if running:
        raise HTTPException(
            409,
            "Проект сейчас обрабатывается. Дождись конца задачи или отмени её в очереди.",
        )

    # Строки задач уйдут каскадом вместе с проектом, но пометить их отменёнными
    # всё равно нужно: иначе воркер успеет захватить задачу между проверкой выше
    # и коммитом удаления. Его захват — это UPDATE ... WHERE status='queued',
    # и после смены статуса он просто не найдёт строку.
    db.execute(
        update(Job)
        .where(Job.project_id == project_id, Job.status == JobStatus.QUEUED)
        .values(status=JobStatus.CANCELLED, message="проект удалён")
    )

    if delete_files:
        _remove_project_files(project)

    db.delete(project)
    db.commit()


@router.post("/{project_id}/publish", status_code=202)
def publish_project(
    project_id: int,
    db: Session = Depends(get_db),
    only_approved: bool = Query(
        False, description="публиковать только взятые в работу, а не все смонтированные"
    ),
) -> dict:
    """Ставит в очередь публикацию всех готовых роликов проекта.

    Куда — решает связь «аккаунт ↔ проект», а не пресет: из пресета берутся
    только оформление и расписание. Уже отправленное пропускается, поэтому
    повторный клик безопасен.
    """
    from .publications import send_many

    project = _get_project(db, project_id)
    wanted = (
        {SegmentStatus.APPROVED}
        if only_approved
        else {
            SegmentStatus.RENDERED,
            SegmentStatus.APPROVED,
            SegmentStatus.PUBLISHING,
            SegmentStatus.PUBLISHED,
            SegmentStatus.FAILED,
        }
    )
    ready = [
        segment
        for segment in sorted(project.segments, key=lambda item: item.start)
        if segment.status in wanted
        and segment.render_path
        and Path(segment.render_path).is_file()
    ]
    if not ready:
        raise HTTPException(
            409,
            "Нет ни одного смонтированного ролика. Сначала возьми фрагменты в работу "
            "и нажми «Смонтировать взятые в работу».",
        )
    return send_many(db, project, ready)


@router.get("/{project_id}/segments", response_model=list[SegmentOut])
def project_segments(
    project_id: int,
    db: Session = Depends(get_db),
    status: SegmentStatus | None = None,
) -> list[SegmentOut]:
    _get_project(db, project_id)
    query = select(Segment).where(Segment.project_id == project_id)
    if status is not None:
        query = query.where(Segment.status == status)
    segments = db.scalars(query.order_by(Segment.score.desc(), Segment.start)).all()
    return [SegmentOut.model_validate(s) for s in segments]


@router.get("/{project_id}/transcript", response_model=TranscriptOut)
def project_transcript(project_id: int, db: Session = Depends(get_db)) -> TranscriptOut:
    project = _get_project(db, project_id)
    if project.transcript is None:
        raise HTTPException(404, "транскрипт ещё не готов")
    return TranscriptOut.model_validate(project.transcript)


@router.put("/{project_id}/accounts", response_model=ProjectOut)
def set_project_accounts(
    project_id: int, payload: LinkAccounts, db: Session = Depends(get_db)
) -> ProjectOut:
    """Та же связь «аккаунт ↔ проект», только со стороны проекта.

    Настраивать удобнее от аккаунта — он живёт дольше и меняется реже. Но когда
    проект уже открыт, лишний переход на другую страницу ради одной галочки
    раздражает, поэтому связь правится и отсюда.
    """
    project = _get_project(db, project_id)
    wanted = list(dict.fromkeys(payload.account_ids))

    found = db.scalars(select(Account).where(Account.id.in_(wanted or [-1]))).all()
    missing = set(wanted) - {account.id for account in found}
    if missing:
        raise HTTPException(404, f"аккаунты не найдены: {sorted(missing)}")

    project.accounts = list(found)
    db.commit()
    return _to_out(db, project)


@router.post("/{project_id}/run/{stage}", status_code=202)
def run_stage(
    project_id: int,
    stage: str,
    db: Session = Depends(get_db),
    auto: bool = Query(True, description="продолжать конвейер дальше автоматически"),
) -> dict:
    project = _get_project(db, project_id)
    job_type = STAGE_JOBS.get(stage)
    if job_type is None:
        raise HTTPException(400, f"неизвестная стадия: {stage}. Доступны: {', '.join(STAGE_JOBS)}")

    if stage == "transcribe" and not project.video_path:
        raise HTTPException(409, "исходник ещё не загружен")
    if stage == "analyze" and project.transcript is None:
        raise HTTPException(409, "нет транскрипта")
    if stage == "chunks" and project.duration <= 0:
        raise HTTPException(409, "исходник ещё не загружен — длительность неизвестна")

    running = db.scalar(
        select(Job.id).where(
            Job.project_id == project_id,
            Job.type == job_type,
            Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
        ).limit(1)
    )
    if running:
        raise HTTPException(409, f"стадия {stage} уже выполняется (задача {running})")

    project.error = ""
    db.commit()
    job = enqueue(db, job_type, project_id=project_id, payload={"auto": auto}, priority=50)
    return {"job_id": job.id, "type": job.type}


@router.post("/{project_id}/render-approved", status_code=202)
def render_approved(project_id: int, db: Session = Depends(get_db)) -> dict:
    """Ставит в рендер все одобренные фрагменты проекта."""
    project = _get_project(db, project_id)
    targets = [
        s for s in project.segments
        if s.status in (SegmentStatus.APPROVED, SegmentStatus.FAILED)
    ]
    if not targets:
        raise HTTPException(409, "нет одобренных фрагментов — сначала отметь нужные")

    job_ids = []
    for segment in targets:
        segment.status = SegmentStatus.APPROVED
        segment.error = ""
        job = enqueue(
            db,
            "segment.render",
            project_id=project_id,
            segment_id=segment.id,
            priority=60,
            payload={"auto": project.auto_publish},
            commit=False,
        )
        job_ids.append(job.id)
    db.commit()
    return {"queued": len(job_ids), "job_ids": job_ids}
