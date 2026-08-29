"""Стадии конвейера. Импорт модуля регистрирует его обработчики задач."""

from . import analyze, chunk, edit, ingest, publish, transcribe  # noqa: F401

__all__ = ["analyze", "chunk", "edit", "ingest", "publish", "transcribe"]
