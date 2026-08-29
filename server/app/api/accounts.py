"""Подключение аккаунтов площадок."""

from __future__ import annotations

import logging
import hashlib
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..models import Account, Project, Publication
from ..providers.publish import PLATFORMS, PublishError, publisher_for_account
from ..providers.publish.instagram import (
    discover_instagram_accounts,
    exchange_long_lived_token,
)
from ..providers.publish.instagram_login import (
    CheckpointRequired,
    TwoFactorRequired,
)
from ..providers.publish.instagram_login import login as instagram_login
from ..providers.publish.instagram_login import (
    login_with_sessionid as instagram_login_with_sessionid,
)
from ..providers.publish.youtube import SCOPES as YOUTUBE_SCOPES
from ..schemas import (
    AccountOut,
    InstagramConnect,
    InstagramLogin,
    InstagramLoginResult,
    InstagramSelect,
    LinkProjects,
)
from ..utils.crypto import CredentialsError, encrypt_json
from ..utils.text import redact_secrets

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/accounts", tags=["accounts"])

# state → redirect_uri. Живёт только между переходом в Google и возвратом.
_PENDING_OAUTH: dict[str, str] = {}


def _callback_url(request: Request) -> str:
    base = settings.public_base_url.rstrip("/") or str(request.base_url).rstrip("/")
    return f"{base}/api/accounts/youtube/callback"


def _youtube_flow(redirect_uri: str):  # noqa: ANN202
    from google_auth_oauthlib.flow import Flow

    if not settings.youtube_client_id or not settings.youtube_client_secret:
        raise HTTPException(
            400,
            "не заданы YOUTUBE_CLIENT_ID и YOUTUBE_CLIENT_SECRET. Создай OAuth-клиент "
            "типа Desktop app в Google Cloud Console и включи YouTube Data API v3",
        )

    client_config = {
        "installed": {
            "client_id": settings.youtube_client_id,
            "client_secret": settings.youtube_client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [redirect_uri],
        }
    }
    return Flow.from_client_config(client_config, scopes=YOUTUBE_SCOPES, redirect_uri=redirect_uri)


def _account_out(account: Account) -> AccountOut:
    data = AccountOut.model_validate(account)
    data.project_ids = [p.id for p in account.projects]
    return data


@router.get("", response_model=list[AccountOut])
def list_accounts(db: Session = Depends(get_db), platform: str | None = None) -> list[AccountOut]:
    query = select(Account).order_by(Account.platform, Account.name)
    if platform:
        query = query.where(Account.platform == platform)
    return [_account_out(a) for a in db.scalars(query).all()]


# --- YouTube ---

@router.get("/youtube/auth-url")
def youtube_auth_url(request: Request) -> dict[str, str]:
    redirect_uri = _callback_url(request)
    flow = _youtube_flow(redirect_uri)
    url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",  # без него Google не выдаёт refresh_token при повторном входе
    )
    _PENDING_OAUTH[state] = redirect_uri
    return {"url": url, "state": state, "redirect_uri": redirect_uri}


@router.get("/youtube/callback", response_class=HTMLResponse)
def youtube_callback(
    request: Request,
    db: Session = Depends(get_db),
    code: str = Query(""),
    state: str = Query(""),
    error: str = Query(""),
) -> HTMLResponse:
    if error:
        return HTMLResponse(_result_page(f"Google вернул ошибку: {error}", ok=False))
    redirect_uri = _PENDING_OAUTH.pop(state, None)
    if redirect_uri is None:
        return HTMLResponse(
            _result_page("Просроченная или неизвестная сессия входа. Начни заново.", ok=False)
        )

    try:
        flow = _youtube_flow(redirect_uri)
        flow.fetch_token(code=code)
        creds = flow.credentials
    except Exception as exc:  # noqa: BLE001
        return HTMLResponse(_result_page(f"Не удалось обменять код: {exc}", ok=False))

    if not creds.refresh_token:
        return HTMLResponse(
            _result_page(
                "Google не выдал refresh_token. Отзови доступ приложения в настройках "
                "аккаунта Google и подключись заново.",
                ok=False,
            )
        )

    credentials = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": list(creds.scopes or YOUTUBE_SCOPES),
    }

    try:
        from googleapiclient.discovery import build

        service = build("youtube", "v3", credentials=creds, cache_discovery=False)
        channels = service.channels().list(part="snippet", mine=True).execute()
        item = (channels.get("items") or [{}])[0]
        channel_id = item.get("id", "")
        channel_name = item.get("snippet", {}).get("title", "YouTube-канал")
    except Exception as exc:  # noqa: BLE001
        return HTMLResponse(_result_page(f"Токен получен, но канал не читается: {exc}", ok=False))

    try:
        account = _upsert_account(
            db, "youtube", channel_id, channel_name, credentials,
            {"channel_id": channel_id, "thumbnail": item.get("snippet", {}).get("thumbnails", {})},
        )
    except CredentialsError as exc:
        return HTMLResponse(_result_page(str(exc), ok=False))

    return HTMLResponse(
        _result_page(f"Канал «{account.name}» подключён. Можно закрыть вкладку.", ok=True)
    )


