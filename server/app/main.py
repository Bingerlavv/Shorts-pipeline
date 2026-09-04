"""Точка входа API: uvicorn app.main:app"""

from __future__ import annotations

import base64
import hmac
import logging
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from . import __version__
from .api import api_router
from .config import REPO_ROOT, settings
from .db import SessionLocal, init_db
from .media.probe import MediaError
from .models import Preset
from .pipeline.config_schema import DEFAULT_CONFIG
from .providers.llm import LLMError
from .providers.publish import PublishError
from .providers.stt import STTError
from .utils.crypto import CredentialsError

log = logging.getLogger(__name__)

WEB_DIST = REPO_ROOT / "web" / "dist"


def _seed_default_preset() -> None:
    db = SessionLocal()
    try:
        if db.scalars(select(Preset).limit(1)).first() is not None:
            return
        db.add(
            Preset(
                name="Базовый",
                description=(
                    "Вертикаль 1080×1920, лёгкое ускорение и зум, зеркалирование "
                    "только при отсутствии текста в кадре."
                ),
                is_default=True,
                config=DEFAULT_CONFIG,
            )
        )
        db.commit()
        log.info("создан пресет по умолчанию")
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ANN201
    # Консоль Windows по умолчанию не в UTF-8, и русские логи превращаются в мусор.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    init_db()
    _seed_default_preset()

    if not settings.secret_key:
        log.warning(
            "SHORTS_SECRET_KEY не задан — подключить аккаунты площадок не получится. "
            'Сгенерируй: python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"'
        )
    if settings.public_base_url.strip():
        # Instagram через Graph API скачивает ролик сам, поэтому сервер приходится
        # выставлять наружу. Но у API нет ни пароля, ни ключа: снаружи станут
        # доступны и аккаунты, и проекты, и запуск публикаций — кому угодно.
        log.warning(
            "SHORTS_PUBLIC_BASE_URL задан (%s) — если сервер и правда виден из "
            "интернета, выставь наружу только /api/segments/*/render, остальное "
            "закрой. У API нет проверки доступа: открытый порт означает открытые "
            "аккаунты.",
            settings.public_base_url.strip(),
        )
    log.info("хранилище: %s", settings.storage_dir)
    yield


app = FastAPI(
    title="Shorts Pipeline",
    version=__version__,
    description="Конвейер: загрузка → транскрипция → поиск моментов → монтаж → публикация",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    # Панель в разработке живёт на порту Vite, в сборке — отдаётся этим же сервером.
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:8000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Пути, которые обязаны работать без входа. Подписанная ссылка на готовый ролик:
# по ней ходит Instagram из интернета, спросить у него логин не у кого — там
# защитой служит сама подпись (см. verify_download_token).
_OPEN_PATHS = ("/api/health",)


def _is_open(request: Request) -> bool:
    path = request.url.path
    if path in _OPEN_PATHS:
        return True
    if path.endswith("/render") and path.startswith("/api/segments/"):
        return bool(request.query_params.get("token"))
    return False


@app.middleware("http")
async def basic_auth(request: Request, call_next):  # noqa: ANN001, ANN201
    """Basic-аутентификация панели.

    Пароль не задан — не спрашиваем ничего: на одной машине панель слушает
    localhost, и лишний барьер только мешает. Как только панель выставлена
    наружу (а с удалёнными воркерами так и будет), пароль становится
    обязательным — им же воркеры забирают материалы.
    """
    if not settings.panel_password or _is_open(request):
        return await call_next(request)

    header = request.headers.get("authorization", "")
    if header.startswith("Basic "):
        try:
            raw = base64.b64decode(header[6:]).decode("utf-8")
            user, _, password = raw.partition(":")
        except Exception:  # noqa: BLE001
            user = password = ""
        # compare_digest на обоих полях: сравнение по времени не должно
        # подсказывать, угадан ли логин.
        ok_user = hmac.compare_digest(user, settings.panel_user)
        ok_password = hmac.compare_digest(password, settings.panel_password)
        if ok_user and ok_password:
            return await call_next(request)

    return JSONResponse(
        status_code=401,
        content={"detail": "нужен вход в панель"},
        headers={"WWW-Authenticate": 'Basic realm="Shorts", charset="UTF-8"'},
    )


@app.exception_handler(MediaError)
@app.exception_handler(STTError)
@app.exception_handler(LLMError)
@app.exception_handler(PublishError)
@app.exception_handler(CredentialsError)
async def domain_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Ошибки конвейера — это понятные пользователю сообщения, а не 500."""
    return JSONResponse(status_code=400, content={"detail": str(exc)})


app.include_router(api_router)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


def web_build_state() -> dict[str, object]:
    """Свежая ли собранная панель относительно исходников.

    Устаревший web/dist — тихая ловушка: сервер отдаёт старый интерфейс без
    новых кнопок, а исходники давно другие. Ошибки при этом никакой, поэтому
    состояние выносим наружу — в лог при старте и в диагностику.
    """
    if not WEB_DIST.exists():
        return {"built": False, "stale": False, "hint": "панель не собрана"}

    def newest(root: Path, patterns: tuple[str, ...]) -> float:
        times = [f.stat().st_mtime for pattern in patterns for f in root.rglob(pattern)]
        return max(times, default=0.0)

    src_dir = REPO_ROOT / "web" / "src"
    src_time = newest(src_dir, ("*.ts", "*.tsx", "*.css")) if src_dir.exists() else 0.0
    dist_time = newest(WEB_DIST, ("*.js", "*.css", "*.html"))
    stale = src_time > dist_time > 0

    return {
        "built": True,
        "stale": stale,
        "built_at": datetime.fromtimestamp(dist_time).isoformat(timespec="seconds"),
        "hint": (
            "Собранная панель старше исходников — на этом порту показывается "
            "устаревший интерфейс. Пересобери: cd web && npm run build, либо открой "
            "dev-сервер Vite."
            if stale
            else ""
        ),
    }


if WEB_DIST.exists():
    _state = web_build_state()
    if _state["stale"]:
        log.warning("панель на / устарела (собрана %s) — %s",
                    _state["built_at"], _state["hint"])
    # Собранная панель отдаётся тем же сервером — один порт на всё.
    app.mount("/", StaticFiles(directory=WEB_DIST, html=True), name="web")
else:

    @app.get("/")
    def web_not_built() -> dict[str, str]:
        return {
            "detail": (
                "Панель не собрана. Для разработки: cd web && npm run dev "
                "(откроется на http://localhost:5173). Для продакшена: npm run build"
            )
        }
