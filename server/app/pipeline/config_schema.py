"""Конфигурация конвейера.

Один словарь описывает поведение всех модулей. Пресет задаёт базу, проект и
отдельный фрагмент могут переопределить любую ветку (глубокое слияние).
"""

from __future__ import annotations

import copy
from typing import Any

DEFAULT_CONFIG: dict[str, Any] = {
    # --- загрузка исходника ---
    "ingest": {
        "max_height": 1080,
        "format": "bestvideo[height<=?1080]+bestaudio/best",
        # Cookies нужны, когда YouTube требует «подтверди, что не бот», а также
        # для приватных, возрастных и спонсорских видео.
        # ВАЖНО: на Windows читается только firefox. Chrome и Edge с версии 127
        # шифруют cookies App-Bound Encryption, и yt-dlp их расшифровать не может
        # (yt-dlp#10927) — для них выгружай cookies.txt и указывай cookies_file.
        "cookies_from_browser": "",  # firefox | chrome | edge | brave | opera | vivaldi
        "cookies_file": "",  # путь к cookies.txt в формате Netscape — работает всегда
        # YouTube отдаёт разным клиентам разные наборы форматов и по-разному
        # требует подтверждения «я не бот». Перебор клиентов подряд проходит
        # проверку без cookies: android_vr её не запускает, а default и tv_simply
        # дают полный набор качеств. Порядок важен — первым идёт самый полный.
        "player_clients": "default,android_vr,tv_simply,web_safari",
        # Ссылки на медиа у YouTube привязаны к адресу, с которого запрошен
        # плеер. Если VPN между двумя запросами переключит узел, отдача падает
        # с 403. Здесь можно закрепить один прокси на обе половины загрузки
        # (http://127.0.0.1:10809, socks5://…) либо написать direct, чтобы
        # игнорировать системный прокси. Пусто — как настроено в системе.
        "proxy": "",
        # Качать диапазонами по столько мегабайт вместо одного длинного запроса.
        # 0 — как обычно. Помогает, когда сплошная загрузка ловит 403, а короткие
        # куски проходят: такое встречается на адресах под ограничением.
        "http_chunk_size_mb": 0,
        "extract_audio": True,
        "detect_subtitles": True,  # эвристика: есть ли вшитые субтитры
    },
    # --- транскрипция ---
    "transcribe": {
        "provider": "auto",  # auto | local | openai | deepgram
        "model": "",  # пусто = из настроек окружения
        "language": "",  # пусто = автоопределение
        "word_timestamps": True,
        "vad_filter": True,
        "beam_size": 5,
    },
    # --- поиск самостоятельных фрагментов ---
    "analyze": {
        # Пусто — берём из SHORTS_LLM_PROVIDER. Здесь можно выбрать другого:
        # например, искать фрагменты сильной облачной моделью, а тексты писать
        # локальной, или наоборот.
        "provider": "",
        "model": "",
        "min_duration": 15.0,
        "max_duration": 45.0,
        "target_count": 10,
        "min_score": 0.55,
        "merge_adjacent": True,  # склеивать соседние моменты ради длительности
        "max_merge_gap": 2.5,  # максимальная пауза между склеиваемыми моментами, с
        "output_language": "de",  # язык заголовков/описаний
        "chunk_minutes": 25,  # длина куска транскрипта, отдаваемого модели за раз
        "extra_instructions": "",  # свободный текст в промпт: тема канала, тон, запреты
    },
    # --- плоская нарезка ---
    # Замена поиску фрагментов, а не дополнение к нему: при enabled конвейер
    # пропускает и транскрипцию, и модель, и просто режет исходник на куски
    # равной длины. Так разбирают материал, который и так состоит из
    # самостоятельных частей — запись эфира, летсплей, длинное интервью, — где
    # искать «сильные моменты» нечего, а нужны ровные шортсы подряд.
    "chunks": {
        "enabled": False,
        "duration": 60.0,  # длина куска, с; 0 — весь ролик одним куском
        "overlap": 0.0,  # нахлёст с предыдущим куском, с
        # Хвост короче этого приклеивается к предыдущему куску, а не выходит
        # отдельным огрызком: пятисекундный шортс не нужен никому.
        "min_tail": 20.0,
        "skip_start": 0.0,  # пропустить в начале — заставка, приветствие
        "skip_end": 0.0,  # пропустить в конце — титры, прощание
        "limit": 0,  # взять не больше стольких кусков, 0 — сколько получится
        # Подставляются {title} — название проекта, {index} — номер куска,
        # {total} — сколько их всего, {start} — начало в виде 12:34.
        "title_template": "{title} — часть {index}",
    },
    # --- нарезка ---
    "cut": {
        "padding_before": 0.3,
        "padding_after": 0.4,
        "snap_to_words": True,  # двигать границы к паузам между словами
        "crossfade": 0.12,  # склейка нескольких диапазонов внутри одного фрагмента
    },
    # --- тексты для площадок ---
    # Поиск фрагментов уже пишет заголовок и описание, но там задача другая:
    # отобрать материал. Здесь текст переписывается отдельным запросом с целью
    # охвата — и его можно перегенерировать, не трогая нарезку.
    "caption": {
        "enabled": False,
        "provider": "",  # пусто = как в SHORTS_LLM_PROVIDER
        "model": "",  # пусто = модель по умолчанию у провайдера
        "language": "de",
        "hashtag_count": 12,
        "title_max_chars": 80,
        # Своими словами: тема канала, тон, что запрещено упоминать.
        "extra_instructions": "",
        # Перед публикацией переписать заново, даже если текст уже есть.
        "before_publish": False,
    },
    # --- монтаж ---
    "edit": {
        "output": {
            "width": 1080,
            "height": 1920,
            "fps": 30,
            "crf": 19,
            "x264_preset": "medium",
            "audio_bitrate": "192k",
            "hardware_encoder": "",  # "" | h264_nvenc | hevc_nvenc
        },
        "framing": {
            "mode": "crop",  # crop | blur_pad | fit
            "focus_x": 0.5,  # центр кадрирования по горизонтали, 0..1
            "focus_y": 0.5,
            "blur_strength": 28,
            # Только для fit: куда поставить горизонтальное видео между полосами.
            # 0.5 — по центру, меньше — выше, чтобы сверху осталось место под
            # заголовок. Полосы можно перекрасить, чёрные подходят не всем.
            "fit_y": 0.42,
            "pad_color": "black",
        },
        # Фон под уменьшенным исходником: типовой формат шортса, где сверху
        # играет сам ролик, а под ним — посторонний видеоряд.
        "background": {
            "enabled": False,
            # Список фонов; на каждый фрагмент берётся один. Выбор привязан к
            # номеру фрагмента, поэтому пересборка даёт тот же фон.
            "asset_ids": [],
            "clip_scale": 0.86,  # ширина исходника относительно кадра, 0..1
            "clip_y": 0.1,  # верхний край исходника, доля высоты кадра
            "corner_radius": 44,  # 0 — прямые углы
            "blur": 0,  # размытие фона, sigma
            "dim": 0.0,  # затемнение фона, 0..1
            "audio": "clip",  # clip — только звук исходника | mix — подмешать фон
            "audio_gain": 0.12,
        },
        "speed": {
            "enabled": True,
            "factor": 1.03,
            "randomize": True,  # взять случайное значение из диапазона на каждый фрагмент
            "min": 1.01,
            "max": 1.05,
            "pitch_correction": True,
        },
        "zoom": {
            "enabled": True,
            "factor": 1.08,
            "mode": "static",  # static | kenburns
            "end_factor": 1.18,  # только для kenburns
            # К чему применять приближение в режиме полос:
            #   frame  — к готовому кадру: видео крупнее, полосы тоньше;
            #   source — к исходнику: в кадр попадает меньше сцены,
            #            полосы остаются той же высоты.
            # В режимах crop и blur_pad разницы нет, кадр и так во весь экран.
            "apply_to": "frame",  # frame | source
        },
        "mirror": {
            "enabled": True,
            "only_if_no_subtitles": True,  # не зеркалить, если в кадре есть текст
        },
        "color": {
            "enabled": True,
            "lut_asset_id": None,
            "brightness": 0.0,  # -1..1
            "contrast": 1.03,  # 0..3
            "saturation": 1.06,  # 0..3
            "gamma": 1.0,
            "sharpen": 0.0,  # 0..1.5
            "vignette": False,
        },
        "mask": {
            "enabled": False,
            "asset_id": None,  # PNG с альфой или видео-оверлей
            "opacity": 1.0,
            "fit": "cover",  # cover | contain | stretch
            "blend": "normal",  # normal | screen | overlay | multiply
        },
        "banner": {
            "enabled": False,
            "asset_id": None,
            "chromakey": {
                "enabled": True,
                # chromakey — для зелёнки: он игнорирует яркость и на однотонном
                # цветном фоне выедает белый текст. colorkey сравнивает полный
                # RGB и подходит для баннеров на плоской заливке.
                "mode": "chromakey",  # chromakey | colorkey
                "color": "0x00FF00",
                "similarity": 0.18,
                "blend": 0.08,
                "despill": True,
            },
            # Обрезка исходного файла долями его размера. Нужна, когда баннер
            # выдан на большом полотне с запасом пустого фона: без неё масштаб
            # приходится задирать, и видимая часть всё равно выходит мелкой.
            "crop": {
                "enabled": False,
                "x": 0.0,
                "y": 0.0,
                "width": 1.0,
                "height": 1.0,
            },
            "position": "top",  # top | bottom | center | custom
            "x": 0,
            "y": 0,
            "scale": 1.0,
            "start": 0.0,
            "duration": 0.0,  # 0 = до конца
            "loop": True,
        },
        "title": {
            "enabled": True,
            "only_if_absent": True,  # рисовать, только если в исходнике нет заголовка
            "text": "",  # пусто = берём title_de фрагмента
            "font_asset_id": None,
            "font_size": 64,
            "color": "white",
            "border_width": 4,
            "border_color": "black",
            "box": True,
            "box_color": "black@0.45",
            "box_padding": 18,
            "position": "top",  # top | bottom | center
            "margin": 200,
            "max_chars_per_line": 20,
            "start": 0.0,
            "duration": 0.0,  # 0 = весь ролик
        },
        "subtitles": {
            "enabled": False,
            "burn": True,
            "font_asset_id": None,
            "font_size": 56,
            "color": "&H00FFFFFF",
            "outline_color": "&H00000000",
            "outline": 3,
            "position": "bottom",
            "margin": 320,
            "max_chars_per_line": 24,
            "uppercase": False,
            "highlight_active_word": False,
        },
    },
    # --- публикация ---
    # Аккаунтов здесь нет намеренно: куда уходит проект, задаётся связью
    # «аккаунт ↔ проект» на странице аккаунтов, а не пресетом. Пресет описывает
    # только то, как оформить и когда отправить.
    "publish": {
        "auto": False,
        "youtube": {
            "privacy": "private",  # private | unlisted | public
            "category_id": "22",
            "made_for_kids": False,
            "title_suffix": " #Shorts",
            "description_template": "{description}\n\n{hashtags}",
            "default_tags": [],
        },
        "instagram": {
            "share_to_feed": True,
            "caption_template": "{title}\n\n{description}\n\n{hashtags}",
        },
        "tiktok": {
            # draft  — ролик приходит во «Входящие» приложения, публикует человек
            #          одним касанием. Работает сразу, без проверок приложения.
            # direct — сразу в профиль, без человека. Но пока приложение не прошло
            #          аудит TikTok, опубликованное так видно только автору —
            #          то есть охвата не будет. Включать после аудита.
            "mode": "draft",  # draft | direct
            # Только для direct и только из разрешённого аккаунту:
            # PUBLIC_TO_EVERYONE | MUTUAL_FOLLOW_FRIENDS | FOLLOWER_OF_CREATOR | SELF_ONLY.
            # Пусто — взять самый закрытый из доступных.
            "privacy": "",
            "disable_comment": False,
            "disable_duet": False,
            "disable_stitch": False,
            # Отдельного описания у TikTok нет — только подпись под роликом.
            "caption_template": "{title}\n\n{hashtags}",
        },
        "schedule": {
            "enabled": False,
            # times — выкладывать в названные часы, это привычная сетка выхода.
            # spacing — просто разносить ролики интервалом внутри окна суток.
            "mode": "times",
            "times": ["10:00", "15:00", "20:00"],  # только для режима times
            # Дни недели, 1 — понедельник, 7 — воскресенье. Пусто — все дни.
            "weekdays": [],
            # Всё, что ниже, относится к режиму spacing. Интервал считается ПО
            # АККАУНТУ: один ролик может уйти на три площадки разом, а вот два
            # разных ролика подряд на один аккаунт — это уже похоже на спам.
            "spacing_minutes": 180,
            "window_start": "",  # «10:00» — раньше не публиковать, пусто — когда угодно
            "window_end": "",  # «22:00» — позже перенести на завтра
            # Общее для обоих режимов.
            "start_offset_minutes": 30,  # задержка самой первой публикации
            "daily_limit": 0,  # сколько публикаций в сутки на аккаунт, 0 — без лимита
        },
    },
}


def deep_merge(base: dict[str, Any], override: dict[str, Any] | None) -> dict[str, Any]:
    """Рекурсивно накладывает override на base, не мутируя аргументы."""
    result = copy.deepcopy(base)
    if not override:
        return result
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def resolve_config(*layers: dict[str, Any] | None) -> dict[str, Any]:
    """Собирает итоговый конфиг: DEFAULT → пресет → проект → фрагмент."""
    config = copy.deepcopy(DEFAULT_CONFIG)
    for layer in layers:
        config = deep_merge(config, layer)
    return config
