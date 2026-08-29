"""Поиск шрифта для drawtext и субтитров."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from ..config import REPO_ROOT

# Порядок предпочтения: свой шрифт в репозитории → системные жирные гротески.
_CANDIDATES = [
    REPO_ROOT / "assets" / "fonts" / "Inter-Bold.ttf",
    REPO_ROOT / "assets" / "fonts" / "default.ttf",
    Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "arialbd.ttf",
    Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "seguibl.ttf",
    Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "arial.ttf",
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
]


class FontError(RuntimeError):
    pass


@lru_cache
def default_font() -> Path:
    for candidate in _CANDIDATES:
        if candidate.exists():
            return candidate
    raise FontError(
        "не найден ни один шрифт для заголовков. Положи TTF в assets/fonts/default.ttf "
        "или загрузи шрифт как ассет через панель"
    )


def resolve_font(path: str | Path | None) -> Path:
    if path:
        candidate = Path(path)
        if candidate.exists():
            return candidate
    return default_font()
