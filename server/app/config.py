"""Настройки приложения. Читаются из окружения и .env в корне репозитория."""

from __future__ import annotations

import shutil
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- инфраструктура ---
    secret_key: str = Field(default="", alias="SHORTS_SECRET_KEY")
    storage_dir: Path = Field(default=REPO_ROOT / "storage", alias="SHORTS_STORAGE_DIR")
    database_url: str = Field(default="", alias="SHORTS_DATABASE_URL")
    worker_concurrency: int = Field(default=2, alias="SHORTS_WORKER_CONCURRENCY")

    # --- воркер как отдельная машина ---
    # Имя, под которым воркер числится в панели. Пусто — берём имя хоста.
    # Оно же ключ, по которому за воркером закрепляются проекты и аккаунты,
    # поэтому менять его на живой установке нельзя: файлы останутся на месте,
    # а закрепления уедут.
    worker_name: str = Field(default="", alias="SHORTS_WORKER_NAME")
    # Адрес, по которому панель заберёт у воркера превью и готовые ролики.
    # Пусто — панель просто не покажет файлы этого воркера, остальное работает.
    worker_public_url: str = Field(default="", alias="SHORTS_WORKER_PUBLIC_URL")
    # Порт файловой отдачи воркера. 0 — не поднимать сервер файлов вовсе.
    worker_files_port: int = Field(default=8770, alias="SHORTS_WORKER_FILES_PORT")
    # Что этот воркер умеет: через запятую, например "gpu,whisper,browser".
    # Пусто — определим сами по окружению.
    worker_labels: str = Field(default="", alias="SHORTS_WORKER_LABELS")
    # Адрес панели. Воркер берёт оттуда материалы монтажа (маски, баннеры, LUT,
    # шрифты) — их загружают один раз в панель, а не раскладывают по машинам.
    # Пусто — материалы ищутся только на локальном диске.
    panel_url: str = Field(default="", alias="SHORTS_PANEL_URL")

    # --- доступ к панели ---
    # Пароль пустой — вход не спрашивается (одна машина, никого вокруг).
    # Задан — панель закрывается basic-аутентификацией, и те же логин с паролем
    # воркер использует, чтобы забирать материалы.
    panel_user: str = Field(default="shorts", alias="SHORTS_PANEL_USER")
    panel_password: str = Field(default="", alias="SHORTS_PANEL_PASSWORD")
    public_base_url: str = Field(default="", alias="SHORTS_PUBLIC_BASE_URL")

    ffmpeg_path: str = Field(default="", alias="SHORTS_FFMPEG_PATH")
    ffprobe_path: str = Field(default="", alias="SHORTS_FFPROBE_PATH")

    # --- STT ---
    whisper_model: str = Field(default="large-v3", alias="SHORTS_WHISPER_MODEL")
    whisper_device: str = Field(default="cuda", alias="SHORTS_WHISPER_DEVICE")
    whisper_compute_type: str = Field(default="float16", alias="SHORTS_WHISPER_COMPUTE_TYPE")
    stt_fallback: str = Field(default="openai", alias="SHORTS_STT_FALLBACK")

    # --- LLM ---
    llm_provider: str = Field(default="anthropic", alias="SHORTS_LLM_PROVIDER")
    llm_model: str = Field(default="claude-opus-5", alias="SHORTS_LLM_MODEL")
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    deepgram_api_key: str = Field(default="", alias="DEEPGRAM_API_KEY")
    ollama_base_url: str = Field(default="http://localhost:11434", alias="OLLAMA_BASE_URL")
    # AITunnel — OpenAI-совместимый шлюз к чужим моделям. Отдельные настройки, а
    # не подмена OPENAI_BASE_URL: так оба провайдера могут жить одновременно.
    aitunnel_api_key: str = Field(default="", alias="AITUNNEL_API_KEY")
    aitunnel_base_url: str = Field(
        default="https://api.aitunnel.ru/v1", alias="AITUNNEL_BASE_URL"
    )
    aitunnel_model: str = Field(default="gpt-4o", alias="AITUNNEL_MODEL")
    aitunnel_stt_model: str = Field(default="whisper-1", alias="AITUNNEL_STT_MODEL")

    # --- площадки ---
    youtube_client_id: str = Field(default="", alias="YOUTUBE_CLIENT_ID")
    youtube_client_secret: str = Field(default="", alias="YOUTUBE_CLIENT_SECRET")
    # TikTok for Developers → приложение → Manage apps. Ключ называется именно
    # client key, а не client id, как у остальных.
    tiktok_client_key: str = Field(default="", alias="TIKTOK_CLIENT_KEY")
    tiktok_client_secret: str = Field(default="", alias="TIKTOK_CLIENT_SECRET")
    # Просим ровно то, что одобрено проверкой. Лишнее право в запросе — это либо
    # отказ на входе, либо задержка аудита: TikTok прямо просит убрать из заявки
    # продукты и права, которые не показаны в демонстрации.
    # Черновики требуют video.upload, публикация сразу в профиль — video.publish.
    tiktok_scopes: str = Field(
        default="user.info.basic,video.upload", alias="TIKTOK_SCOPES"
    )
    # Должен совпадать с полем Redirect URI в приложении посимвольно: localhost
    # и 127.0.0.1 для TikTok — разные адреса, лишний слэш тоже ломает вход.
    # Пусто — берём http://127.0.0.1:8000/api/accounts/tiktok/callback.
    tiktok_redirect_uri: str = Field(default="", alias="TIKTOK_REDIRECT_URI")
    # Выкладка в TikTok через свой браузер (Patchright), в обход аудита
    # приложения. Пусто — встроенный Chromium от patchright (нужен один раз
    # `patchright install chromium`); "chrome" — системный Chrome.
    tiktok_browser_channel: str = Field(default="", alias="TIKTOK_BROWSER_CHANNEL")
    instagram_app_id: str = Field(default="", alias="INSTAGRAM_APP_ID")
    instagram_app_secret: str = Field(default="", alias="INSTAGRAM_APP_SECRET")
    # Вход по логину и паролю — их читает только scripts/ig_publish.py.
    # Аккаунты, подключённые через панель, хранят свои данные в базе зашифрованными.
    instagram_username: str = Field(default="", alias="INSTAGRAM_USERNAME")
    instagram_password: str = Field(default="", alias="INSTAGRAM_PASSWORD")
    instagram_totp_seed: str = Field(default="", alias="INSTAGRAM_TOTP_SEED")
    instagram_proxy: str = Field(default="", alias="INSTAGRAM_PROXY")
    instagram_sessionid: str = Field(default="", alias="INSTAGRAM_SESSIONID")

    # --- телеграм-бот ---
    telegram_bot_token: str = Field(default="", alias="SHORTS_TELEGRAM_BOT_TOKEN")
    # Кто имеет право управлять конвейером. Без списка бот никого не слушает:
    # токен рано или поздно утекает, а бот умеет тратить квоты и публиковать.
    telegram_allowed_chats: str = Field(default="", alias="SHORTS_TELEGRAM_ALLOWED_CHATS")

    # --- производные пути ---
    @property
    def sources_dir(self) -> Path:
        return self.storage_dir / "sources"

    @property
    def audio_dir(self) -> Path:
        return self.storage_dir / "audio"

    @property
    def clips_dir(self) -> Path:
        return self.storage_dir / "clips"

    @property
    def renders_dir(self) -> Path:
        return self.storage_dir / "renders"

    @property
    def assets_dir(self) -> Path:
        return self.storage_dir / "assets"

    @property
    def thumbs_dir(self) -> Path:
        return self.storage_dir / "thumbs"

    @property
    def models_dir(self) -> Path:
        return self.storage_dir / "models"

    @property
    def tiktok_dir(self) -> Path:
        return self.storage_dir / "tiktok"

    @property
    def asset_cache_dir(self) -> Path:
        """Куда воркер складывает материалы, скачанные с панели."""
        return self.storage_dir / "assets" / "cache"

    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{(self.storage_dir / 'shorts.db').as_posix()}"

    def resolve_ffmpeg(self) -> str:
        return self._resolve_binary(self.ffmpeg_path, "ffmpeg")

    def resolve_ffprobe(self) -> str:
        return self._resolve_binary(self.ffprobe_path, "ffprobe")

    @staticmethod
    def _resolve_binary(configured: str, name: str) -> str:
        if configured:
            return configured
        found = shutil.which(name)
        if found:
            return found
        bundled = REPO_ROOT / "tools" / "ffmpeg" / "bin" / f"{name}.exe"
        if bundled.exists():
            return str(bundled)
        # Возвращаем голое имя: ошибка всплывёт при запуске с понятным текстом.
        return name

    def ensure_dirs(self) -> None:
        for path in (
            self.storage_dir,
            self.sources_dir,
            self.audio_dir,
            self.clips_dir,
            self.renders_dir,
            self.assets_dir,
            self.thumbs_dir,
            self.models_dir,
            self.tiktok_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    # Относительный путь в .env считаем от корня репозитория, а не от текущего
    # каталога: API и воркер запускаются из разных мест и должны видеть одно
    # и то же хранилище.
    storage = Path(settings.storage_dir)
    settings.storage_dir = (storage if storage.is_absolute() else REPO_ROOT / storage).resolve()
    settings.ensure_dirs()
    return settings


settings = get_settings()