# --- Instagram ---

@router.post("/instagram/discover")
def instagram_discover(payload: InstagramConnect) -> dict[str, Any]:
    """По токену пользователя находит доступные Instagram Business-аккаунты."""
    token = payload.access_token.strip()
    try:
        if payload.exchange_long_lived:
            exchanged = exchange_long_lived_token(token)
            token = exchanged.get("access_token", token)
        accounts = discover_instagram_accounts(token)
    except PublishError as exc:
        raise HTTPException(400, str(exc)) from exc

    if not accounts:
        raise HTTPException(
            404,
            "не найдено ни одного Instagram-аккаунта. Нужен Business или Creator-аккаунт, "
            "привязанный к странице Facebook, а у токена — права "
            "instagram_basic, instagram_content_publish, pages_show_list",
        )
    return {"access_token": token, "accounts": accounts}


@router.post("/instagram/connect", response_model=AccountOut, status_code=201)
def instagram_connect(payload: InstagramSelect, db: Session = Depends(get_db)) -> AccountOut:
    credentials = {
        "access_token": payload.access_token,
        "ig_user_id": payload.ig_user_id,
    }
    account = _upsert_account(
        db,
        "instagram",
        payload.ig_user_id,
        payload.username or payload.page_name or payload.ig_user_id,
        credentials,
        {"ig_user_id": payload.ig_user_id, "page_name": payload.page_name},
    )

    ok, message = publisher_for_account(account).verify()
    if not ok:
        # В сообщении может оказаться строка прокси с паролем: она уходит и в
        # базу, и в панель, поэтому логин с паролем оттуда вырезаем.
        message = redact_secrets(message)
        account.is_active = False
        account.last_error = message
        db.commit()
        raise HTTPException(400, f"аккаунт сохранён, но проверка не прошла: {message}")

    return _account_out(account)


@router.post("/instagram/login", response_model=InstagramLoginResult)
def instagram_login_connect(
    payload: InstagramLogin, db: Session = Depends(get_db)
) -> InstagramLoginResult:
    """Подключение обычного аккаунта: логин, пароль и, если нужно, код 2FA.

    Двухфакторный код и checkpoint возвращаются не ошибкой, а статусом: форме
    нужно понять, что просить у пользователя дальше.
    """
    try:
        if payload.sessionid.strip():
            client = instagram_login_with_sessionid(payload.sessionid, proxy=payload.proxy)
        else:
            client = instagram_login(
                payload.username,
                payload.password,
                proxy=payload.proxy,
                verification_code=payload.verification_code,
                totp_seed=payload.totp_seed,
            )
    except TwoFactorRequired as exc:
        return InstagramLoginResult(status="two_factor_required", message=str(exc))
    except CheckpointRequired as exc:
        return InstagramLoginResult(status="checkpoint_required", message=str(exc))
    except PublishError as exc:
        raise HTTPException(400, str(exc)) from exc

    try:
        info = client.account_info()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"вход выполнен, но профиль не читается: {exc}") from exc

    credentials = {
        "username": info.username or payload.username.strip().lstrip("@"),
        "password": payload.password,
        "totp_seed": payload.totp_seed.strip(),
        "proxy": payload.proxy.strip(),
        # Сессия избавляет от повторного входа по паролю: каждый такой вход
        # Instagram считает подключением нового устройства.
        "session": client.get_settings(),
    }
    try:
        account = _upsert_account(
            db,
            "instagram",
            str(info.pk),
            f"@{info.username}",
            credentials,
            {"auth": "login", "username": info.username, "user_id": str(info.pk)},
        )
    except CredentialsError as exc:
        raise HTTPException(400, str(exc)) from exc

    return InstagramLoginResult(
        status="ok",
        message=f"@{info.username} подключён",
        account=_account_out(account),
    )


# --- общее ---

@router.put("/{account_id}/projects", response_model=AccountOut)
def set_account_projects(
    account_id: int, payload: LinkProjects, db: Session = Depends(get_db)
) -> AccountOut:
    """Задаёт, какие проекты публикуются в этот аккаунт.

    Список полный, а не добавочный: панель присылает то, что человек видит
    отмеченным, и снятая галочка должна снимать связь.
    """
    account = _get_account(db, account_id)
    wanted = list(dict.fromkeys(payload.project_ids))

    found = db.scalars(select(Project).where(Project.id.in_(wanted or [-1]))).all()
    missing = set(wanted) - {p.id for p in found}
    if missing:
        raise HTTPException(404, f"проекты не найдены: {sorted(missing)}")

    order = {pid: index for index, pid in enumerate(wanted)}
    account.projects = sorted(found, key=lambda p: order[p.id])
    db.commit()
    db.refresh(account)
    return _account_out(account)


