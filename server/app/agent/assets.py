"""Материалы монтажа на стороне воркера.

Маски, баннеры, LUT и шрифты загружают один раз в панель. Раскладывать их по
всем машинам руками — та же морока, что и с профилями браузера, поэтому воркер
забирает нужный файл с панели сам и держит копию в ``storage/assets/cache``.

Кэш сверяется по размеру: файл в панели перезаписывают редко, а полный хеш
гонять на каждый рендер незачем.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import httpx

from ..config import settings
from ..models import Asset
from ..utils.text import safe_filename

log = logging.getLogger(__name__)

DOWNLOAD_TIMEOUT = 120


def _cached_path(asset: Asset) -> Path:
    # Имя с id впереди: два материала могут называться одинаково.
    name = safe_filename(Path(asset.name or f"asset_{asset.id}").name) or f"asset_{asset.id}"
    return settings.asset_cache_dir / f"{asset.id}_{name}"


def _auth() -> tuple[str, str] | None:
    if not settings.panel_password:
        return None
    return (settings.panel_user, settings.panel_password)


def ensure_local(asset: Asset) -> Path | None:
    """Путь к материалу на этой машине. None — взять неоткуда.

    Сначала смотрим путь из базы (панель и одиночная установка), затем кэш,
    и только потом идём качать с панели.
    """
    original = Path(asset.path) if asset.path else None
    if original is not None and original.is_file():
        return original

    cached = _cached_path(asset)
    # Размер нулевой у самой записи — значит сверять нечем, доверяем кэшу.
    if cached.is_file() and (not asset.size or cached.stat().st_size == asset.size):
        return cached

    base = settings.panel_url.strip().rstrip("/")
    if not base:
        log.warning(
            "материал «%s» (id %s) не найден локально, а адрес панели не задан "
            "(SHORTS_PANEL_URL) — монтаж пойдёт без него",
            asset.name,
            asset.id,
        )
        return None

    url = f"{base}/api/assets/{asset.id}/file"
    temp = cached.with_suffix(cached.suffix + ".part")
    try:
        cached.parent.mkdir(parents=True, exist_ok=True)
        # trust_env=False: панель обычно за VPN, ходить к ней надо напрямую.
        with httpx.Client(timeout=DOWNLOAD_TIMEOUT, trust_env=False, auth=_auth()) as client:
            with client.stream("GET", url) as response:
                response.raise_for_status()
                with temp.open("wb") as handle:
                    for block in response.iter_bytes(512 * 1024):
                        handle.write(block)
        # Переименование атомарно — недокачанный файл никогда не станет кэшем.
        os.replace(temp, cached)
    except Exception as exc:  # noqa: BLE001
        temp.unlink(missing_ok=True)
        log.warning("не забрал материал «%s» (id %s) с панели: %s", asset.name, asset.id, exc)
        return None

    log.info("материал «%s» скачан с панели в %s", asset.name, cached)
    return cached
