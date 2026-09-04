"""Показать браузерную выкладку в TikTok вживую — с видимым окном.

Открывает тот же профиль/устройство/прокси, что и настоящая выкладка
(`headless=False`), печатает, каким браузер видит себя со страницы, и водит
по TikTok Studio. Ничего не публикует, если не передать `--video` и `--post`.

Примеры:
    # свежий разовый профиль, страницы проверки отпечатка + TikTok Studio
    python scripts/tiktok_demo.py

    # существующий аккаунт: открыть его профиль и зайти в TikTok Studio
    python scripts/tiktok_demo.py --account 3

    # существующий аккаунт: реально прогнать загрузку ролика (в черновик)
    python scripts/tiktok_demo.py --account 3 --video storage/renders/clip.mp4

    # то же, но нажать «Опубликовать»
    python scripts/tiktok_demo.py --account 3 --video clip.mp4 --post
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "server"))

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        # line_buffering — чтобы прогресс было видно сразу, даже когда вывод
        # уходит в файл или конвейер, а не на терминал.
        _stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from app.config import settings  # noqa: E402
from app.db import session_scope  # noqa: E402
from app.models import Account  # noqa: E402
from app.providers.publish.base import PublishRequest  # noqa: E402
from app.providers.publish.tiktok_browser import TikTokBrowserPublisher, _has_session  # noqa: E402
from app.providers.publish.tiktok_device import make_device  # noqa: E402
from app.utils.crypto import decrypt_json  # noqa: E402

STUDIO = "https://www.tiktok.com/tiktokstudio/upload?from=creator_center"

FINGERPRINT_JS = """() => {
  const d = navigator.userAgentData;
  return {
    userAgent: navigator.userAgent,
    language: navigator.language,
    languages: navigator.languages,
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    screen: [screen.width, screen.height],
    colorSchemeDark: matchMedia('(prefers-color-scheme: dark)').matches,
    webdriver: navigator.webdriver,
    hardwareConcurrency: navigator.hardwareConcurrency,
    deviceMemory: navigator.deviceMemory,
    platform: navigator.platform,
    brands: d ? d.brands : null,
    highEntropy: d ? 'ask' : null,
  };
}"""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="tiktok_demo", description="Видимый прогон браузерной выкладки")
    p.add_argument("--account", default="", help="id или часть имени существующего браузерного аккаунта")
    p.add_argument("--video", type=Path, help="ролик для реальной загрузки (иначе только открыть Studio)")
    p.add_argument("--post", action="store_true", help="нажать «Опубликовать» (иначе черновик)")
    p.add_argument("--caption", default="", help="текст подписи (иначе метка demo)")
    p.add_argument("--url", default="", help="куда зайти вместо страниц проверки")
    p.add_argument("--inspect", action="store_true",
                   help="на странице Studio выгрузить кнопки/поля/data-e2e — сверить селекторы")
    p.add_argument("--keep", type=int, default=90, help="сколько секунд держать окно открытым в конце")
    p.add_argument("--check-url", default="https://bot.sannysoft.com/", help="страница проверки на бота")
    return p.parse_args(argv)


def pick_publisher(needle: str) -> TikTokBrowserPublisher:
    if not needle:
        slug = f"demo-{int(time.time())}"
        creds = {
            "profile_dir": f"tiktok/{slug}",
            "proxy": "",
            "channel": settings.tiktok_browser_channel,
            "device": make_device(slug),
        }
        print(f"разовый профиль: storage/tiktok/{slug}")
        return TikTokBrowserPublisher(creds, {"auth": "patchright"})

    with session_scope() as db:
        rows = [
            a for a in db.query(Account).filter(Account.platform == "tiktok").all()
            if (a.meta or {}).get("auth") == "patchright"
        ]
        if needle.isdigit():
            acc = next((a for a in rows if a.id == int(needle)), None)
        else:
            hits = [a for a in rows if needle.lower().lstrip("@") in a.name.lower()]
            acc = hits[0] if len(hits) == 1 else None
        if acc is None:
            names = ", ".join(f"{a.id}:{a.name}" for a in rows) or "нет"
            raise SystemExit(f"аккаунт «{needle}» не найден. Есть: {names}")
        print(f"аккаунт: {acc.name} (id {acc.id})")
        return TikTokBrowserPublisher(decrypt_json(acc.credentials_enc), acc.meta)


def dump_identity(page) -> None:  # noqa: ANN001
    fp = page.evaluate(FINGERPRINT_JS)
    he = {}
    try:
        he = page.evaluate(
            "async () => (navigator.userAgentData "
            "? await navigator.userAgentData.getHighEntropyValues("
            "['platform','platformVersion','architecture','bitness','uaFullVersion','fullVersionList']) : {})"
        )
    except Exception:  # noqa: BLE001
        pass
    print("\n─ как браузер видит себя ─────────────────────────────")
    print(f"  User-Agent      : {fp['userAgent']}")
    print(f"  navigator.webdriver: {fp['webdriver']}")
    print(f"  languages       : {fp['languages']}  (Intl TZ: {fp['timezone']})")
    print(f"  screen          : {fp['screen']}   dark: {fp['colorSchemeDark']}")
    print(f"  cores / memory  : {fp['hardwareConcurrency']} / {fp['deviceMemory']}")
    print(f"  UA brands       : {fp['brands']}")
    if he:
        print(f"  CH platform     : {he.get('platform')} {he.get('platformVersion')} "
              f"{he.get('architecture')}/{he.get('bitness')}")
        print(f"  CH full versions: {he.get('fullVersionList')}")
    print("─────────────────────────────────────────────────────\n")


def dump_studio_dom(page) -> None:  # noqa: ANN001
    data = page.evaluate("""() => {
      const vis = el => { const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== 'hidden'; };
      const buttons = [...document.querySelectorAll('button, [role=\"button\"]')]
        .filter(vis).map(b => ({
          text: (b.innerText || b.textContent || '').trim().slice(0, 40),
          e2e: b.getAttribute('data-e2e') || (b.closest('[data-e2e]')||{}).getAttribute?.('data-e2e') || '',
          disabled: b.disabled || b.getAttribute('aria-disabled') === 'true',
        })).filter(b => b.text || b.e2e);
      const edits = [...document.querySelectorAll('[contenteditable=\"true\"]')].filter(vis).map(e => ({
          cls: (e.className || '').toString().slice(0, 80),
          role: e.getAttribute('role') || '',
          e2e: (e.closest('[data-e2e]')||{}).getAttribute?.('data-e2e') || '',
      }));
      const e2es = [...new Set([...document.querySelectorAll('[data-e2e]')].filter(vis)
          .map(e => e.getAttribute('data-e2e')))];
      return { buttons, edits, e2es };
    }""")
    print("\n─ DOM TikTok Studio ─────────────────────────────────")
    print("  кнопки:")
    for b in data["buttons"]:
        flag = " [disabled]" if b["disabled"] else ""
        print(f"    «{b['text']}»  e2e={b['e2e'] or '-'}{flag}")
    print("  contenteditable:")
    for e in data["edits"]:
        print(f"    role={e['role'] or '-'}  e2e={e['e2e'] or '-'}  class={e['cls']}")
    print("  все data-e2e на странице:")
    print("    " + ", ".join(data["e2es"]))
    print("─────────────────────────────────────────────────────\n")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.video and not args.video.exists():
        raise SystemExit(f"ролик не найден: {args.video}")

    publisher = pick_publisher(args.account)
    dev = publisher.device
    print(f"устройство: {dev['locale']} / {dev['timezone_id']} / "
          f"{dev['screen']['width']}x{dev['screen']['height']} / Win {dev['platform_version']} / "
          f"{dev['color_scheme']}")
    print(f"канал браузера: {publisher.channel or 'встроенный Chromium'}   "
          f"прокси: {publisher.proxy.split('@')[-1] or 'нет'}\n")

    if args.video and args.inspect:
        # Загрузить ролик и выгрузить DOM формы, ничего не отправляя.
        from app.providers.publish.tiktok_browser import CAPTION_CSS  # noqa: E402

        with publisher._browser(headless=False) as (context, page):  # noqa: SLF001
            print("открываю TikTok Studio…")
            page.goto(STUDIO, wait_until="domcontentloaded", timeout=60_000)
            publisher._dismiss_overlays(page)  # noqa: SLF001
            page.locator('input[type="file"]').first.set_input_files(str(args.video))
            print(f"файл отправлен: {args.video.name}; жду форму…")
            page.locator(CAPTION_CSS).first.wait_for(state="visible", timeout=180_000)
            publisher._dismiss_overlays(page)  # noqa: SLF001
            publisher._expand_more(page)  # noqa: SLF001
            time.sleep(3)
            dump_studio_dom(page)
            print(f"смотри окно, {args.keep} с…")
            time.sleep(args.keep)
        return 0

    if args.video:
        # Настоящий прогон загрузки — видимым окном, по умолчанию в черновик.
        req = PublishRequest(
            video_path=args.video,
            title="",
            description=args.caption or "demo upload — можно удалить",
            hashtags=[],
            extra={"headless": False, "publish_now": args.post},
        )
        res = publisher.publish(
            req,
            on_progress=lambda f, m: print(f"  [{f * 100:3.0f}%] {m}"),
            on_log=lambda m: print(f"  {m}"),
        )
        print(f"\nитог: {res.raw.get('note')}  {res.url}")
        print(f"окно закроется через {args.keep} с…")
        time.sleep(args.keep)
        return 0

    # Без ролика — просто показать: проверка на бота, отпечаток, TikTok Studio.
    with publisher._browser(headless=False) as (context, page):  # noqa: SLF001
        if not args.inspect:
            target = args.url or args.check_url
            print(f"открываю {target}")
            page.goto(target, wait_until="domcontentloaded", timeout=60_000)
            dump_identity(page)
            time.sleep(6)

        print("открываю TikTok Studio…")
        page.goto(STUDIO, wait_until="domcontentloaded", timeout=60_000)
        logged = _has_session(context)
        print(f"сессия TikTok в профиле: {'есть' if logged else 'нет (будет страница входа)'}")

        if args.inspect and logged:
            time.sleep(8)  # дать форме прорисоваться
            dump_studio_dom(page)

        print(f"\nсмотри окно. Закроется через {args.keep} с (Ctrl+C — раньше).")
        time.sleep(args.keep)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nпрервано")
        sys.exit(130)
