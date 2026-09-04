"""Пер-аккаунтное «устройство» для браузерной выкладки в TikTok.

Согласованный набор параметров, замороженный на профиль: язык, часовой пояс,
размер экрана, оформление, версия Windows. UA и Client Hints при этом берутся
у реального встроенного Chromium — меняется только версия Windows (10 ↔ 11).
Так подмена остаётся достоверной: рассинхрон между ``navigator.userAgent`` и
``sec-ch-ua`` детектится легче честного дефолта, а версия движка в UA всегда
совпадает с настоящей.

Набор детерминирован от имени профиля — тот же аккаунт всегда получает то же
устройство, даже если оно ещё не сохранено в credentials.
"""

from __future__ import annotations

import random

# Язык и пояс держим парами: язык интерфейса и таймзона не должны спорить.
# Если у аккаунта гео-прокси — локаль и пояс лучше задать руками под него,
# случайные тут только для «пусто».
_LOCALE_TZ: list[tuple[str, str]] = [
    ("ru-RU", "Europe/Moscow"),
    ("ru-RU", "Europe/Samara"),
    ("en-US", "America/New_York"),
    ("en-US", "America/Chicago"),
    ("en-US", "America/Denver"),
    ("en-US", "America/Los_Angeles"),
    ("en-GB", "Europe/London"),
    ("de-DE", "Europe/Berlin"),
    ("fr-FR", "Europe/Paris"),
    ("pl-PL", "Europe/Warsaw"),
    ("uk-UA", "Europe/Kyiv"),
    ("kk-KZ", "Asia/Almaty"),
]

# Реальные размеры десктопных экранов Windows, с перекосом в сторону 1080p.
_SCREENS: list[tuple[int, int]] = [
    (1920, 1080), (1920, 1080), (1920, 1080), (1920, 1080), (1920, 1080),
    (1366, 768), (1366, 768), (1366, 768),
    (1536, 864), (1536, 864),
    (1600, 900), (1440, 900), (1680, 1050),
    (2560, 1440), (1280, 720), (1360, 768),
]

# sec-ch-ua-platform-version для Windows: "10.0.0" — Win10 (и Win11 до 22H2),
# "13/14/15.0.0" — более новые Win11. В природе примерно поровну.
_PLATFORM_VERSIONS: list[str] = ["10.0.0", "10.0.0", "10.0.0", "13.0.0", "14.0.0", "15.0.0"]

_SCHEMES: list[str] = ["light", "light", "light", "dark"]

# Высота хрома браузера (вкладки + адресная строка) — вычитаем из экрана,
# чтобы вьюпорт был правдоподобным.
_CHROME_BARS: list[int] = [79, 87, 113, 121, 139]


def make_device(seed: str, *, locale: str = "", timezone_id: str = "") -> dict:
    """Собирает устройство. Явные locale/timezone_id побеждают случайный выбор."""
    rnd = random.Random(f"tiktok-device::{seed}")
    loc, tz = rnd.choice(_LOCALE_TZ)
    loc = locale.strip() or loc
    tz = timezone_id.strip() or tz

    screen_w, screen_h = rnd.choice(_SCREENS)
    return {
        "locale": loc,
        "timezone_id": tz,
        "screen": {"width": screen_w, "height": screen_h},
        "viewport": {
            "width": screen_w,
            "height": max(600, screen_h - rnd.choice(_CHROME_BARS)),
        },
        "color_scheme": rnd.choice(_SCHEMES),
        "platform_version": rnd.choice(_PLATFORM_VERSIONS),
    }
