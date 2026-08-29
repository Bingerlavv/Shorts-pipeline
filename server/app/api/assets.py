"""Ассеты: маски, баннеры, LUT, шрифты."""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..models import Asset
from ..schemas import AssetOut
from ..utils.text import safe_filename

router = APIRouter(prefix="/api/assets", tags=["assets"])

ALLOWED_KINDS = {"mask", "banner", "background", "lut", "font", "watermark"}
KIND_SUFFIXES = {
    "mask": {".png", ".webp", ".mov", ".webm", ".mp4"},
    "banner": {".png", ".webp", ".mov", ".webm", ".mp4", ".gif"},
    # Фон — всегда видео: картинка в этой роли смотрится статично и мертво.
    "background": {".mp4", ".mov", ".webm", ".mkv"},
    "lut": {".cube", ".3dl"},
    "font": {".ttf", ".otf"},
    "watermark": {".png", ".webp"},
}
MAX_BYTES = 200 * 1024 * 1024


@router.get("", response_model=list[AssetOut])
def list_assets(db: Session = Depends(get_db), kind: str | None = None) -> list[AssetOut]:
    query = select(Asset).order_by(Asset.created_at.desc())
    if kind:
        query = query.where(Asset.kind == kind)
    return [AssetOut.model_validate(a) for a in db.scalars(query).all()]


@router.post("", response_model=AssetOut, status_code=201)
async def upload_asset(
    db: Session = Depends(get_db),
    kind: str = Form(...),
    name: str = Form(""),
    file: UploadFile = File(...),
) -> AssetOut:
    if kind not in ALLOWED_KINDS:
        raise HTTPException(400, f"неизвестный тип ассета: {kind}. Доступны: {', '.join(ALLOWED_KINDS)}")

    suffix = Path(file.filename or "").suffix.lower()
    allowed = KIND_SUFFIXES[kind]
    if suffix not in allowed:
        raise HTTPException(
            400, f"для типа «{kind}» ожидается один из форматов: {', '.join(sorted(allowed))}"
        )

    target_dir = settings.assets_dir / kind
    target_dir.mkdir(parents=True, exist_ok=True)
    stored_name = f"{uuid.uuid4().hex[:12]}_{safe_filename(Path(file.filename).stem)}{suffix}"
    target = target_dir / stored_name

    size = 0
    with target.open("wb") as handle:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_BYTES:
                handle.close()
                target.unlink(missing_ok=True)
                raise HTTPException(413, "файл больше 200 МБ")
            handle.write(chunk)

    asset = Asset(
        kind=kind,
        name=name or Path(file.filename).name,
        path=str(target),
        mime=file.content_type or "",
        size=size,
    )
    db.add(asset)
    db.commit()
    db.refresh(asset)
    return AssetOut.model_validate(asset)


@router.get("/{asset_id}/file")
def download_asset(asset_id: int, db: Session = Depends(get_db)) -> FileResponse:
    asset = db.get(Asset, asset_id)
    if asset is None:
        raise HTTPException(404, f"ассет {asset_id} не найден")
    path = Path(asset.path)
    if not path.exists():
        raise HTTPException(410, "файл ассета удалён с диска")
    return FileResponse(path, filename=asset.name)


@router.delete("/{asset_id}", status_code=204, response_class=Response, response_model=None)
def delete_asset(asset_id: int, db: Session = Depends(get_db)) -> None:
    asset = db.get(Asset, asset_id)
    if asset is None:
        raise HTTPException(404, f"ассет {asset_id} не найден")
    Path(asset.path).unlink(missing_ok=True)
    db.delete(asset)
    db.commit()
