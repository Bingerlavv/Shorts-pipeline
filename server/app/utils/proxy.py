"""Приведение строки прокси к виду, который понимают библиотеки.

Продавцы прокси выдают их через двоеточия — `host:port:логин:пароль`, — а
requests, httpx и instagrapi ждут `схема://логин:пароль@host:port`. Разница
чисто в записи, но без перевода вход просто не состоится, причём с ошибкой
про недоступный узел, а не про формат.
"""

from __future__ import annotations

from urllib.parse import quote

SCHEMES = ("http", "https", "socks4", "socks5", "socks5h")


class ProxyFormatError(ValueError):
    pass


def _port(value: str) -> int | None:
    if not value.isdigit():
        return None
    number = int(value)
    return number if 1 <= number <= 65535 else None


def normalize_proxy(raw: str) -> str:
    """Возвращает `схема://логин:пароль@host:port`. Пустая строка остаётся пустой.

    Понимает четыре записи, которые встречаются в жизни:
      194.71.107.74:11368:user:pass
      http://194.71.107.74:11368:user:pass
      http://user:pass@194.71.107.74:11368
      194.71.107.74:11368
    """
    text = (raw or "").strip()
    if not text:
        return ""

    scheme = "http"
    if "://" in text:
        scheme, _, text = text.partition("://")
        scheme = scheme.lower()
        if scheme not in SCHEMES:
            raise ProxyFormatError(
                f"неизвестная схема прокси: {scheme}. Бывают: {', '.join(SCHEMES)}"
            )

    # Запись с «собакой» уже правильная — её только проверяем. Но «собака»
    # может оказаться и внутри пароля в записи через двоеточия, и тогда слева
    # от неё остаётся не адрес, а огрызок. Отличаем по адресу: у настоящей
    # записи в нём двоеточий уже нет, всё лишнее ушло в логин с паролем.
    if "@" in text:
        auth, _, host_port = text.rpartition("@")
        host, _, port = host_port.rpartition(":")
        if host and ":" not in host and _port(port) is not None:
            return f"{scheme}://{auth}@{host}:{port}"

    parts = text.split(":")
    if len(parts) == 2:
        host, port = parts
        if _port(port) is None:
            raise ProxyFormatError(f"«{port}» не похоже на порт")
        return f"{scheme}://{host}:{port}"

    if len(parts) == 4:
        # Порядок у продавцов разный, поэтому опознаём по тому, что похоже на
        # порт: во втором поле — host:port:логин:пароль, в четвёртом — наоборот.
        if _port(parts[1]) is not None:
            host, port, user, password = parts
        elif _port(parts[3]) is not None:
            user, password, host, port = parts
        else:
            raise ProxyFormatError(
                f"в «{raw}» не нашёл порт. Ожидаю host:port:логин:пароль"
            )
        if not host or not user:
            raise ProxyFormatError(f"в «{raw}» пустой адрес или логин")
        # Логин и пароль уходят внутрь ссылки, поэтому «@», «:» и «/» в них
        # надо закодировать — иначе ссылка разберётся не там, где нужно.
        return f"{scheme}://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}"

    raise ProxyFormatError(
        f"не понял запись прокси «{raw}». Ожидаю host:port:логин:пароль "
        "или схема://логин:пароль@host:port"
    )
