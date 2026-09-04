"""Планирование и выполнение публикаций."""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...config import settings
from ...models import (
    Account,
    Project,
    Publication,
    PublicationStatus,
    Segment,
    SegmentStatus,
    utcnow,
)
from ...providers.publish import PublishError, PublishRequest, publisher_for_account
from ...utils.crypto import encrypt_json
from ...utils.text import redact_secrets, truncate
from ..scheduling import next_slot
from ..context import JobContext
from ..resolve import config_for_segment
from ..registry import handler

log = logging.getLogger(__name__)


# Instagram считает загрузки по адресу, а не по аккаунту: две заливки, ушедшие
# с одного IP одновременно, выглядят как автоматизация и стоят блокировки.
# Поэтому ролики туда уходят строго по одному. Аккаунт со своим прокси — это
# отдельный адрес, ему полагается своя дорожка, и он идёт параллельно с общей.
LANE_WAIT = 900.0
_lanes: dict[str, threading.Lock] = {}
_lanes_guard = threading.Lock()


def _lane(key: str) -> threading.Lock:
    with _lanes_guard:
        lock = _lanes.get(key)
        if lock is None:
            lock = threading.Lock()
            _lanes[key] = lock
        return lock


@contextmanager
def _upload_lane(ctx: JobContext, platform: str, publisher: Any) -> Iterator[None]:
    if platform != "instagram":
        yield
        return

    proxy = str(publisher.credentials.get("proxy") or "").strip()
    lock = _lane(proxy)
    if lock.locked():
        # В прокси обычно вписан пароль, поэтому в журнал идёт только хвост
        # после «@» — адрес узла, по которому дорожку и опознают.
        where = f"через {proxy.rsplit('@', 1)[-1]}" if proxy else "с этого адреса"
        ctx.info(f"жду очереди: в Instagram заливаем по одному ролику {where}")

    if not lock.acquire(timeout=LANE_WAIT):
        # Публикацию упавшей не помечаем: очередь просто занята, и задача должна
        # вернуться в неё, а не осесть в ошибках с испорченным статусом.
        raise PublishError(
            "очередь загрузки в Instagram занята слишком долго — верну задачу в очередь",
            retryable=True,
        )
    try:
        yield
    finally:
        lock.release()


def _render_template(template: str, segment: Segment) -> str:
    return (
        template.replace("{title}", segment.title_de or "")
        .replace("{description}", segment.description_de or "")
        .replace("{hashtags}", " ".join(segment.hashtags or []))
        .replace("{hook}", segment.hook or "")
        .strip()
    )


def settle_segment_status(segment: Segment) -> None:
    """Пересобирает статус фрагмента по его публикациям.

    Фрагмент встаёт в PUBLISHING, как только публикацию поставили в очередь.
    Если та не состоялась, вернуть его должно что-то ещё — иначе он навсегда
    остаётся «публикуется», хотя ничего не происходит.
    """
    in_flight = [
        p for p in segment.publications
        if p.status in (
            PublicationStatus.PENDING,
            PublicationStatus.SCHEDULED,
            PublicationStatus.UPLOADING,
        )
    ]
    if in_flight:
        segment.status = SegmentStatus.PUBLISHING
        return
    if any(p.status == PublicationStatus.PUBLISHED for p in segment.publications):
        segment.status = SegmentStatus.PUBLISHED
    elif segment.render_path:
        segment.status = SegmentStatus.RENDERED
    else:
        segment.status = SegmentStatus.CANDIDATE


def release_publication(db, publication_id: int | None, reason: str) -> bool:  # noqa: ANN001
    """Снимает публикацию с очереди, когда её задача выполнена не будет.

    Отменённая задача оставляла строку публикации в PENDING навсегда: задачи
    больше нет, отправлять некому, — а панель на повторную отправку того же
    ролика на тот же аккаунт отвечала «уже отправлен». Это неправда: не ушло
    ничего. Переводим в FAILED — то самое состояние, из которого отправка
    разрешена снова, и с внятной причиной в тексте.
    """
    if not publication_id:
        return False
    publication = db.get(Publication, publication_id)
    if publication is None:
        return False
    # Успевшую уйти публикацию не трогаем: отменили уже после отправки, и
    # ролик на площадке — это факт, а не намерение.
    if publication.status == PublicationStatus.PUBLISHED:
        return False

    publication.status = PublicationStatus.FAILED
    publication.error = reason
    if publication.segment is not None:
        settle_segment_status(publication.segment)
    db.commit()
    return True


