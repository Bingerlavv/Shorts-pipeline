"""Процесс-воркер: N потоков разбирают очередь задач.

Запуск:  python -m app.queue.worker
Работа упирается в subprocess (ffmpeg, yt-dlp) и сетевые вызовы, поэтому потоков
достаточно — GIL здесь не мешает.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import sys
import threading
import time
import traceback

from ..config import settings
from ..db import SessionLocal, init_db
from ..pipeline import registry
from ..pipeline.context import JobCancelled, JobContext
from . import manager

log = logging.getLogger("worker")

POLL_INTERVAL = 1.5
STALE_SWEEP_INTERVAL = 300

_shutdown = threading.Event()


def _worker_id(index: int) -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{index}"


def _propagate_failure(db, job) -> None:  # noqa: ANN001
    """Провалившаяся задача должна быть видна в карточке проекта, а не только в очереди."""
    from ..models import JobStatus as Status
    from ..models import Project, ProjectStatus, Segment, SegmentStatus

    db.refresh(job)
    if job.status != Status.FAILED:
        return  # задача ушла на повтор — статусы не трогаем

    message = job.error or "задача завершилась с ошибкой"
    if job.segment_id:
        segment = db.get(Segment, job.segment_id)
        if segment is not None:
            segment.status = SegmentStatus.FAILED
            segment.error = message
    if job.project_id:
        project = db.get(Project, job.project_id)
        if project is not None and not job.segment_id:
            project.status = ProjectStatus.FAILED
            project.error = message
            project.stage_message = ""
    db.commit()


def _clear_failure(db, job) -> None:  # noqa: ANN001
    """Успешная задача снимает ошибку, оставшуюся от прошлой попытки.

    Раньше project.error сбрасывался только в начале загрузки, поэтому падение
    распознавания или анализа висело на проекте вечно — даже когда следующие
    стадии проходили. В панели это выглядело как ошибка, которой уже нет.
    """
    from ..models import Project, Segment

    if job.segment_id:
        segment = db.get(Segment, job.segment_id)
        if segment is not None and segment.error:
            segment.error = ""
    if job.project_id:
        project = db.get(Project, job.project_id)
        if project is not None and project.error:
            project.error = ""
    db.commit()


def _run_job(db, job, worker_id: str) -> None:
    # Сессия живёт столько же, сколько поток воркера, а expire_on_commit
    # выключен — без явного сброса объекты остаются такими, какими их
    # загрузили в первый раз. Правки из панели идут другой сессией, и без
    # этой строки воркер их не увидит до перезапуска: новые настройки
    # проекта, смена пресета, отредактированные заголовки.
    db.expire_all()

    ctx = JobContext(db=db, job=job)
    ctx.info(f"старт {job.type} (воркер {worker_id})")
    try:
        handler = registry.get_handler(job.type)
        handler(ctx)
    except JobCancelled:
        ctx.info("задача отменена")
        if job.type == "segment.publish" and job.publication_id:
            from ..pipeline.stages.publish import release_publication

            release_publication(
                db, job.publication_id, "отменено вручную — ролик не отправлен"
            )
        manager.finish(db, job.id, error=None)
        job.status = job.status  # статус отмены уже выставлен API
        db.commit()
        return
    except Exception as exc:  # noqa: BLE001 — воркер не должен падать из-за задачи
        detail = "".join(traceback.format_exception(exc))
        log.exception("задача %s (%s) упала", job.id, job.type)
        ctx.info(f"ОШИБКА: {exc}")
        manager.append_log(db, job.id, detail)
        manager.finish(db, job.id, error=f"{type(exc).__name__}: {exc}")
        _propagate_failure(db, job)
        return
    manager.finish(db, job.id)
    _clear_failure(db, job)
    ctx.info("готово")


def _loop(index: int) -> None:
    worker_id = _worker_id(index)
    db = SessionLocal()
    log.info("поток %s запущен", worker_id)
    try:
        while not _shutdown.is_set():
            try:
                job = manager.claim_next(db, worker_id)
            except Exception:  # noqa: BLE001
                log.exception("не удалось забрать задачу")
                _shutdown.wait(POLL_INTERVAL * 4)
                continue

            if job is None:
                _shutdown.wait(POLL_INTERVAL)
                continue

            _run_job(db, job, worker_id)
    finally:
        db.close()
        log.info("поток %s остановлен", worker_id)


def _stale_sweeper() -> None:
    db = SessionLocal()
    try:
        while not _shutdown.wait(STALE_SWEEP_INTERVAL):
            try:
                count = manager.requeue_stale(db)
                if count:
                    log.warning("возвращено в очередь зависших задач: %s", count)
            except Exception:  # noqa: BLE001
                log.exception("сбой уборки зависших задач")
    finally:
        db.close()


def main() -> int:
    # Консоль Windows по умолчанию не в UTF-8, и русские логи превращаются в мусор.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    init_db()
    registry.load_all()
    log.info("зарегистрированы типы задач: %s", ", ".join(registry.registered_types()))

    db = SessionLocal()
    try:
        restored = manager.reset_running_on_boot(db)
        if restored:
            log.warning("восстановлено задач после прошлого запуска: %s", restored)
    finally:
        db.close()

    def _handle_signal(_signum, _frame):  # noqa: ANN001
        log.info("получен сигнал остановки, доделываем текущие задачи…")
        _shutdown.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    threads = [
        threading.Thread(target=_loop, args=(i,), name=f"worker-{i}", daemon=True)
        for i in range(max(1, settings.worker_concurrency))
    ]
    threads.append(threading.Thread(target=_stale_sweeper, name="stale-sweeper", daemon=True))
    for thread in threads:
        thread.start()

    log.info("воркер готов, потоков: %s", settings.worker_concurrency)
    try:
        while not _shutdown.is_set():
            time.sleep(0.5)
    except KeyboardInterrupt:
        _shutdown.set()

    for thread in threads:
        thread.join(timeout=30)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
