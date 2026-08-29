"""Публикация на YouTube через Data API v3.

Квота по умолчанию — 10 000 единиц в сутки, загрузка стоит 1600, то есть
примерно шесть роликов в день на проект Google Cloud. Об этом лучше знать
до того, как очередь встанет.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

from ...config import settings
from ...utils.text import truncate
from .base import ProgressCallback, PublishError, PublishRequest, PublishResult, Publisher

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]
UPLOAD_QUOTA_COST = 1600
TITLE_LIMIT = 100
DESCRIPTION_LIMIT = 5000


class YouTubePublisher(Publisher):
    platform = "youtube"

    def __init__(self, credentials: dict[str, Any], meta: dict[str, Any] | None = None) -> None:
        super().__init__(credentials, meta)
        self._refreshed: dict[str, Any] | None = None

    def _credentials(self):  # noqa: ANN202
        from google.oauth2.credentials import Credentials

        data = self.credentials
        if not data.get("refresh_token"):
            raise PublishError(
                "у аккаунта нет refresh_token — подключи его заново, "
                "разрешив офлайн-доступ"
            )
        return Credentials(
            token=data.get("token"),
            refresh_token=data["refresh_token"],
            token_uri=data.get("token_uri", "https://oauth2.googleapis.com/token"),
            client_id=data.get("client_id") or settings.youtube_client_id,
            client_secret=data.get("client_secret") or settings.youtube_client_secret,
            scopes=data.get("scopes", SCOPES),
        )

    def _service(self):  # noqa: ANN202
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build

        creds = self._credentials()
        if not creds.valid:
            try:
                creds.refresh(Request())
            except Exception as exc:  # noqa: BLE001
                raise PublishError(
                    f"не удалось обновить токен YouTube: {exc}. Подключи аккаунт заново"
                ) from exc
            self._refreshed = {**self.credentials, "token": creds.token}
        return build("youtube", "v3", credentials=creds, cache_discovery=False)

    def refreshed_credentials(self) -> dict[str, Any] | None:
        return self._refreshed

    def verify(self) -> tuple[bool, str]:
        try:
            service = self._service()
            response = service.channels().list(part="snippet", mine=True).execute()
        except PublishError as exc:
            return False, str(exc)
        except Exception as exc:  # noqa: BLE001
            return False, f"YouTube: {exc}"

        items = response.get("items", [])
        if not items:
            return False, "у аккаунта нет YouTube-канала"
        return True, items[0]["snippet"]["title"]

    def publish(
        self,
        request: PublishRequest,
        *,
        on_progress: ProgressCallback | None = None,
        on_log: Callable[[str], None] | None = None,
    ) -> PublishResult:
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaFileUpload

        if not request.video_path.exists():
            raise PublishError(f"файл не найден: {request.video_path}")

        service = self._service()
        extra = request.extra

        description = request.description
        if request.hashtags:
            description = f"{description}\n\n{' '.join(request.hashtags)}".strip()

        body: dict[str, Any] = {
            "snippet": {
                "title": truncate(request.title, TITLE_LIMIT),
                "description": truncate(description, DESCRIPTION_LIMIT),
                "tags": (request.tags or [])[:30],
                "categoryId": str(extra.get("category_id", "22")),
            },
            "status": {
                "privacyStatus": request.privacy,
                "selfDeclaredMadeForKids": bool(extra.get("made_for_kids", False)),
            },
        }
        if extra.get("publish_at"):
            # Отложенная публикация возможна только у приватного видео.
            body["status"]["privacyStatus"] = "private"
            body["status"]["publishAt"] = extra["publish_at"]

        media = MediaFileUpload(
            str(request.video_path),
            chunksize=8 * 1024 * 1024,
            resumable=True,
            mimetype="video/mp4",
        )
        upload = service.videos().insert(part="snippet,status", body=body, media_body=media)

        if on_log:
            on_log(f"загружаю на YouTube (расход квоты ~{UPLOAD_QUOTA_COST} единиц)")

        response = None
        while response is None:
            try:
                status, response = upload.next_chunk()
            except HttpError as exc:
                raise self._translate_http_error(exc) from exc
            except Exception as exc:  # noqa: BLE001
                raise PublishError(f"обрыв загрузки на YouTube: {exc}", retryable=True) from exc
            if status and on_progress:
                on_progress(status.progress() * 0.95, f"{status.progress() * 100:.0f}%")

        video_id = response["id"]
        if on_log:
            on_log(f"видео создано: {video_id}")

        if request.thumbnail_path and request.thumbnail_path.exists():
            try:
                service.thumbnails().set(
                    videoId=video_id, media_body=str(request.thumbnail_path)
                ).execute()
            except HttpError as exc:
                # Обложку разрешают не всем каналам — не повод валить публикацию.
                if on_log:
                    on_log(f"обложку загрузить не удалось: {exc}")

        if on_progress:
            on_progress(1.0, "опубликовано")

        return PublishResult(
            remote_id=video_id,
            url=f"https://www.youtube.com/shorts/{video_id}",
            raw=response,
        )

    @staticmethod
    def _translate_http_error(exc) -> PublishError:  # noqa: ANN001
        status = getattr(exc.resp, "status", 0)
        detail = getattr(exc, "reason", "") or str(exc)
        if status == 403 and "quota" in detail.lower():
            return PublishError(
                "исчерпана суточная квота YouTube API (одна загрузка = 1600 единиц "
                "из 10 000). Квота обновится в полночь по тихоокеанскому времени, "
                "либо запроси увеличение в Google Cloud Console",
                retryable=True,
            )
        if status in (500, 502, 503, 504):
            return PublishError(f"YouTube временно недоступен ({status})", retryable=True)
        if status == 401:
            return PublishError("токен YouTube отозван — подключи аккаунт заново")
        return PublishError(f"YouTube вернул {status}: {detail}")