def queue_publications(ctx, segment: Segment, publish_config: dict[str, Any]) -> int:  # noqa: ANN001
    """Создаёт публикации и ставит их в очередь. Возвращает, сколько поставлено.

    Вызывается из двух мест: сразу после монтажа и после генерации текстов, —
    поэтому живёт здесь, а не внутри одной из стадий.
    """
    created = schedule_publications(ctx.db, segment, publish_config)
    if not created:
        ctx.info(
            "автопубликация включена, но к проекту не привязан ни один активный "
            "аккаунт — открой «Аккаунты» и отметь этот проект у нужных, либо "
            "добавь их в карточке проекта"
        )
        return 0

    for publication in created:
        enqueue(
            ctx.db,
            "segment.publish",
            project_id=segment.project_id,
            segment_id=segment.id,
            publication_id=publication.id,
            priority=ctx.job.priority + 10,
            run_after=publication.scheduled_at,
            commit=False,
        )
    ctx.db.commit()
    ctx.info(f"поставлено публикаций: {len(created)}")
    return len(created)


def needs_caption(config: dict[str, Any], segment: Segment) -> bool:
    """Стоит ли переписать тексты перед публикацией.

    По умолчанию — только если заголовка нет вовсе: перезаписывать то, что
    человек поправил руками после ревью, было бы неприятной неожиданностью.
    Флаг before_publish включает переписывание всегда.
    """
    caption = config.get("caption", {})
    if not caption.get("enabled"):
        return False
    if not (segment.transcript_text or "").strip():
        return False
    return bool(caption.get("before_publish")) or not (segment.title_de or "").strip()


def accounts_for_project(project: Project) -> list[Account]:
    """Куда уходит этот проект. Источник правды — связь, а не конфиг."""
    return [account for account in project.accounts if account.is_active]


def schedule_publications(
    db: Session, segment: Segment, publish_config: dict[str, Any]
) -> list[Publication]:
    """Создаёт записи публикаций по привязанным аккаунтам. Не ставит их в очередь."""
    created: list[Publication] = []
    schedule = publish_config.get("schedule", {})

    existing_count = db.scalar(
        select(Publication.id).where(Publication.segment_id == segment.id).limit(1)
    )
    if existing_count:
        log.info("для фрагмента %s публикации уже созданы", segment.id)

    for account in accounts_for_project(segment.project):
        platform = account.platform
        platform_config = publish_config.get(platform, {})

        if platform == "youtube":
            title = truncate(segment.title_de or "Short", 100)
            suffix = platform_config.get("title_suffix", "")
            if suffix and not title.endswith(suffix):
                title = truncate(title, 100 - len(suffix)) + suffix
            description = _render_template(
                platform_config.get("description_template", "{description}\n\n{hashtags}"),
                segment,
            )
            privacy = platform_config.get("privacy", "private")
        elif platform == "tiktok":
            # У TikTok одно поле подписи и нет отдельного описания, поэтому
            # заголовок держим коротким, а всё остальное собираем в подпись.
            title = truncate(segment.title_de or "", 150)
            description = _render_template(
                platform_config.get("caption_template", "{title}\n\n{hashtags}"),
                segment,
            )
            # В режиме черновика приватность выбирает человек в приложении,
            # поэтому здесь она остаётся пустой и в запрос не уходит.
            privacy = str(platform_config.get("privacy") or "")
        else:
            title = truncate(segment.title_de or "", 200)
            description = _render_template(
                platform_config.get(
                    "caption_template", "{title}\n\n{description}\n\n{hashtags}"
                ),
                segment,
            )
            privacy = "public"

        # Точное время у фрагмента бьёт расписание: человек уже сказал, когда.
        # Иначе время считает общий скедулер — он смотрит, что уже стоит в
        # очереди на этот аккаунт, и разносит ролики интервалом. Раньше счёт
        # вёлся внутри одного фрагмента и обнулялся на следующем, поэтому все
        # ролики уходили почти одновременно.
        scheduled_at = segment.publish_at or next_slot(db, account.id, schedule)

        publication = Publication(
            segment_id=segment.id,
            account_id=account.id,
            platform=platform,
            title=title,
            description=description,
            privacy=privacy,
            scheduled_at=scheduled_at,
            status=(
                PublicationStatus.SCHEDULED if scheduled_at else PublicationStatus.PENDING
            ),
        )
        db.add(publication)
        # Сразу пишем в базу: следующий вызов скедулера должен видеть
        # только что занятый слот, иначе весь пакет сядет на одно время.
        db.flush()
        created.append(publication)

    if created:
        db.flush()
    return created


def public_video_url(segment: Segment) -> str:
    """URL, по которому Instagram сможет скачать готовый ролик."""
    base = settings.public_base_url.rstrip("/")
    if not base:
        return ""
    return f"{base}/api/segments/{segment.id}/render?token={quote(_download_token(segment))}"


