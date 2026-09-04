"""Выкладка в TikTok через свой браузер под управлением Patchright.

Patchright — это Playwright с патчами против детекта автоматизации; API у него
тот же. Официальный Content Posting API без аудита приложения умеет только
черновики во «Входящие», поэтому здесь мы открываем TikTok Studio в отдельном
профиле Chromium (свой user-data-dir на диске, по желанию — через прокси) и
грузим ролик так, как это делает человек.

Аккаунт заводится на странице «Аккаунты» → «TikTok (браузер)»: создаётся пустой
профиль и один раз открывается видимое окно для входа в TikTok. Дальше сессия
живёт в профиле, и выкладка идёт уже без окна. Переподключить сессию, когда
TikTok её сбросит, можно тем же окном — `scripts/tiktok_login.py`.

Сессию профиль хранит сам (это persistent-контекст на диске), но после входа и
после каждой удачной выкладки мы снимаем её слепок: в ``credentials.session``
общей базы и заодно в ``storage/tiktok/<имя>.json``. Если профиля нет или он
без входа — куки поднимаются из слепка. Именно поэтому вход достаточно сделать
один раз в панели: воркер на другой машине возьмёт ту же сессию из базы, и
таскать профили руками не нужно.

В базе это обычный аккаунт площадки ``tiktok`` с пометкой
``meta.auth == "patchright"``.
"""

from __future__ import annotations

import json
import logging
import re
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import unquote, urlsplit

from ...config import settings
from .base import (
    PublishError,
    PublishRequest,
    PublishResult,
    Publisher,
    ProgressCallback,
    build_caption,
)
from .tiktok_device import make_device

log = logging.getLogger(__name__)

CAPTION_LIMIT = 2200

STUDIO_UPLOAD_URL = "https://www.tiktok.com/tiktokstudio/upload?from=creator_center"
LOGIN_URL = "https://www.tiktok.com/login"
HOME_URL = "https://www.tiktok.com/"

# Подписи кнопок зависят от языка профиля — ловим по нескольким, плюс по
# data-e2e. Регистронезависимость — только флагом re.I: инлайновый (?i) в
# паттерне ломает конвертацию в JS-regex на стороне Playwright.
POST_RE = re.compile(r"^\s*(post|publish|опубликовать|posten|veröffentlichen|发布)\s*$", re.I)
# Всплывашки первого захода: онбординг-подсказки, куки-баннеры, «что нового».
DISMISS_RE = re.compile(
    r"^\s*(понятно|ясно|got it|okay,? got it|закрыть|close|skip|пропустить|"
    r"dismiss|later|позже|не сейчас|no thanks|нет,? спасибо|принять все|accept all)\s*$",
    re.I,
)
SHOW_MORE_RE = re.compile(
    r"(показать больше|показать ещё|показать все|show more|see more|more options)", re.I
)

# Поле подписи — от новой вёрстки Studio к старой; последний селектор общий.
CAPTION_SELECTORS = (
    'div.public-DraftEditor-content[contenteditable="true"]',
    '[data-e2e="caption_container"] div[contenteditable="true"]',
    'div[contenteditable="true"][role="combobox"]',
    'div[contenteditable="true"]',
)
CAPTION_CSS = ", ".join(CAPTION_SELECTORS)
POST_SELECTORS = (
    'button[data-e2e="post_video_button"]',
    '[data-e2e="post_video_button"] button',
)

# Сколько ждём, пока TikTok примет и обработает видео (кнопка публикации до
# этого выключена).
UPLOAD_TIMEOUT = 900
UPLOAD_POLL = 3.0

# «Идёт проверка …» рядом с блоком «Проверки». Пока висит — публикация даёт
# модалку-переспрос; ждём её ухода, но не бесконечно (полная проверка контента
# у TikTok бывает «~10 минут»).
CHECKS_RE = re.compile(r"(ид[её]т проверка|checking|running (a )?check|проверка\s*\.\.\.)", re.I)
CHECKS_WAIT = 150      # секунд ждать быстрые проверки
CHECKS_POLL = 5.0
# Подтверждение, что ролик реально ушёл в ленту.
PUBLISHED_TOAST_RE = re.compile(
    r"(ваше видео (опубликовано|публикуется)|видео опубликован|posted|published|"
    r"your video (is being posted|has been posted))",
    re.I,
)


