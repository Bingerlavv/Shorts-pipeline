"""Работа с текстом: перенос заголовков, экранирование для ffmpeg, хэштеги."""

from __future__ import annotations

import re
import textwrap

_WHITESPACE = re.compile(r"\s+")
# yt-dlp и ffmpeg подкрашивают вывод, и коды раскраски утекают в текст ошибки
_ANSI = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"  # CSI-последовательность целиком
    r"|\x1b[@-Z\\-_]"  # короткие escape-последовательности
    r"|\[[0-9;]{1,8}m"  # остаток раскраски, у которого ESC уже потерян
    r"|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]"  # прочие управляющие байты
)


def normalize(text: str) -> str:
    return _WHITESPACE.sub(" ", (text or "").strip())


def strip_ansi(text: str) -> str:
    """Убирает коды раскраски терминала — в панели они выглядят как мусор."""
    return _ANSI.sub("", text or "")


def wrap_title(text: str, max_chars_per_line: int = 20, max_lines: int = 3) -> str:
    """Разбивает заголовок на строки. Длинные немецкие слова не рвём."""
    text = normalize(text)
    if not text:
        return ""
    lines = textwrap.wrap(
        text,
        width=max(8, max_chars_per_line),
        break_long_words=False,
        break_on_hyphens=True,
    )
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip(" ,.;:") + "…"
    return "\n".join(lines)


def escape_filter_path(path: str) -> str:
    """Путь внутри фильтрографа (subtitles=, lut3d=). Windows-пути требуют заботы."""
    result = str(path).replace("\\", "/")
    result = result.replace(":", r"\:")
    result = result.replace("'", r"\'")
    return result


def clean_hashtags(tags: list[str] | None, limit: int = 12) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for tag in tags or []:
        cleaned = re.sub(r"[^0-9A-Za-zÄÖÜäöüß_]", "", str(tag))
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append("#" + cleaned)
        if len(result) >= limit:
            break
    return result


def merge_hashtags(caption: str, hashtags: list[str] | None) -> str:
    """Дописывает хэштеги к подписи, пропуская уже вставленные шаблоном.

    Шаблон подписи по умолчанию заканчивается на {hashtags}, поэтому слепое
    добавление списка выдаёт каждый тег дважды.
    """
    caption = (caption or "").rstrip()
    present = {word.lower() for word in caption.split()}
    missing = [tag for tag in (hashtags or []) if tag.lower() not in present]
    if not missing:
        return caption.strip()
    return f"{caption}\n\n{' '.join(missing)}".strip()


# В строке прокси обычно вписаны логин и пароль: http://user:pass@host:port.
# Библиотеки охотно вставляют её целиком в текст ошибки, а текст ошибки уезжает
# в базу, в панель и в телеграм — то есть пароль от прокси расходится по местам,
# где его никто не ждёт.
_CREDENTIALS_IN_URL = re.compile(r"(\w+://)[^/\s:@]+:[^/\s@]+@")


def redact_secrets(text: str) -> str:
    """Затирает логин и пароль внутри ссылок в тексте ошибки."""
    return _CREDENTIALS_IN_URL.sub(lambda m: m.group(1) + "***@", text or "")


def truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def safe_filename(text: str, max_length: int = 60) -> str:
    cleaned = re.sub(r"[^\w\-. ]", "", normalize(text), flags=re.UNICODE)
    cleaned = cleaned.strip().replace(" ", "_")
    return (cleaned or "clip")[:max_length]
