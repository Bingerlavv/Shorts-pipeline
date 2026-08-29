"""Пресеты настроек конвейера."""

from __future__ import annotations

import json

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Preset
from ..pipeline.config_schema import DEFAULT_CONFIG, resolve_config
from ..schemas import PresetIn, PresetOut

router = APIRouter(prefix="/api/presets", tags=["presets"])


@router.get("/schema")
def config_schema() -> dict[str, Any]:
    """Полный конфиг со значениями по умолчанию — панель строит по нему форму."""
    return DEFAULT_CONFIG


@router.get("", response_model=list[PresetOut])
def list_presets(db: Session = Depends(get_db)) -> list[PresetOut]:
    presets = db.scalars(select(Preset).order_by(Preset.is_default.desc(), Preset.name)).all()
    return [PresetOut.model_validate(p) for p in presets]


@router.post("/{preset_id}/clone", response_model=PresetOut, status_code=201)
def clone_preset(preset_id: int, db: Session = Depends(get_db)) -> PresetOut:
    """Копия пресета со всеми настройками.

    Нужна, когда те же правила монтажа идут на другие аккаунты: проще
    продублировать и поменять один список, чем собирать полсотни полей заново.
    Копия никогда не становится основной — иначе дубль молча перехватил бы
    все новые проекты.
    """
    source = db.get(Preset, preset_id)
    if source is None:
        raise HTTPException(404, f"пресет {preset_id} не найден")

    taken = {name for (name,) in db.execute(select(Preset.name)).all()}
    name = f"{source.name} — копия"
    index = 2
    while name in taken:
        name = f"{source.name} — копия {index}"
        index += 1

    clone = Preset(
        name=name,
        description=source.description,
        is_default=False,
        # Копируем через json, чтобы вложенные словари не остались общими
        # с исходным пресетом: правка копии меняла бы и оригинал.
        config=json.loads(json.dumps(source.config)),
    )
    db.add(clone)
    db.commit()
    db.refresh(clone)
    return PresetOut.model_validate(clone)


@router.post("", response_model=PresetOut, status_code=201)
def create_preset(payload: PresetIn, db: Session = Depends(get_db)) -> PresetOut:
    if db.scalars(select(Preset).where(Preset.name == payload.name)).first():
        raise HTTPException(409, f"пресет с именем «{payload.name}» уже есть")

    preset = Preset(
        name=payload.name,
        description=payload.description,
        is_default=payload.is_default,
        config=payload.config,
    )
    db.add(preset)
    db.flush()
    if payload.is_default:
        db.execute(update(Preset).where(Preset.id != preset.id).values(is_default=False))
    db.commit()
    db.refresh(preset)
    return PresetOut.model_validate(preset)


@router.get("/{preset_id}", response_model=PresetOut)
def get_preset(preset_id: int, db: Session = Depends(get_db)) -> PresetOut:
    preset = db.get(Preset, preset_id)
    if preset is None:
        raise HTTPException(404, f"пресет {preset_id} не найден")
    return PresetOut.model_validate(preset)


@router.get("/{preset_id}/resolved")
def resolved_preset(preset_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Пресет, наложенный на значения по умолчанию — то, что реально применится."""
    preset = db.get(Preset, preset_id)
    if preset is None:
        raise HTTPException(404, f"пресет {preset_id} не найден")
    return resolve_config(preset.config)


@router.put("/{preset_id}", response_model=PresetOut)
def update_preset(
    preset_id: int, payload: PresetIn, db: Session = Depends(get_db)
) -> PresetOut:
    preset = db.get(Preset, preset_id)
    if preset is None:
        raise HTTPException(404, f"пресет {preset_id} не найден")

    duplicate = db.scalars(
        select(Preset).where(Preset.name == payload.name, Preset.id != preset_id)
    ).first()
    if duplicate:
        raise HTTPException(409, f"пресет с именем «{payload.name}» уже есть")

    preset.name = payload.name
    preset.description = payload.description
    preset.config = payload.config
    preset.is_default = payload.is_default
    if payload.is_default:
        db.execute(update(Preset).where(Preset.id != preset_id).values(is_default=False))
    db.commit()
    return PresetOut.model_validate(preset)


@router.delete("/{preset_id}", status_code=204, response_class=Response, response_model=None)
def delete_preset(preset_id: int, db: Session = Depends(get_db)) -> None:
    preset = db.get(Preset, preset_id)
    if preset is None:
        raise HTTPException(404, f"пресет {preset_id} не найден")
    if preset.projects:
        raise HTTPException(
            409,
            f"пресет используется в {len(preset.projects)} проект(ах) — "
            "сначала переключи их на другой",
        )
    db.delete(preset)
    db.commit()