def _load() -> Callable[[], Any]:
    """Ленивый импорт patchright: без него живёт вся остальная публикация."""
    try:
        from patchright.sync_api import sync_playwright
    except ImportError as exc:  # noqa: BLE001
        raise PublishError(
            "не установлен patchright — он нужен только для выкладки в TikTok "
            "через свой браузер. Поставь его в окружение сервера: "
            r".venv\Scripts\python -m pip install patchright",
            retryable=False,
        ) from exc
    return sync_playwright


def _has_session(context: Any) -> bool:
    try:
        cookies = context.cookies(HOME_URL)
    except Exception:  # noqa: BLE001
        return False
    return any(c.get("name") == "sessionid" and c.get("value") for c in cookies)


class TikTokBrowserPublisher(Publisher):
    """Выкладка в TikTok через профиль Chromium под Patchright."""

    platform = "tiktok"
    needs_public_url = False

    def __init__(self, credentials: dict[str, Any], meta: dict[str, Any] | None = None) -> None:
        super().__init__(credentials, meta)

        raw_dir = str(credentials.get("profile_dir") or (meta or {}).get("profile_dir") or "").strip()
        if not raw_dir:
            raise PublishError(
                "у аккаунта не задан профиль браузера (profile_dir)", retryable=False
            )
        path = Path(raw_dir)
        self.profile_dir = path if path.is_absolute() else (settings.storage_dir / path)

        self.proxy = str(credentials.get("proxy") or "").strip()
        # Пусто — встроенный Chromium от patchright; "chrome" — установленный
        # системный Chrome (чуть незаметнее, но должен быть установлен).
        self.channel = str(credentials.get("channel") or "").strip()
        self.username = str((meta or {}).get("username") or "").lstrip("@")

        # Замороженное «устройство» профиля: язык, пояс, экран, версия Windows.
        # Если в credentials его нет — собираем детерминированно от имени
        # профиля и просим сохранить (refreshed_credentials).
        stored = credentials.get("device")
        self.device: dict[str, Any] = stored or make_device(self.profile_dir.name)
        self._device_dirty = not stored
        # Слепок входа из общей БД. Он и делает вход переносимым: залогинились
        # в панели — воркер поднимет ту же сессию, ничего не перенося руками.
        self._session_dirty = False

    # ---------- Publisher ----------

    def publish(
        self,
        request: PublishRequest,
        *,
        on_progress: ProgressCallback | None = None,
        on_log: Callable[[str], None] | None = None,
    ) -> PublishResult:
        say = on_log or (lambda msg: log.info(msg))
        step = on_progress or (lambda f, m: None)

        path = request.video_path
        if not path.exists():
            raise PublishError(f"файл ролика не найден: {path}")

        headless = bool(request.extra.get("headless", True))
        publish_now = bool(request.extra.get("publish_now", False))
        caption = build_caption(request, CAPTION_LIMIT)

        with self._browser(headless=headless) as (context, page):
            step(0.1, "открываю TikTok Studio")
            page.goto(STUDIO_UPLOAD_URL, wait_until="domcontentloaded", timeout=60_000)
            if not _has_session(context) or "/login" in page.url or "/passport" in page.url:
                raise PublishError(
                    "профиль не залогинен в TikTok — открой вход заново: "
                    r".venv\Scripts\python scripts\tiktok_login.py",
                    retryable=False,
                )
            self._dismiss_overlays(page)

            field = page.locator('input[type="file"]').first
            field.wait_for(state="attached", timeout=30_000)
            field.set_input_files(str(path))
            say(f"файл отправлен: {path.name}")
            step(0.3, "жду, пока TikTok примет видео")

            editor = page.locator(CAPTION_CSS).first
            editor.wait_for(state="visible", timeout=180_000)
            # Приём файла нередко поднимает онбординг-подсказку поверх формы.
            self._dismiss_overlays(page)
            if caption:
                editor.click()
                page.keyboard.press("Control+A")
                page.keyboard.press("Delete")
                editor.press_sequentially(caption, delay=12)
                step(0.6, "подпись введена")

            self._expand_more(page)  # раскрыть доп. настройки

            if not publish_now:
                # У веб-Studio нет кнопки «Сохранить черновик» — только
                # «Опубликовать» и «Удалить». Оставляем ролик заполненным и не
                # публикуем: Studio сама держит его локальным черновиком, дальше
                # человек открывает Studio → «Продолжить» → «Опубликовать».
                self._save_session(context)
                note = (
                    "залито в TikTok Studio как локальный черновик — открой Studio, "
                    "«Продолжить» и опубликуй сам"
                )
                say(note)
                step(1.0, "готово")
                return PublishResult(remote_id="", url="", raw={"via": "patchright", "note": note})

            post = self._button(page, POST_RE, *POST_SELECTORS)
            if post is None:
                raise PublishError(
                    "не нашёл кнопку «Опубликовать» в TikTok Studio — вёрстка изменилась",
                    retryable=False,
                )
            self._wait_enabled(post, say)
            self._dismiss_overlays(page)  # подсказка могла всплыть за время обработки

            # Даём дойти быстрым проверкам (авторские права на музыку, ~30 с) —
            # тогда TikTok не переспрашивает и публикация проходит надёжно.
            self._wait_checks(page, say)

            say("публикую")
            post = self._button(page, POST_RE, *POST_SELECTORS) or post
            self._safe_click(page, post)
            # Если всё же переспросил «Проверка не завершена, публиковать?» —
            # подтверждаем (так же делает человек, не дожидаясь долгой проверки).
            if self._confirm_post(page):
                say("подтвердил публикацию в диалоге TikTok")

            note = self._confirm_published(page, caption, say)
            page.wait_for_timeout(8000)  # дать TikTok дозакрыть публикацию до close()

            self._save_session(context)
            step(1.0, "готово")
            return PublishResult(
                remote_id="",
                url=self._posted_url(page),
                raw={"via": "patchright", "note": note},
            )

    def verify(self) -> tuple[bool, str]:
        """Открывает профиль скрыто и проверяет, что вход в TikTok жив."""
        empty = not self.profile_dir.exists() or not any(self.profile_dir.iterdir())
        if empty and self._stored_session() is None:
            return False, (
                "профиль браузера пуст — выполни вход: "
                r".venv\Scripts\python scripts\tiktok_login.py"
            )
        try:
            with self._browser(headless=True) as (context, page):
                page.goto(HOME_URL, wait_until="domcontentloaded", timeout=45_000)
                ok = _has_session(context)
                name = self._read_username(page) if ok else ""
        except PublishError as exc:
            return False, str(exc)
        except Exception as exc:  # noqa: BLE001
            return False, f"не удалось открыть браузер профиля: {exc}"

        if not ok:
            return False, (
                "в профиле нет активной сессии TikTok — выполни вход: "
                r".venv\Scripts\python scripts\tiktok_login.py"
            )
        return True, f"TikTok (браузер): @{name or self.username or 'вошли'}"

    def login_interactive(
        self, *, timeout: float = 300.0, on_log: Callable[[str], None] | None = None
    ) -> str:
        """Открывает видимое окно, ждёт входа в TikTok, возвращает ник."""
        say = on_log or (lambda msg: log.info(msg))
        self.profile_dir.mkdir(parents=True, exist_ok=True)

        with self._browser(headless=False) as (context, page):
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60_000)
            say(
                "Окно открыто. Войди в нужный аккаунт TikTok — как только вход "
                "пройдёт, окно закроется само."
            )
            deadline = time.time() + timeout
            while time.time() < deadline:
                if _has_session(context):
                    break
                page.wait_for_timeout(2000)
            else:
                raise PublishError(
                    f"вход не завершён за {int(timeout)} с — попробуй ещё раз",
                    retryable=False,
                )
            name = self._read_username(page)
            self._save_session(context)
            say(f"сессия сохранена: {self._session_file}")
        return name

    def refreshed_credentials(self) -> dict[str, Any] | None:
        """Что просим сохранить в базе после работы.

        Устройство — чтобы не пересобиралось; слепок сессии — чтобы вход,
        сделанный на одной машине, работал на любой другой.
        """
        if not (self._device_dirty or self._session_dirty):
            return None
        self._device_dirty = False
        self._session_dirty = False
        return dict(self.credentials)

    # ---------- Внутреннее ----------

    @contextmanager
    def _browser(self, *, headless: bool) -> Iterator[tuple[Any, Any]]:
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        dev = self.device
        sync_playwright = _load()
        with sync_playwright() as pw:
            try:
                context = pw.chromium.launch_persistent_context(
                    user_data_dir=str(self.profile_dir),
                    headless=headless,
                    channel=self.channel or None,
                    proxy=self._proxy_dict(),
                    locale=dev["locale"],
                    timezone_id=dev["timezone_id"],
                    viewport=dev["viewport"],
                    screen=dev["screen"],
                    color_scheme=dev["color_scheme"],
                    # Без этого Playwright добавляет --no-sandbox и Chrome рисует
                    # жёлтую плашку «неподдерживаемый флаг» — лишний маркер.
                    chromium_sandbox=True,
                    args=[
                        f"--window-size={dev['screen']['width']},{dev['screen']['height']}",
                    ],
                )
            except Exception as exc:  # noqa: BLE001
                message = str(exc)
                if "Executable doesn't exist" in message or "install" in message.lower():
                    raise PublishError(
                        "не скачан Chromium для patchright. Выполни один раз: "
                        r".venv\Scripts\patchright install chromium",
                        retryable=False,
                    ) from exc
                if "ProcessSingleton" in message or "SingletonLock" in message or "in use" in message:
                    raise PublishError(
                        "профиль браузера занят другим окном — закрой его и повтори",
                        retryable=True,
                    ) from exc
                raise PublishError(f"не удалось запустить браузер: {message}") from exc
            try:
                if self._restore_session(context):
                    log.info("сессия TikTok поднята из сохранённого слепка")
                page = context.pages[0] if context.pages else context.new_page()
                page.set_default_timeout(45_000)
                self._apply_identity(context, page)
                yield context, page
            finally:
                try:
                    context.close()
                except Exception:  # noqa: BLE001
                    pass

    @property
    def _session_file(self) -> Path:
        """Слепок сессии рядом с каталогом профиля: storage/tiktok/<имя>.json."""
        return self.profile_dir.parent / f"{self.profile_dir.name}.json"

    def _save_session(self, context: Any) -> None:
        """Снимает слепок входа: на диск рядом с профилем и в общую БД.

        Файл — для этой машины, запись в credentials — для всех остальных:
        она уезжает в общую базу и оттуда достаётся любым воркером.
        """
        try:
            state = context.storage_state()
        except Exception as exc:  # noqa: BLE001
            log.warning("не удалось снять слепок сессии TikTok: %s", exc)
            return

        if state != self.credentials.get("session"):
            self.credentials["session"] = state
            self._session_dirty = True

        try:
            self._session_file.parent.mkdir(parents=True, exist_ok=True)
            self._session_file.write_text(
                json.dumps(state, ensure_ascii=False), encoding="utf-8"
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("слепок сессии не лёг на диск: %s", exc)

    def _stored_session(self) -> dict[str, Any] | None:
        """Слепок входа: сперва из общей БД, потом из файла рядом с профилем.

        БД идёт первой намеренно: вход мог быть сделан на другой машине, и там
        слепок свежее. Файл — запасной путь для одиночной установки.
        """
        state = self.credentials.get("session")
        if isinstance(state, dict) and state.get("cookies"):
            return state
        if self._session_file.exists():
            try:
                return json.loads(self._session_file.read_text(encoding="utf-8"))
            except Exception as exc:  # noqa: BLE001
                log.warning("сохранённая сессия TikTok не читается: %s", exc)
        return None

    def _restore_session(self, context: Any) -> bool:
        """Профиль без входа, а слепок цел — подкладываем непросроченные куки."""
        if _has_session(context):
            return False
        state = self._stored_session()
        if state is None:
            return False
        now = time.time()
        cookies = [
            c
            for c in state.get("cookies", [])
            if c.get("expires", -1) in (-1, 0) or c.get("expires", 0) > now
        ]
        if not cookies:
            return False
        try:
            context.add_cookies(cookies)
        except Exception as exc:  # noqa: BLE001
            log.warning("сессия TikTok не восстановилась: %s", exc)
            return False
        return _has_session(context)

    # Короткий https-ресурс, чтобы прочитать настоящие Client Hints
    # (navigator.userAgentData на about:blank недоступен). Тот же домен, что и
    # цель, и через тот же прокси — лишних зависимостей не добавляет.
    _CH_PROBE_URL = "https://www.tiktok.com/robots.txt"
    _HIGH_ENTROPY = (
        "architecture",
        "bitness",
        "model",
        "uaFullVersion",
        "fullVersionList",
        "wow64",
    )

    @staticmethod
    def _dechrome(brands: list[dict[str, Any]] | None) -> list[dict[str, str]]:
        """HeadlessChrome → Google Chrome в списке брендов, версии не трогаем."""
        out: list[dict[str, str]] = []
        for item in brands or []:
            name = str(item.get("brand", ""))
            if name == "HeadlessChrome":
                name = "Google Chrome"
            out.append({"brand": name, "version": str(item.get("version", ""))})
        return out

    def _apply_identity(self, context: Any, page: Any) -> None:
        """Согласованная подмена UA/Client Hints для страницы.

        Встроенный Chromium в headless представляется ``HeadlessChrome`` — это
        явный маркер. Забираем у него настоящие бренды и полные версии, меняем
        ``HeadlessChrome`` на ``Google Chrome`` и версию Windows из «устройства»,
        строку UA приводим к обычному ``Chrome/<major>.0.0.0``. Ставим через CDP
        ``Network.setUserAgentOverride`` — родной механизм Chrome, он чинит и
        ``navigator.userAgentData``, и заголовки ``sec-ch-ua-*`` разом. Версия
        движка при этом остаётся настоящей.
        """
        hints = ", ".join(f'"{name}"' for name in self._HIGH_ENTROPY)
        script = (
            "async () => {"
            "  const d = navigator.userAgentData;"
            "  let hv = {};"
            "  try { if (d && d.getHighEntropyValues)"
            f"    hv = await d.getHighEntropyValues([{hints}]); }} catch (e) {{}}"
            "  return { ua: navigator.userAgent,"
            "           brands: (d && d.brands) ? d.brands : [], hv };"
            "}"
        )
        real: dict[str, Any] = {}
        try:
            if not str(page.url or "").startswith("http"):
                page.goto(self._CH_PROBE_URL, wait_until="domcontentloaded", timeout=20_000)
            real = page.evaluate(script)
        except Exception as exc:  # noqa: BLE001
            # Не смогли прочитать реальные хинты — почистим хотя бы строку UA.
            log.warning("не прочитал реальные Client Hints, чиню только UA: %s", exc)
            try:
                real = {"ua": page.evaluate("() => navigator.userAgent"), "brands": [], "hv": {}}
            except Exception:  # noqa: BLE001
                return

        ua = str(real.get("ua") or "")
        match = re.search(r"(?:Headless)?Chrome/(\d+)[\d.]*", ua)
        if not match:
            return  # не Chromium — не трогаем
        major = match.group(1)
        clean_ua = re.sub(r"(?:Headless)?Chrome/[\d.]+", f"Chrome/{major}.0.0.0", ua)

        hv = real.get("hv") or {}
        brands = self._dechrome(real.get("brands")) or [
            {"brand": "Not/A)Brand", "version": "8"},
            {"brand": "Chromium", "version": major},
            {"brand": "Google Chrome", "version": major},
        ]
        full_list = self._dechrome(hv.get("fullVersionList")) or [
            {"brand": b["brand"], "version": b["version"] if "." in b["version"] else f"{b['version']}.0.0.0"}
            for b in brands
        ]
        params: dict[str, Any] = {
            "userAgent": clean_ua,
            "platform": "Windows",
            "userAgentMetadata": {
                "brands": brands,
                "fullVersionList": full_list,
                "fullVersion": str(hv.get("uaFullVersion") or ""),
                "platform": "Windows",
                "platformVersion": self.device["platform_version"],
                "architecture": str(hv.get("architecture") or "x86"),
                "bitness": str(hv.get("bitness") or "64"),
                "wow64": bool(hv.get("wow64", False)),
                "model": str(hv.get("model") or ""),
                "mobile": False,
            },
        }
        try:
            session = context.new_cdp_session(page)
            session.send("Network.setUserAgentOverride", params)
        except Exception as exc:  # noqa: BLE001
            log.warning("не применил подмену UA: %s", exc)

    def _proxy_dict(self) -> dict[str, str] | None:
        if not self.proxy:
            return None
        parts = urlsplit(self.proxy)
        if not parts.hostname:
            return None
        host = parts.hostname
        server = f"{parts.scheme}://{host}:{parts.port}" if parts.port else f"{parts.scheme}://{host}"
        proxy: dict[str, str] = {"server": server}
        if parts.username:
            proxy["username"] = unquote(parts.username)
        if parts.password:
            proxy["password"] = unquote(parts.password)
        return proxy

    @staticmethod
    def _button(page: Any, name_re: re.Pattern[str], *css: str) -> Any | None:
        """Локатор кнопки: сперва по data-e2e/CSS, потом по подписи и роли.

        None — если ничего не подошло: пусть решает вызывающий.
        """
        for selector in css:
            loc = page.locator(selector)
            if loc.count():
                return loc.first
        by_role = page.get_by_role("button", name=name_re)
        if by_role.count():
            return by_role.first
        loc = page.locator("button", has_text=name_re)
        return loc.first if loc.count() else None

    def _dismiss_overlays(self, page: Any) -> None:
        """Гасит первый заход в Studio: онбординг-подсказки, баннеры, модалки.

        Всё best-effort: не нашли — молча дальше. Крутим пару кругов, потому что
        одна закрытая подсказка иногда открывает следующую.
        """
        closers = (
            '[class*="guide" i] [class*="close" i]',
            '[class*="tooltip" i] [class*="close" i]',
            '[class*="popup" i] button[class*="close" i]',
            'div[role="dialog"] [aria-label*="close" i]',
            'button[aria-label*="Закрыть"]',
            'button[aria-label*="Close"]',
        )
        for _ in range(3):
            hit = False
            try:
                btn = page.get_by_role("button", name=DISMISS_RE)
                if btn.count() and btn.first.is_visible():
                    btn.first.click(timeout=2000)
                    hit = True
            except Exception:  # noqa: BLE001
                pass
            if not hit:
                for sel in closers:
                    try:
                        x = page.locator(sel).first
                        if x.count() and x.is_visible():
                            x.click(timeout=1500)
                            hit = True
                            break
                    except Exception:  # noqa: BLE001
                        pass
            if not hit:
                break
            page.wait_for_timeout(400)
        try:
            page.keyboard.press("Escape")
        except Exception:  # noqa: BLE001
            pass

    def _expand_more(self, page: Any) -> None:
        """Разворачивает «Показать больше» — под ним черновик и часть настроек."""
        try:
            more = page.get_by_role("button", name=SHOW_MORE_RE)
            if not more.count():
                more = page.locator("button, div[role='button']", has_text=SHOW_MORE_RE)
            if more.count() and more.first.is_visible():
                more.first.click(timeout=2000)
                page.wait_for_timeout(600)
        except Exception:  # noqa: BLE001
            pass

    def _safe_click(self, page: Any, locator: Any) -> None:
        """Клик с попыткой убрать перехватчик и доскроллить до кнопки."""
        try:
            locator.scroll_into_view_if_needed(timeout=3000)
        except Exception:  # noqa: BLE001
            pass
        try:
            locator.click(timeout=8000)
            return
        except Exception as exc:  # noqa: BLE001
            log.info("клик не прошёл (%s), убираю всплывашки и пробую ещё раз", exc)
        self._dismiss_overlays(page)
        locator.click(timeout=8000)

    def _wait_enabled(self, locator: Any, say: Callable[[str], None]) -> None:
        deadline = time.time() + UPLOAD_TIMEOUT
        told = False
        while time.time() < deadline:
            try:
                if locator.is_enabled():
                    return
            except Exception:  # noqa: BLE001
                pass
            if not told:
                say("видео обрабатывается на стороне TikTok…")
                told = True
            time.sleep(UPLOAD_POLL)
        raise PublishError(
            "TikTok так и не разблокировал кнопку публикации — видео могло не "
            "обработаться. Проверь профиль, прежде чем повторять",
            retryable=False,
        )

    _CONFIRM_DIALOG_RE = re.compile(
        r"(продолжить публикацию|проверка .* не выполнена|continue (posting|to post)|"
        r"check (is|has) not (finished|completed))",
        re.I,
    )

    def _confirm_post(self, page: Any) -> bool:
        """Ловит и подтверждает диалог «Продолжить публикацию?».

        TikTok показывает его, если жмёшь «Опубликовать», пока идёт проверка.
        Работаем строго внутри всплывшего диалога, чтобы не ткнуть в фон.
        """
        try:
            for _ in range(6):
                page.wait_for_timeout(1000)
                if "tiktokstudio/upload" not in (page.url or ""):
                    return False  # уже ушли со страницы — модалки нет
                dialog = page.locator('div[role="dialog"], [class*="Modal" i]').filter(
                    has_text=self._CONFIRM_DIALOG_RE
                )
                if not dialog.count() or not dialog.first.is_visible():
                    continue
                btn = dialog.first.get_by_role("button", name=POST_RE)
                if not btn.count():
                    btn = dialog.first.locator("button", has_text=POST_RE)
                if btn.count() and btn.first.is_visible():
                    btn.first.click(timeout=5000)
                    return True
        except Exception as exc:  # noqa: BLE001
            log.warning("диалог подтверждения публикации не обработан: %s", exc)
        return False

    @staticmethod
    def _wait_checks(page: Any, say: Callable[[str], None]) -> None:
        """Ждём, пока уйдут «Идёт проверка …» — иначе TikTok переспрашивает."""
        deadline = time.time() + CHECKS_WAIT
        told = False
        while time.time() < deadline:
            try:
                if page.get_by_text(CHECKS_RE).count() == 0:
                    return
            except Exception:  # noqa: BLE001
                return
            if not told:
                say("жду, пока TikTok закончит проверки перед публикацией…")
                told = True
            time.sleep(CHECKS_POLL)

    def _confirm_published(self, page: Any, caption: str, say: Callable[[str], None]) -> str:
        """Убеждаемся, что ролик реально появился — иначе не врём про успех.

        Признаки: тост об успехе, уход на /content, и главное — новый ролик в
        списке публикаций (по подписи или по числу ссылок на видео).
        """
        head = (caption or "").strip().split("\n", 1)[0][:40]
        deadline = time.time() + 150
        while time.time() < deadline:
            try:
                if page.get_by_text(PUBLISHED_TOAST_RE).count():
                    say("TikTok подтвердил публикацию")
                    return "опубликовано"
            except Exception:  # noqa: BLE001
                pass
            if "tiktokstudio/content" in (page.url or ""):
                try:
                    body = page.evaluate("() => (document.querySelector('main')||document.body).innerText")
                    if head and head in body:
                        return "опубликовано"
                except Exception:  # noqa: BLE001
                    pass
            time.sleep(3)

        # Долистываем до списка публикаций и проверяем ещё раз явно.
        try:
            page.goto(
                "https://www.tiktok.com/tiktokstudio/content",
                wait_until="domcontentloaded",
                timeout=45_000,
            )
            page.wait_for_timeout(8000)
            body = page.evaluate("() => (document.querySelector('main')||document.body).innerText")
            if head and head in body:
                return "опубликовано"
        except Exception:  # noqa: BLE001
            pass

        raise PublishError(
            "TikTok не подтвердил публикацию: ролика нет в «Публикациях». Скорее "
            "всего сработала проверка контента (частая история с мультиками/"
            "сериалами) — проверь Studio вручную",
            retryable=True,
        )

    @staticmethod
    def _posted_url(page: Any) -> str:
        try:
            link = page.locator('a[href*="/video/"]').first
            if link.count():
                href = link.get_attribute("href") or ""
                return href if href.startswith("http") else f"https://www.tiktok.com{href}"
        except Exception:  # noqa: BLE001
            pass
        return ""

    def _read_username(self, page: Any) -> str:
        try:
            page.goto("https://www.tiktok.com/profile", wait_until="domcontentloaded", timeout=20_000)
            page.wait_for_url(re.compile(r"/@[\w.\-]+"), timeout=15_000)
            return page.url.split("/@", 1)[-1].split("?", 1)[0].strip("/")
        except Exception:  # noqa: BLE001
            return ""
