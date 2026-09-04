"""Файловая отдача воркера.

Готовые ролики, нарезка и превью остаются на той машине, которая их сделала.
Панель показывает их, проксируя запрос сюда по адресу из ``Worker.public_url``.

Доступ подписывается HMAC от общего ``SHORTS_SECRET_KEY``: ключ есть и у панели,
и у воркера, поэтому договариваться о чём-то ещё не нужно, а перебрать чужие
ролики по ссылке нельзя. Права смотреть файлы это не заменяет — сервер лучше
не выставлять в интернет голым, но и без пароля он бесполезен.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import mimetypes
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..config import settings

log = logging.getLogger(__name__)

# Что вообще разрешено отдавать и из какого поля БД брать путь.
KINDS = {
    "render": "render_path",
    "clip": "clip_path",
    "thumb": "thumb_path",
}
CHUNK = 512 * 1024


def file_token(kind: str, ident: int) -> str:
    """Подпись ссылки. Считается одинаково на панели и на воркере."""
    raw = f"{kind}:{ident}".encode("utf-8")
    return hmac.new(settings.secret_key.encode("utf-8"), raw, hashlib.sha256).hexdigest()[:32]


def file_url(public_url: str, kind: str, ident: int) -> str:
    base = (public_url or "").rstrip("/")
    return f"{base}/files/{kind}/{ident}?token={file_token(kind, ident)}" if base else ""


def _resolve(kind: str, ident: int) -> Path | None:
    """Путь к файлу по записи в БД. None — нечего отдавать."""
    from ..db import SessionLocal
    from ..models import Segment

    field = KINDS.get(kind)
    if field is None:
        return None
    db = SessionLocal()
    try:
        segment = db.get(Segment, ident)
        if segment is None:
            return None
        value = getattr(segment, field, "") or ""
    finally:
        db.close()
    if not value:
        return None
    path = Path(value)
    return path if path.is_file() else None


class _Handler(BaseHTTPRequestHandler):
    server_version = "ShortsWorker"

    def log_message(self, fmt: str, *args) -> None:  # noqa: ANN002 — подпись из stdlib
        log.debug("файлы: " + fmt, *args)

    def do_GET(self) -> None:  # noqa: N802 — имя из stdlib
        parsed = urlparse(self.path)
        parts = [item for item in parsed.path.split("/") if item]

        if parts == ["health"]:
            self._text(200, "ok")
            return

        if len(parts) != 3 or parts[0] != "files":
            self._text(404, "not found")
            return

        _, kind, raw_id = parts
        if not raw_id.isdigit():
            self._text(400, "bad id")
            return
        ident = int(raw_id)

        token = (parse_qs(parsed.query).get("token") or [""])[0]
        if not hmac.compare_digest(token, file_token(kind, ident)):
            self._text(403, "bad token")
            return

        path = _resolve(kind, ident)
        if path is None:
            self._text(404, "no file")
            return
        self._send_file(path)

    def _send_file(self, path: Path) -> None:
        size = path.stat().st_size
        media = mimetypes.guess_type(path.name)[0] or "application/octet-stream"

        # Range нужен, чтобы в панели работала перемотка предпросмотра.
        start, end = 0, size - 1
        status = 200
        header = self.headers.get("Range", "")
        if header.startswith("bytes="):
            spec = header[6:].split("-", 1)
            try:
                if spec[0]:
                    start = int(spec[0])
                if len(spec) > 1 and spec[1]:
                    end = min(int(spec[1]), size - 1)
                if start > end or start >= size:
                    raise ValueError
                status = 206
            except ValueError:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return

        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", media)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()

        with path.open("rb") as handle:
            handle.seek(start)
            left = length
            while left > 0:
                block = handle.read(min(CHUNK, left))
                if not block:
                    break
                try:
                    self.wfile.write(block)
                except (BrokenPipeError, ConnectionResetError):
                    return  # панель закрыла соединение — обычное дело для видео
                left -= len(block)

    def _text(self, status: int, body: str) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class FileServer:
    def __init__(self, httpd: ThreadingHTTPServer, thread: threading.Thread) -> None:
        self._httpd = httpd
        self._thread = thread

    def stop(self) -> None:
        self._httpd.shutdown()
        self._thread.join(timeout=10)
        self._httpd.server_close()
        log.info("файловый сервер воркера остановлен")


def start_file_server() -> FileServer | None:
    """Поднимает отдачу файлов. None — порт не задан или занят."""
    port = int(settings.worker_files_port or 0)
    if port <= 0:
        log.info("файловый сервер выключен (SHORTS_WORKER_FILES_PORT=0)")
        return None
    if not settings.secret_key:
        log.warning("нет SHORTS_SECRET_KEY — файловый сервер не поднимаю, ссылки нечем подписать")
        return None

    try:
        httpd = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
    except OSError as exc:
        log.warning("файловый сервер не поднялся на порту %s: %s", port, exc)
        return None

    thread = threading.Thread(target=httpd.serve_forever, name="worker-files", daemon=True)
    thread.start()
    log.info(
        "файлы отдаю на порту %s, панели показываюсь как %s",
        port,
        settings.worker_public_url or "(адрес не задан — превью не будет)",
    )
    return FileServer(httpd, thread)