@router.post("/{account_id}/verify")
def verify_account(account_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    account = _get_account(db, account_id)
    try:
        ok, message = publisher_for_account(account).verify()
    except (PublishError, CredentialsError) as exc:
        ok, message = False, str(exc)

    account.last_error = "" if ok else redact_secrets(message)
    account.is_active = ok
    db.commit()
    return {"ok": ok, "message": message}


@router.post("/{account_id}/toggle", response_model=AccountOut)
def toggle_account(account_id: int, db: Session = Depends(get_db)) -> AccountOut:
    account = _get_account(db, account_id)
    account.is_active = not account.is_active
    db.commit()
    return _account_out(account)


@router.delete("/{account_id}", status_code=204, response_class=Response, response_model=None)
def delete_account(
    account_id: int, db: Session = Depends(get_db), force: bool = False
) -> None:
    """Удаляет аккаунт вместе с его публикациями.

    История публикаций — единственная запись о том, что и когда ушло на этот
    аккаунт, поэтому просто так её не сносим: без force сначала отвечаем, сколько
    записей на кону, чтобы панель успела спросить.
    """
    account = _get_account(db, account_id)
    published = db.scalar(
        select(func.count(Publication.id)).where(Publication.account_id == account_id)
    ) or 0

    if published and not force:
        raise HTTPException(
            409,
            f"у аккаунта «{account.name}» {published} публикаций в истории. "
            "Удаление сотрёт их вместе с аккаунтом. Если нужно просто перестать "
            "публиковать — отключи аккаунт кнопкой рядом.",
        )

    db.delete(account)
    db.commit()


def _get_account(db: Session, account_id: int) -> Account:
    account = db.get(Account, account_id)
    if account is None:
        raise HTTPException(404, f"аккаунт {account_id} не найден")
    return account


def _upsert_account(
    db: Session,
    platform: str,
    external_id: str,
    name: str,
    credentials: dict[str, Any],
    meta: dict[str, Any],
) -> Account:
    if platform not in PLATFORMS:
        raise HTTPException(400, f"неизвестная площадка: {platform}")

    account = db.scalars(
        select(Account).where(
            Account.platform == platform, Account.external_id == external_id
        )
    ).first()
    if account is None:
        account = Account(platform=platform, external_id=external_id)
        db.add(account)

    account.name = name
    account.credentials_enc = encrypt_json(credentials)
    account.meta = meta
    account.is_active = True
    account.last_error = ""
    db.commit()
    db.refresh(account)
    return account


def _result_page(message: str, *, ok: bool) -> str:
    colour = "#137333" if ok else "#c5221f"
    icon = "✓" if ok else "✕"
    nonce = secrets.token_hex(4)
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><title>Подключение аккаунта</title></head>
<body style="font-family:system-ui,sans-serif;display:flex;align-items:center;
justify-content:center;height:100vh;margin:0;background:#f6f7f9">
<div style="max-width:420px;padding:32px;background:#fff;border-radius:12px;
box-shadow:0 2px 16px rgba(0,0,0,.08);text-align:center">
<div style="font-size:44px;color:{colour};line-height:1">{icon}</div>
<p style="color:#202124;font-size:15px;line-height:1.5;margin:16px 0 0">{message}</p>
<!-- {nonce} -->
</div></body></html>"""


# --- TikTok ---------------------------------------------------------------

TIKTOK_AUTH = "https://www.tiktok.com/v2/auth/authorize/"
TIKTOK_TOKEN = "https://open.tiktokapis.com/v2/oauth/token/"

# Приложение заводится как Desktop: для этого типа TikTok разрешает в адресе
# возврата только localhost и 127.0.0.1 — то есть внешний домен и туннель не
# нужны вовсе. Взамен обязателен PKCE.
TIKTOK_REDIRECT = "http://127.0.0.1:8000/api/accounts/tiktok/callback"

# state -> (адрес возврата, code_verifier). Отдельно от общего хранилища:
# у остальных площадок в нём лежит одна строка.
_TIKTOK_PENDING: dict[str, tuple[str, str]] = {}


def tiktok_redirect_uri() -> str:
    """Адрес возврата, который надо вписать в настройках приложения TikTok.

    Заданный в .env побеждает всё остальное: TikTok сверяет адрес посимвольно,
    и подогнать его под уже записанный в приложении бывает проще, чем править
    приложение.
    """
    explicit = settings.tiktok_redirect_uri.strip()
    if explicit:
        return explicit
    base = settings.public_base_url.strip().rstrip("/")
    return f"{base}/api/accounts/tiktok/callback" if base else TIKTOK_REDIRECT


def _pkce() -> tuple[str, str]:
    """Пара для PKCE: секрет и его отпечаток.

    TikTok ждёт отпечаток в шестнадцатеричном виде — в отличие от почти всех
    остальных, где принят base64url. На этом легко потерять день.
    """
    verifier = secrets.token_urlsafe(64)  # 43–128 символов из разрешённого набора
    challenge = hashlib.sha256(verifier.encode("ascii")).hexdigest()
    return verifier, challenge


@router.get("/tiktok/auth-url")
def tiktok_auth_url() -> dict[str, str]:
    if not settings.tiktok_client_key or not settings.tiktok_client_secret:
        raise HTTPException(
            400,
            "не заданы TIKTOK_CLIENT_KEY и TIKTOK_CLIENT_SECRET. Заведи приложение "
            "в TikTok for Developers (тип Desktop), включи Content Posting API "
            "и впиши ключи в .env",
        )

    redirect_uri = tiktok_redirect_uri()
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(16)
    _TIKTOK_PENDING[state] = (redirect_uri, verifier)

    params = urlencode(
        {
            "client_key": settings.tiktok_client_key,
            "response_type": "code",
            "scope": settings.tiktok_scopes,
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    return {"url": f"{TIKTOK_AUTH}?{params}", "state": state, "redirect_uri": redirect_uri}


@router.get("/tiktok/redirect-uri")
def tiktok_redirect_hint() -> dict[str, str]:
    """Что скопировать в поле Redirect URI на стороне TikTok.

    Отдельная ручка, чтобы панель показывала адрес до попытки входа: без него
    в приложении вход не состоится, а сообразить это по ошибке от TikTok трудно.
    """
    return {"redirect_uri": tiktok_redirect_uri()}


@router.get("/tiktok/callback", response_class=HTMLResponse)
def tiktok_callback(
    db: Session = Depends(get_db),
    code: str = Query(""),
    state: str = Query(""),
    error: str = Query(""),
    error_description: str = Query(""),
) -> HTMLResponse:
    if error:
        return HTMLResponse(
            _result_page(f"TikTok вернул ошибку: {error_description or error}", ok=False)
        )

    pending = _TIKTOK_PENDING.pop(state, None)
    if pending is None:
        return HTMLResponse(
            _result_page("Просроченная или неизвестная сессия входа. Начни заново.", ok=False)
        )
    redirect_uri, verifier = pending

    with httpx.Client(timeout=30, trust_env=False) as client:
        response = client.post(
            TIKTOK_TOKEN,
            data={
                "client_key": settings.tiktok_client_key,
                "client_secret": settings.tiktok_client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
                "code_verifier": verifier,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        token = response.json() if response.content else {}
        if response.status_code != 200 or not token.get("access_token"):
            detail = token.get("error_description") or token.get("error") or response.text[:200]
            return HTMLResponse(_result_page(f"Не удалось обменять код: {detail}", ok=False))

        # Имя нужно, чтобы аккаунт в панели назывался как в приложении, а не
        # набором цифр open_id. Заодно это первая проверка живого токена.
        profile = client.get(
            "https://open.tiktokapis.com/v2/user/info/",
            params={"fields": "open_id,union_id,display_name,avatar_url"},
            headers={"Authorization": f"Bearer {token['access_token']}"},
        )
    info = (profile.json().get("data") or {}).get("user", {}) if profile.content else {}

    open_id = str(token.get("open_id") or info.get("open_id") or "")
    display = info.get("display_name") or "TikTok"
    credentials = {
        "access_token": token["access_token"],
        "refresh_token": token.get("refresh_token", ""),
        "expires_at": time.time() + float(token.get("expires_in", 0) or 0),
        "open_id": open_id,
        "scopes": token.get("scope", settings.tiktok_scopes),
    }

    try:
        account = _upsert_account(
            db, "tiktok", open_id, f"@{display}", credentials,
            {"open_id": open_id, "username": display, "avatar": info.get("avatar_url", "")},
        )
    except CredentialsError as exc:
        return HTMLResponse(_result_page(str(exc), ok=False))

    granted = str(token.get("scope") or "")
    note = ""
    if "video.publish" not in granted:
        note = (
            " Разрешение на публикацию в профиль не выдано — доступен только режим "
            "черновика, ролик придёт во «Входящие» приложения."
        )
    return HTMLResponse(
        _result_page(f"Аккаунт «{account.name}» подключён.{note} Окно можно закрыть.", ok=True)
    )
