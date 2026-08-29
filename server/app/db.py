"""Подключение к БД. SQLite в режиме WAL, чтобы воркер и API писали параллельно."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings

log = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


_url = settings.resolved_database_url()
_is_sqlite = _url.startswith("sqlite")

engine = create_engine(
    _url,
    echo=False,
    future=True,
    connect_args={"check_same_thread": False, "timeout": 30} if _is_sqlite else {},
)

if _is_sqlite:

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _record):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def get_db() -> Iterator[Session]:
    """Зависимость FastAPI."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Транзакция для кода вне запросов (воркер, скрипты)."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db() -> None:
    from . import models  # noqa: F401  — регистрирует таблицы в метаданных

    Base.metadata.create_all(bind=engine)

    # create_all() не меняет уже существующие таблицы, поэтому новые колонки
    # доливаем сами. Проверка по факту, а не по номеру версии: миграций в
    # проекте нет, а городить их ради пары полей — лишнее.
    with engine.begin() as connection:
        known = {
            row[1] for row in connection.execute(text("PRAGMA table_info(projects)"))
        }
        if "telegram_chat_id" not in known:
            connection.execute(
                text("ALTER TABLE projects ADD COLUMN telegram_chat_id INTEGER DEFAULT 0")
            )

    with engine.begin() as connection:
        known = {
            row[1] for row in connection.execute(text("PRAGMA table_info(segments)"))
        }
        if "publish_at" not in known:
            connection.execute(text("ALTER TABLE segments ADD COLUMN publish_at DATETIME"))

    _migrate_publish_targets()

    # Один ролик не должен уйти на один и тот же аккаунт дважды. Проверка есть
    # и в API, но два быстрых клика проходят её одновременно — здесь страхует
    # сама база. Ограничение частичное: провалившиеся публикации не считаются,
    # их переотправляют намеренно. Статусы хранятся именами enum, отсюда 'FAILED'.
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_publication_once_per_account "
                "ON publications (segment_id, account_id) WHERE status <> 'FAILED'"
            )
        )


def _migrate_publish_targets() -> None:
    """Переносит выбор аккаунтов из конфигов в связь «аккаунт ↔ проект».

    Раньше аккаунты назначались внутри пресета и переопределений проекта:
    publish.instagram.account_ids. Это оказалось вывернутым наизнанку — аккаунт
    живёт дольше любого проекта, и отмечать его заново на каждом новом видео
    значит делать одну и ту же работу по пять раз. Теперь связь хранится
    отдельной таблицей, а из конфигов эти поля вычищаются: два источника правды
    хуже любого из них по отдельности.

    Пустой список аккаунтов при включённой площадке раньше означал «во все
    активные» — здесь это разворачивается в явные связи, иначе проект молча
    перестал бы публиковаться.
    """
    from .models import Account, Preset, Project  # локально: избегаем цикла импорта

    db = SessionLocal()
    try:
        presets = db.query(Preset).all()
        projects = db.query(Project).all()
        if not projects and not presets:
            return

        # Мигрировать нечего, если ни в одном конфиге не осталось account_ids.
        def targets_of(config: dict | None) -> dict[str, dict]:
            return (config or {}).get("publish", {}) or {}

        has_legacy = any(
            "account_ids" in targets_of(layer).get(platform, {})
            or "enabled" in targets_of(layer).get(platform, {})
            for layer in [p.config for p in presets] + [p.config_overrides for p in projects]
            for platform in ("youtube", "instagram")
        )
        if not has_legacy:
            return

        by_platform: dict[str, list[Account]] = {}
        for account in db.query(Account).all():
            by_platform.setdefault(account.platform, []).append(account)

        default_preset = next((p for p in presets if p.is_default), None)
        linked = 0
        for project in projects:
            preset = project.preset or default_preset
            chosen: list[Account] = []
            for platform in ("youtube", "instagram"):
                base = targets_of(preset.config if preset else None).get(platform, {})
                over = targets_of(project.config_overrides).get(platform, {})
                enabled = over.get("enabled", base.get("enabled", False))
                if not enabled:
                    continue
                ids = over.get("account_ids", base.get("account_ids")) or []
                pool = by_platform.get(platform, [])
                chosen += [a for a in pool if a.id in ids] if ids else list(pool)

            existing = {a.id for a in project.accounts}
            for account in chosen:
                if account.id not in existing:
                    project.accounts.append(account)
                    linked += 1

        # Чистим оба слоя: и пресеты, и переопределения проектов.
        def strip(config: dict | None) -> tuple[dict | None, bool]:
            if not config or "publish" not in config:
                return config, False
            changed = False
            publish = dict(config["publish"])
            for platform in ("youtube", "instagram"):
                section = publish.get(platform)
                if not isinstance(section, dict):
                    continue
                cleaned = {
                    k: v for k, v in section.items() if k not in ("account_ids", "enabled")
                }
                if cleaned != section:
                    publish[platform] = cleaned
                    changed = True
            if not changed:
                return config, False
            return {**config, "publish": publish}, True

        for preset in presets:
            config, changed = strip(preset.config)
            if changed:
                preset.config = config
        for project in projects:
            config, changed = strip(project.config_overrides)
            if changed:
                project.config_overrides = config

        db.commit()
        log.info(
            "перенос настроек публикации: связей «аккаунт ↔ проект» создано %s", linked
        )
    finally:
        db.close()
