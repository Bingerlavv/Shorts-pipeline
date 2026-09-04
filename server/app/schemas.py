"""Схемы запросов и ответов API."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .models import JobStatus, ProjectStatus, PublicationStatus, SegmentStatus
from .utils.proxy import ProxyFormatError, normalize_proxy


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- проекты ---

class ProjectCreate(BaseModel):
    # Ссылка либо путь к файлу на диске: что именно пришло, разбирает
    # _classify_source в api/projects.py — там же файл проверяется на месте,
    # чтобы опечатка в пути всплыла сразу, а не через минуту в журнале задачи.
    source_url: str = Field(min_length=1, max_length=2000)
    title: str = ""
    preset_id: int | None = None
    auto_publish: bool = False
    config_overrides: dict[str, Any] = Field(default_factory=dict)
    start_now: bool = True

    @field_validator("source_url")
    @classmethod
    def _validate_source(cls, value: str) -> str:
        # Путь из проводника прилетает в кавычках («Копировать как путь»),
        # и человек их обычно не замечает.
        value = value.strip().strip('"')
        if not value:
            raise ValueError("укажи ссылку на видео или путь к файлу")
        return value


class ProjectUpdate(BaseModel):
    title: str | None = None
    preset_id: int | None = None
    auto_publish: bool | None = None
    config_overrides: dict[str, Any] | None = None
    has_burned_subtitles: bool | None = None


class ProjectOut(ORMModel):
    id: int
    title: str
    source_url: str
    source_kind: str
    status: ProjectStatus
    stage_message: str
    error: str
    duration: float
    width: int
    height: int
    fps: float
    has_burned_subtitles: bool | None
    preset_id: int | None
    auto_publish: bool
    config_overrides: dict[str, Any]
    source_meta: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    segment_count: int = 0
    has_transcript: bool = False
    # Куда публикуется проект. Хранится связью, в config_overrides её нет.
    account_ids: list[int] = Field(default_factory=list)
    # На какой машине лежат файлы проекта. Пусто — ещё не загружали.
    worker_id: int | None = None
    worker_name: str = ""


# --- транскрипт ---

class TranscriptOut(ORMModel):
    id: int
    language: str
    provider: str
    model: str
    segments: list[dict[str, Any]]
    full_text: str


# --- фрагменты ---

class SegmentOut(ORMModel):
    id: int
    project_id: int
    start: float
    end: float
    source_ranges: list[list[float]]
    title_de: str
    description_de: str
    hashtags: list[str]
    hook: str
    transcript_text: str
    score: float
    reason: str
    status: SegmentStatus
    error: str
    clip_path: str
    render_path: str
    thumb_path: str
    render_meta: dict[str, Any]
    edit_overrides: dict[str, Any]
    publish_at: datetime | None
    created_at: datetime
    updated_at: datetime


class SegmentUpdate(BaseModel):
    start: float | None = None
    end: float | None = None
    source_ranges: list[list[float]] | None = None
    title_de: str | None = None
    description_de: str | None = None
    hashtags: list[str] | None = None
    status: SegmentStatus | None = None
    edit_overrides: dict[str, Any] | None = None
    # Точное время выхода. Пустая строка в запросе сбрасывает его обратно на
    # расписание, поэтому None здесь означает «не трогать», а не «убрать».
    publish_at: datetime | None = None
    clear_publish_at: bool = False


class SegmentCreate(BaseModel):
    project_id: int
    start: float
    end: float
    title_de: str = ""
    description_de: str = ""


# --- пресеты ---

class PresetIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    is_default: bool = False
    config: dict[str, Any] = Field(default_factory=dict)


class PresetOut(ORMModel):
    id: int
    name: str
    description: str
    is_default: bool
    config: dict[str, Any]
    created_at: datetime
    updated_at: datetime


# --- ассеты ---

class AssetOut(ORMModel):
    id: int
    kind: str
    name: str
    mime: str
    size: int
    meta: dict[str, Any]
    created_at: datetime


# --- аккаунты ---

class SchedulePreview(BaseModel):
    """Запрос предпросмотра сетки выхода."""

    schedule: dict[str, Any] = Field(default_factory=dict)
    # Аккаунт нужен, чтобы учесть уже поставленное: без него сетка считается
    # с чистого листа и выглядит свободнее, чем есть на самом деле.
    account_id: int | None = None
    count: int = Field(default=8, ge=1, le=40)


class AccountOut(ORMModel):
    id: int
    platform: str
    name: str
    external_id: str
    meta: dict[str, Any]
    is_active: bool
    last_error: str
    created_at: datetime
    # Какие проекты уходят в этот аккаунт. Связь редактируется отсюда же.
    project_ids: list[int] = Field(default_factory=list)
    # Где выполняется публикация: профиль браузера привязан к машине.
    worker_id: int | None = None
    worker_name: str = ""


class PinWorker(BaseModel):
    """На какой машине обслуживать аккаунт. None — на любой свободной."""

    worker_id: int | None = None


class LinkProjects(BaseModel):
    """Полный список проектов аккаунта: что прислали, то и будет."""

    project_ids: list[int] = Field(default_factory=list)


class LinkAccounts(BaseModel):
    """Полный список аккаунтов проекта — та же связь с другой стороны."""

    account_ids: list[int] = Field(default_factory=list)


class InstagramConnect(BaseModel):
    """Ручное подключение: пользователь вставляет токен из Graph API Explorer."""

    access_token: str = Field(min_length=20)
    exchange_long_lived: bool = True


class InstagramSelect(BaseModel):
    access_token: str
    ig_user_id: str
    username: str = ""
    page_name: str = ""


class InstagramLogin(BaseModel):
    """Вход логином и паролем (или готовым sessionid), без Graph API.

    Пароль сохраняется (зашифрованным): Instagram рано или поздно сбрасывает
    сессию, и без пароля переподключиться молча не выйдет.
    """

    username: str = Field(default="", max_length=100)
    password: str = Field(default="", max_length=200)
    # Cookie sessionid из браузера. Обходит проверку входа: её уже прошёл
    # браузер. Логин и пароль в этом случае не нужны.
    sessionid: str = ""
    # Код из приложения-аутентификатора. Нужен, только если включена 2FA.
    verification_code: str = ""
    # Секрет 2FA (base32) — если задан, коды генерируются сами при каждом входе.
    totp_seed: str = ""
    # Весь трафик аккаунта пойдёт через него. Принимаем и вид продавцов
    # (host:port:логин:пароль), и вид библиотек (схема://логин:пароль@host:port) —
    # приводим к второму сразу на входе, чтобы дальше по коду был один формат.
    proxy: str = ""

    @field_validator("proxy")
    @classmethod
    def _normalize_proxy(cls, value: str) -> str:
        try:
            return normalize_proxy(value)
        except ProxyFormatError as exc:
            raise ValueError(str(exc)) from exc

    @model_validator(mode="after")
    def _need_credentials(self) -> "InstagramLogin":
        if self.sessionid.strip():
            return self
        if not self.username.strip() or not self.password:
            raise ValueError("нужны логин и пароль — или sessionid из браузера")
        return self


class InstagramLoginResult(BaseModel):
    """ok | two_factor_required | checkpoint_required."""

    status: str
    message: str = ""
    account: AccountOut | None = None


class TikTokBrowserConnect(BaseModel):
    """Подключение TikTok через свой браузер (Patchright).

    OAuth здесь нет: заводится отдельный профиль Chromium, вход в TikTok
    выполняется один раз видимым окном.
    """

    name: str = Field(min_length=1, max_length=200)
    # Весь трафик профиля пойдёт через него. Принимаем и вид продавцов
    # (host:port:логин:пароль), и вид библиотек (схема://логин:пароль@host:port).
    proxy: str = ""
    # Открыть окно входа сразу после создания профиля.
    login_now: bool = True
    # Необязательно, под гео прокси: язык интерфейса (ru-RU, en-US…) и часовой
    # пояс (Europe/Moscow…). Пусто — подберутся автоматически и геонейтрально.
    locale: str = Field(default="", max_length=15)
    timezone: str = Field(default="", max_length=64)

    @field_validator("name", "locale", "timezone")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()

    @field_validator("proxy")
    @classmethod
    def _normalize_proxy(cls, value: str) -> str:
        try:
            return normalize_proxy(value)
        except ProxyFormatError as exc:
            raise ValueError(str(exc)) from exc


# --- воркеры ---

class WorkerOut(ORMModel):
    id: int
    name: str
    hostname: str
    public_url: str
    labels: list[str] = Field(default_factory=list)
    version: str
    concurrency: int
    running_jobs: int
    disk_free: int
    is_enabled: bool
    last_error: str
    last_seen_at: datetime | None
    created_at: datetime
    # Считается на лету, в таблице этого нет.
    online: bool = False
    projects: int = 0
    accounts: int = 0
    queued: int = 0


# --- публикации ---

class PublicationCreate(BaseModel):
    segment_id: int
    account_id: int
    title: str = ""
    description: str = ""
    privacy: str = "private"
    scheduled_at: datetime | None = None
    start_now: bool = True


class PublicationOut(ORMModel):
    id: int
    segment_id: int
    # Заполняется в обработчике: в самой публикации проекта нет, а календарю
    # он нужен для ссылки на карточку фрагмента.
    project_id: int | None = None
    account_id: int
    platform: str
    status: PublicationStatus
    title: str
    description: str
    privacy: str
    scheduled_at: datetime | None
    published_at: datetime | None
    remote_id: str
    remote_url: str
    error: str
    created_at: datetime


# --- задачи ---

class JobOut(ORMModel):
    id: int
    type: str
    status: JobStatus
    priority: int
    attempts: int
    max_attempts: int
    progress: float
    message: str
    error: str
    project_id: int | None
    segment_id: int | None
    publication_id: int | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class JobDetail(JobOut):
    payload: dict[str, Any]
    log: str


# --- система ---

class ProviderStatus(BaseModel):
    name: str
    available: bool
    reason: str = ""
    model: str = ""
    selected: bool = False
    installed: list[str] = []


class SystemStatus(BaseModel):
    version: str
    ffmpeg: dict[str, Any]
    ytdlp: dict[str, Any] = {}
    web_build: dict[str, Any] = {}
    storage: dict[str, Any]
    stt_providers: list[ProviderStatus]
    llm_providers: list[ProviderStatus]
    llm_selected: str
    public_base_url: str
    secret_key_set: bool
    queue: dict[str, int]