def _download_token(segment: Segment) -> str:
    """Короткая подпись, чтобы публичная ссылка не была перебираемой."""
    import hashlib

    raw = f"{segment.id}:{settings.secret_key}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


def verify_download_token(segment: Segment, token: str) -> bool:
    import hmac

    return hmac.compare_digest(_download_token(segment), token or "")


@handler("segment.publish")
def run_publish(ctx: JobContext) -> None:
    publication = ctx.db.get(Publication, ctx.job.publication_id)
    if publication is None:
        raise PublishError(f"публикация {ctx.job.publication_id} не найдена")

    segment = publication.segment
    account = publication.account

    if not segment.render_path or not Path(segment.render_path).exists():
        raise PublishError("готовый ролик отсутствует — сначала выполни монтаж")
    if not account.is_active:
        raise PublishError(f"аккаунт «{account.name}» отключён")

    # Издателя строим заранее: по его учётным данным определяется, какой
    # адрес займёт загрузка, а значит — в какую дорожку вставать.
    publisher = publisher_for_account(account)

    with _upload_lane(ctx, publication.platform, publisher):
        publication.status = PublicationStatus.UPLOADING
        publication.error = ""
        segment.status = SegmentStatus.PUBLISHING
        ctx.db.commit()

        extra: dict[str, Any] = {}

        if publication.platform == "youtube":
            extra = {"category_id": "22", "made_for_kids": False}
        elif publication.platform == "tiktok":
            tiktok = config_for_segment(ctx.db, segment)["publish"].get("tiktok", {})
            if (account.meta or {}).get("auth") == "patchright":
                # Через свой браузер приватность выбирается в TikTok Studio;
                # mode здесь означает лишь «жать ли Опубликовать» (иначе — в
                # черновики Studio).
                extra = {
                    "publish_now": str(tiktok.get("mode", "draft")) == "direct",
                    "headless": bool(tiktok.get("headless", True)),
                }
            else:
                extra = {
                    "mode": tiktok.get("mode", "draft"),
                    "privacy": publication.privacy,
                    "disable_comment": bool(tiktok.get("disable_comment")),
                    "disable_duet": bool(tiktok.get("disable_duet")),
                    "disable_stitch": bool(tiktok.get("disable_stitch")),
                }
        else:
            extra = {"share_to_feed": True}
            # Вход по логину и паролю отдаёт файл напрямую, Graph API — только по ссылке.
            if publisher.needs_public_url:
                url = public_video_url(segment)
                if not url:
                    raise PublishError(
                        "SHORTS_PUBLIC_BASE_URL не задан. Instagram через Graph API "
                        "скачивает ролик сам, поэтому сервер должен быть доступен из "
                        "интернета. Либо подключи аккаунт по логину и паролю"
                    )
                extra["video_url"] = url

        request = PublishRequest(
            video_path=Path(segment.render_path),
            title=publication.title,
            description=publication.description,
            hashtags=segment.hashtags or [],
            privacy=publication.privacy,
            tags=[tag.lstrip("#") for tag in (segment.hashtags or [])],
            thumbnail_path=Path(segment.thumb_path) if segment.thumb_path else None,
            extra=extra,
        )

        ctx.info(f"публикую на {publication.platform} в аккаунт «{account.name}»")
        try:
            result = publisher.publish(
                request,
                on_progress=lambda fraction, message: ctx.progress(fraction, message),
                on_log=ctx.info,
            )
        except PublishError as exc:
            # Текст ошибки уезжает в базу, в панель и в телеграм. Библиотеки
            # любят вставить в него строку прокси целиком — вместе с паролем.
            message = redact_secrets(str(exc))
            publication.status = PublicationStatus.FAILED
            publication.error = message
            segment.status = SegmentStatus.FAILED
            segment.error = message
            account.last_error = message
            ctx.db.commit()
            if exc.retryable:
                raise  # воркер повторит согласно max_attempts
            ctx.job.max_attempts = ctx.job.attempts  # больше не пробуем
            ctx.db.commit()
            raise

        refreshed = publisher.refreshed_credentials()
        if refreshed:
            account.credentials_enc = encrypt_json(refreshed)

        publication.status = PublicationStatus.PUBLISHED
        publication.remote_id = result.remote_id
        publication.remote_url = result.url
        publication.published_at = utcnow()
        account.last_error = ""

        settle_segment_status(segment)
        segment.error = ""
        ctx.db.commit()

        ctx.progress(1.0, "опубликовано")
        ctx.info(f"готово: {result.url or result.remote_id}")
