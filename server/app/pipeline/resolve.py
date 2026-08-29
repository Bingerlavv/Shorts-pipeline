"""Сборка итогового конфига и путей к ассетам для проекта/фрагмента."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Asset, Preset, Project, Segment
from .config_schema import resolve_config


def default_preset(db: Session) -> Preset | None:
    return db.scalars(select(Preset).where(Preset.is_default.is_(True))).first()


def config_for_project(db: Session, project: Project) -> dict[str, Any]:
    preset = project.preset or default_preset(db)
    return resolve_config(
        preset.config if preset else None,
        project.config_overrides,
    )


def config_for_segment(db: Session, segment: Segment) -> dict[str, Any]:
    project = segment.project
    preset = project.preset or default_preset(db)
    return resolve_config(
        preset.config if preset else None,
        project.config_overrides,
        segment.edit_overrides,
    )


def asset_path(db: Session, asset_id: int | None) -> Path | None:
    if not asset_id:
        return None
    asset = db.get(Asset, asset_id)
    if asset is None:
        return None
    path = Path(asset.path)
    return path if path.exists() else None


def pick_background(db: Session, config: dict[str, Any], seed: int | None) -> Path | None:
    """Берёт один фон из списка. Один и тот же фрагмент всегда получает один фон.

    Выбор привязан к seed (номеру фрагмента), а не к времени: иначе пересборка
    давала бы другой фон, и уже одобренный ролик менялся бы под руками.
    """
    background = config.get("edit", {}).get("background", {})
    if not background.get("enabled"):
        return None

    paths = [
        path
        for path in (asset_path(db, asset_id) for asset_id in background.get("asset_ids") or [])
        if path is not None and path.exists()
    ]
    if not paths:
        return None

    paths.sort()
    # random.Random(seed).choice на подряд идущих небольших числах раздаёт
    # неровно: из девяти фрагментов пять получали один и тот же фон. Хеш
    # Кнута разносит соседние номера по списку, поэтому идущие подряд шортсы
    # одного проекта получают разные фоны — а это ровно то, ради чего фонов
    # загружают несколько.
    index = ((seed if seed is not None else 0) * 2654435761) % len(paths)
    return paths[index]


def edit_asset_paths(db: Session, config: dict[str, Any]) -> dict[str, Path | None]:
    """Достаёт файлы, на которые ссылается секция edit."""
    edit = config.get("edit", {})
    return {
        "mask": asset_path(db, edit.get("mask", {}).get("asset_id")),
        "banner": asset_path(db, edit.get("banner", {}).get("asset_id")),
        "lut": asset_path(db, edit.get("color", {}).get("lut_asset_id")),
        "font": asset_path(db, edit.get("title", {}).get("font_asset_id")),
        "subtitle_font": asset_path(db, edit.get("subtitles", {}).get("font_asset_id")),
    }
