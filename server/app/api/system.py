"""Диагностика: что настроено, а что нет."""

from __future__ import annotations

import shutil
import subprocess
from datetime import date
from pathlib import Path

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import __version__
from ..config import settings
from ..db import get_db
from ..models import Job, JobStatus
from ..providers.llm import provider_status as llm_status
from ..providers.stt import provider_status as stt_status
from ..schemas import ProviderStatus, SystemStatus

router = APIRouter(prefix="/api/system", tags=["system"])


def _ffmpeg_info() -> dict:
    binary = settings.resolve_ffmpeg()
    resolved = shutil.which(binary) or (binary if Path(binary).exists() else "")
    if not resolved:
        return {
            "available": False,
            "path": binary,
            "version": "",
            "hint": "Запусти scripts/bootstrap.ps1 — он скачает сборку ffmpeg в tools/ffmpeg",
        }
    try:
        result = subprocess.run(
            [resolved, "-version"], capture_output=True, text=True, timeout=10
        )
        version = result.stdout.splitlines()[0] if result.stdout else ""
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "path": resolved, "version": "", "hint": str(exc)}
    return {"available": True, "path": resolved, "version": version, "hint": ""}


def _ytdlp_info() -> dict:
    """Возраст yt-dlp — обычная причина отказов загрузки, поэтому он на виду.

    YouTube ломает извлечение раз в несколько месяцев, и починка приезжает
    только с новой версией. Всё, что старше полугода, считаем подозрительным.
    """
    try:
        from yt_dlp.version import __version__ as ytdlp_version
    except ImportError as exc:
        return {"available": False, "version": "", "hint": f"yt-dlp не установлен: {exc}"}

    hint = ""
    try:
        # версии выглядят как 2026.7.4 — год и месяц берём из первых частей
        year, month = (int(part) for part in ytdlp_version.split(".")[:2])
        age_months = (date.today().year - year) * 12 + date.today().month - month
        if age_months >= 6:
            hint = (
                f"версии {age_months} мес. — YouTube за это время наверняка менял защиту. "
                "Обнови: pip install -U yt-dlp"
            )
    except ValueError:
        pass
    return {"available": True, "version": ytdlp_version, "hint": hint}


def _web_build() -> dict:
    from ..main import web_build_state

    return web_build_state()


def _storage_info() -> dict:
    usage = shutil.disk_usage(settings.storage_dir)

    def folder_size(path: Path) -> int:
        if not path.exists():
            return 0
        return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())

    return {
        "root": str(settings.storage_dir),
        "free_gb": round(usage.free / 1024**3, 1),
        "total_gb": round(usage.total / 1024**3, 1),
        "sources_mb": round(folder_size(settings.sources_dir) / 1024**2, 1),
        "renders_mb": round(folder_size(settings.renders_dir) / 1024**2, 1),
        "clips_mb": round(folder_size(settings.clips_dir) / 1024**2, 1),
    }


@router.get("/status", response_model=SystemStatus)
def status(db: Session = Depends(get_db)) -> SystemStatus:
    rows = db.execute(select(Job.status, func.count(Job.id)).group_by(Job.status)).all()
    queue = {item.value: 0 for item in JobStatus}
    for job_status, count in rows:
        key = job_status.value if hasattr(job_status, "value") else str(job_status)
        queue[key] = int(count)

    return SystemStatus(
        version=__version__,
        ffmpeg=_ffmpeg_info(),
        ytdlp=_ytdlp_info(),
        web_build=_web_build(),
        storage=_storage_info(),
        stt_providers=[ProviderStatus(**item) for item in stt_status()],
        llm_providers=[ProviderStatus(**item) for item in llm_status()],
        llm_selected=f"{settings.llm_provider}/{settings.llm_model}",
        public_base_url=settings.public_base_url,
        secret_key_set=bool(settings.secret_key),
        queue=queue,
    )
