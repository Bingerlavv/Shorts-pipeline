"""Схема данных конвейера."""

from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Куда уходят ролики проекта. Связь живёт отдельной таблицей, а не полем в
# конфиге: аккаунт переживает любой проект, и назначать его заново на каждом
# новом видео — это одна и та же работа по пять раз. Настраивается со стороны
# аккаунта («что публикуем сюда»), но читается и с обеих сторон.
account_projects = Table(
    "account_projects",
    Base.metadata,
    Column(
        "account_id",
        ForeignKey("accounts.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "project_id",
        ForeignKey("projects.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


class ProjectStatus(str, enum.Enum):
    NEW = "new"
    DOWNLOADING = "downloading"
    TRANSCRIBING = "transcribing"
    ANALYZING = "analyzing"
    READY = "ready"          # фрагменты найдены, ждут ревью
    RENDERING = "rendering"
    DONE = "done"
    FAILED = "failed"


class SegmentStatus(str, enum.Enum):
    CANDIDATE = "candidate"
    APPROVED = "approved"
    REJECTED = "rejected"
    RENDERING = "rendering"
    RENDERED = "rendered"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    FAILED = "failed"


class JobStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PublicationStatus(str, enum.Enum):
    PENDING = "pending"
    SCHEDULED = "scheduled"
    UPLOADING = "uploading"
    PUBLISHED = "published"
    FAILED = "failed"


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(500), default="")
    source_url: Mapped[str] = mapped_column(String(2000))
    source_kind: Mapped[str] = mapped_column(String(32), default="url")

    status: Mapped[ProjectStatus] = mapped_column(
        Enum(ProjectStatus, native_enum=False), default=ProjectStatus.NEW, index=True
    )
    stage_message: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")

    video_path: Mapped[str] = mapped_column(String(1000), default="")
    audio_path: Mapped[str] = mapped_column(String(1000), default="")
    duration: Mapped[float] = mapped_column(Float, default=0.0)
    width: Mapped[int] = mapped_column(Integer, default=0)
    height: Mapped[int] = mapped_column(Integer, default=0)
    fps: Mapped[float] = mapped_column(Float, default=0.0)
    has_burned_subtitles: Mapped[bool | None] = mapped_column(Boolean, default=None)

    preset_id: Mapped[int | None] = mapped_column(ForeignKey("presets.id", ondelete="SET NULL"))
    # Точечные правки конвейера именно для этого видео — накладываются поверх пресета.
    config_overrides: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    auto_publish: Mapped[bool] = mapped_column(Boolean, default=False)
    # Чат, из которого пришла ссылка: туда бот шлёт отчёт о ходе работы.
    # 0 — проект заведён из панели.
    telegram_chat_id: Mapped[int] = mapped_column(Integer, default=0)
    source_meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # На какой машине лежат файлы проекта. Ставится на стадии загрузки и
    # дальше держит все его задачи на том же воркере.
    worker_id: Mapped[int | None] = mapped_column(
        ForeignKey("workers.id", ondelete="SET NULL"), index=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    preset: Mapped["Preset | None"] = relationship(back_populates="projects")
    transcript: Mapped["Transcript | None"] = relationship(
        back_populates="project", cascade="all, delete-orphan", uselist=False
    )
    segments: Mapped[list["Segment"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", order_by="Segment.start"
    )
    accounts: Mapped[list["Account"]] = relationship(
        secondary=account_projects, back_populates="projects", order_by="Account.name"
    )
    worker: Mapped["Worker | None"] = relationship(lazy="joined")


class Transcript(Base):
    __tablename__ = "transcripts"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), unique=True, index=True
    )
    language: Mapped[str] = mapped_column(String(16), default="")
    provider: Mapped[str] = mapped_column(String(64), default="")
    model: Mapped[str] = mapped_column(String(128), default="")
    # [{"start": float, "end": float, "text": str}]
    segments: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    # [{"start": float, "end": float, "word": str}] — нужен для точной подрезки границ
    words: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    full_text: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    project: Mapped[Project] = relationship(back_populates="transcript")


class Segment(Base):
    """Кандидат в шортс: один самостоятельный момент или склейка соседних."""

    __tablename__ = "segments"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )

    start: Mapped[float] = mapped_column(Float)
    end: Mapped[float] = mapped_column(Float)
    # Из каких кусков транскрипта склеен фрагмент: [[start, end], ...]
    source_ranges: Mapped[list[list[float]]] = mapped_column(JSON, default=list)

    title_de: Mapped[str] = mapped_column(String(500), default="")
    description_de: Mapped[str] = mapped_column(Text, default="")
    hashtags: Mapped[list[str]] = mapped_column(JSON, default=list)
    hook: Mapped[str] = mapped_column(Text, default="")
    transcript_text: Mapped[str] = mapped_column(Text, default="")

    score: Mapped[float] = mapped_column(Float, default=0.0)
    reason: Mapped[str] = mapped_column(Text, default="")

    status: Mapped[SegmentStatus] = mapped_column(
        Enum(SegmentStatus, native_enum=False), default=SegmentStatus.CANDIDATE, index=True
    )
    error: Mapped[str] = mapped_column(Text, default="")

    clip_path: Mapped[str] = mapped_column(String(1000), default="")
    render_path: Mapped[str] = mapped_column(String(1000), default="")
    thumb_path: Mapped[str] = mapped_column(String(1000), default="")
    render_meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    # Переопределения монтажа именно для этого фрагмента.
    edit_overrides: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    # Точное время выхода этого ролика. Заполнено — расписание для него не
    # считается вовсе: человек уже сказал, когда, и спорить с ним незачем.
    publish_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    project: Mapped[Project] = relationship(back_populates="segments")
    publications: Mapped[list["Publication"]] = relationship(
        back_populates="segment", cascade="all, delete-orphan"
    )

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


class Preset(Base):
    """Именованный набор настроек анализа и монтажа."""

    __tablename__ = "presets"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    projects: Mapped[list[Project]] = relationship(back_populates="preset")


class Asset(Base):
    """Загруженный файл: маска, баннер, LUT, шрифт, водяной знак."""

    __tablename__ = "assets"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(300))
    path: Mapped[str] = mapped_column(String(1000))
    mime: Mapped[str] = mapped_column(String(120), default="")
    size: Mapped[int] = mapped_column(Integer, default=0)
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Worker(Base):
    """Машина, которая тянет задачи из общей очереди.

    Панель сама ничего не считает — она управляет воркерами и показывает, что
    где лежит. Файлы (исходники, нарезка, готовые ролики) остаются на том
    воркере, который их сделал, поэтому проект закрепляется за воркером на
    стадии загрузки и дальше не переезжает.
    """

    __tablename__ = "workers"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Имя из SHORTS_WORKER_NAME (или имя хоста). Ключ, по которому воркер
    # опознаёт себя при перезапуске и находит свои закрепления.
    name: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    hostname: Mapped[str] = mapped_column(String(200), default="")
    # Откуда панель забирает превью и готовые ролики этого воркера.
    # Пусто — файлы недоступны, задачи всё равно выполняются.
    public_url: Mapped[str] = mapped_column(String(500), default="")
    labels: Mapped[list[str]] = mapped_column(JSON, default=list)
    version: Mapped[str] = mapped_column(String(64), default="")

    concurrency: Mapped[int] = mapped_column(Integer, default=0)
    running_jobs: Mapped[int] = mapped_column(Integer, default=0)
    disk_free: Mapped[int] = mapped_column(Integer, default=0)

    # Выключенный воркер не берёт новых задач (текущие доделывает).
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Account(Base):
    """Подключённый аккаунт площадки. Токены хранятся зашифрованными."""

    __tablename__ = "accounts"
    __table_args__ = (UniqueConstraint("platform", "external_id", name="uq_account_platform_ext"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    platform: Mapped[str] = mapped_column(String(32), index=True)  # youtube | instagram
    name: Mapped[str] = mapped_column(String(300))
    external_id: Mapped[str] = mapped_column(String(200), default="")
    credentials_enc: Mapped[str] = mapped_column(Text, default="")
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    # Где выполняется публикация. Профиль браузера и сессия площадки лежат на
    # конкретной машине и не переезжают, поэтому аккаунт закрепляется за
    # воркером. Пусто — публикует любой свободный (годится для API-аккаунтов).
    worker_id: Mapped[int | None] = mapped_column(
        ForeignKey("workers.id", ondelete="SET NULL"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    # Внешний ключ в базе объявлен с ON DELETE CASCADE, но ORM об этом не знает
    # и перед удалением пытается обнулить publications.account_id — колонка не
    # допускает NULL, и удаление падало на IntegrityError. passive_deletes
    # передаёт работу базе, как и задумано схемой.
    publications: Mapped[list["Publication"]] = relationship(
        back_populates="account", cascade="all, delete-orphan", passive_deletes=True
    )
    projects: Mapped[list["Project"]] = relationship(
        secondary=account_projects, back_populates="accounts", order_by="Project.created_at.desc()"
    )
    worker: Mapped["Worker | None"] = relationship(lazy="joined")


class Publication(Base):
    __tablename__ = "publications"

    id: Mapped[int] = mapped_column(primary_key=True)
    segment_id: Mapped[int] = mapped_column(
        ForeignKey("segments.id", ondelete="CASCADE"), index=True
    )
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))
    platform: Mapped[str] = mapped_column(String(32), index=True)

    status: Mapped[PublicationStatus] = mapped_column(
        Enum(PublicationStatus, native_enum=False), default=PublicationStatus.PENDING, index=True
    )
    title: Mapped[str] = mapped_column(String(500), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    privacy: Mapped[str] = mapped_column(String(32), default="private")
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    remote_id: Mapped[str] = mapped_column(String(200), default="")
    remote_url: Mapped[str] = mapped_column(String(1000), default="")
    error: Mapped[str] = mapped_column(Text, default="")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    segment: Mapped[Segment] = relationship(back_populates="publications")
    account: Mapped[Account] = relationship(back_populates="publications")


class Job(Base):
    """Единица работы для воркера."""

    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_claim", "status", "run_after", "priority", "id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, native_enum=False), default=JobStatus.QUEUED, index=True
    )
    priority: Mapped[int] = mapped_column(Integer, default=100)  # меньше = раньше
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=2)

    progress: Mapped[float] = mapped_column(Float, default=0.0)
    message: Mapped[str] = mapped_column(Text, default="")
    log: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")

    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    segment_id: Mapped[int | None] = mapped_column(
        ForeignKey("segments.id", ondelete="CASCADE"), index=True
    )
    publication_id: Mapped[int | None] = mapped_column(
        ForeignKey("publications.id", ondelete="CASCADE")
    )
    # Пусто — возьмёт любой воркер. Заполнено — только этот: у него файлы
    # проекта или профиль аккаунта, на другой машине задача бессмысленна.
    worker_id: Mapped[int | None] = mapped_column(
        ForeignKey("workers.id", ondelete="SET NULL"), index=True
    )

    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    claimed_by: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
