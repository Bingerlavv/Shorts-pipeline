"""Реестр обработчиков задач.

Каждый модуль конвейера регистрирует свои типы задач декоратором, воркер знает
только про этот реестр.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .context import JobContext

Handler = Callable[[JobContext], Any]

_HANDLERS: dict[str, Handler] = {}


def handler(job_type: str) -> Callable[[Handler], Handler]:
    def decorator(func: Handler) -> Handler:
        if job_type in _HANDLERS:
            raise RuntimeError(f"обработчик для {job_type!r} уже зарегистрирован")
        _HANDLERS[job_type] = func
        return func

    return decorator


def get_handler(job_type: str) -> Handler:
    try:
        return _HANDLERS[job_type]
    except KeyError:
        raise LookupError(f"нет обработчика для задачи типа {job_type!r}") from None


def registered_types() -> list[str]:
    return sorted(_HANDLERS)


def load_all() -> None:
    """Импортирует все стадии, чтобы сработали декораторы."""
    from .stages import (  # noqa: F401
        analyze,
        caption,
        chunk,
        edit,
        ingest,
        publish,
        transcribe,
    )
