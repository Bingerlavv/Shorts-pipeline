"""Контекст выполнения задачи: доступ к БД, прогресс, лог, отмена."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from ..models import Job, JobStatus
from ..queue import manager as queue_manager

log = logging.getLogger(__name__)


class JobCancelled(Exception):
    """Задачу отменили из панели во время выполнения."""


@dataclass
class JobContext:
    db: Session
    job: Job
    cancel_event: threading.Event = field(default_factory=threading.Event)

    # Диапазон прогресса текущей стадии внутри общей задачи.
    _range_start: float = 0.0
    _range_end: float = 1.0

    @property
    def payload(self) -> dict[str, Any]:
        return self.job.payload or {}

    def stage(self, start: float, end: float) -> "JobContext":
        """Подконтекст, чей прогресс 0..1 отображается в [start, end] общего."""
        child = JobContext(db=self.db, job=self.job, cancel_event=self.cancel_event)
        child._range_start = self._range_start + (self._range_end - self._range_start) * start
        child._range_end = self._range_start + (self._range_end - self._range_start) * end
        return child

    def progress(self, value: float, message: str = "") -> None:
        self.check_cancelled()
        scaled = self._range_start + (self._range_end - self._range_start) * max(0.0, min(1.0, value))
        queue_manager.report_progress(self.db, self.job.id, scaled, message)

    def info(self, line: str) -> None:
        log.info("job %s: %s", self.job.id, line)
        queue_manager.append_log(self.db, self.job.id, line)

    def check_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise JobCancelled
        # Панель могла выставить CANCELLED напрямую.
        self.db.expire(self.job, ["status"])
        if self.job.status == JobStatus.CANCELLED:
            self.cancel_event.set()
            raise JobCancelled
