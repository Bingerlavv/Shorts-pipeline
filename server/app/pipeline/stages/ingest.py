"""Загрузка исходника по ссылке и снятие его параметров."""

from __future__ import annotations

import logging
import re
import threading
import time
from pathlib import Path
from typing import Any

from ...config import settings
from ...media import extract_audio, extract_thumbnail, has_burned_subtitles, probe_media
from ...models import Project, ProjectStatus
from ...queue import enqueue
from ...utils.text import safe_filename
from ..context import JobContext
from ..registry import handler
from ..resolve import config_for_project

log = logging.getLogger(__name__)


class IngestError(RuntimeError):
    pass


# Воркер держит несколько потоков, и два проекта спокойно уходили качаться
# одновременно — для YouTube это прямая дорога к 429. Загрузки с него идут по
# одной и с паузой; монтаж и распознавание при этом остаются параллельными.
MIN_GAP = 20.0
_youtube_gate = threading.Lock()
_last_youtube_call = 0.0


def _is_youtube(url: str) -> bool:
    return "youtube.com" in url or "youtu.be" in url


class _YtLogger:
    """Проводит предупреждения yt-dlp в журнал задачи.

    Раньше стоял no_warnings, и самое важное сообщение — 429 Too Many Requests —
    не доходило никуда. В трассировке оставался только вызванный им 403, и два
    дня подряд причина искалась не там. Повторы гасим: yt-dlp любит сказать одно
    и то же по разу на каждый клиент.
    """

    def __init__(self, ctx: JobContext) -> None:
        self._ctx = ctx
        self._seen: set[str] = set()

    def debug(self, message: str) -> None:
        pass

    def info(self, message: str) -> None:
        pass

    def error(self, message: str) -> None:
        pass  # ошибку и так поднимет исключение, второй раз незачем

    def warning(self, message: str) -> None:
        text = re.split(r"\s*(?:See |Also see |Use --)", str(message))[0]
        text = text.replace("WARNING:", "").strip()
        if text and text not in self._seen:
            self._seen.add(text)
            self._ctx.info(f"yt-dlp: {text}")


def _is_rate_limited(raw: str) -> bool:
    low = raw.lower()
    return (
        "429" in low
        or "too many requests" in low
        or "sign in to confirm you" in low
        or "not a bot" in low
    )


COOKIES_HOWTO = (
    "Возьми cookies одним из двух способов. Проще: поставь Firefox, зайди в нём "
    "на youtube.com под своим аккаунтом, закрой браузер и впиши firefox в поле "
    "«Cookies из браузера». Либо выгрузи cookies.txt расширением вроде "
    "«Get cookies.txt LOCALLY» и укажи путь к файлу в поле «Файл cookies». "
    "Chrome и Edge на Windows не подходят: с версии 127 они шифруют cookies так, "
    "что yt-dlp их не читает."
)


def _is_forbidden(raw: str) -> bool:
    low = raw.lower()
    return "403" in low and "forbidden" in low


def _is_retryable(raw: str) -> bool:
    """Ошибка, которую лечит повторный заход, а не правка настроек.

    Кроме 403 сюда попадает «нет подходящего формата»: клиент, до которого
    достучались, вернул огрызок плеера — одни раскадровки без дорожек. Другой
    клиент на то же видео обычно отдаёт нормальный набор.
    """
    return _is_forbidden(raw) or "requested format is not available" in raw.lower()


def _explain_download_error(url: str, raw: str, browser: str) -> str:
    """Переводит простыню от yt-dlp в одну понятную строку с подсказкой.

    Сообщения yt-dlp тянут за собой ссылки на wiki и советы для командной
    строки, которые в панели бесполезны: там нет флагов, там есть настройки.
    """
    low = raw.lower()

    if "failed to decrypt with dpapi" in low or "could not copy chrome" in low:
        return (
            f"Не удалось прочитать cookies из {browser or 'браузера'}: на Windows Chrome и Edge "
            f"шифруют их так, что yt-dlp не справляется. {COOKIES_HOWTO}"
        )
    if "429" in low or "too many requests" in low:
        return (
            "YouTube ограничил частоту запросов с этого адреса (429 Too Many Requests). "
            "Так бывает после нескольких загрузок подряд или череды повторов неудачной. "
            "Ограничение снимается само, но повторные попытки его продлевают. "
            "Быстрее всего помогает смена сервера VPN."
        )
    if "sign in to confirm you" in low or "not a bot" in low:
        if browser:
            tail = (
                "Если вход есть и браузер закрыт — выгрузи cookies.txt расширением вроде "
                "«Get cookies.txt LOCALLY» и укажи путь в поле «Файл cookies»."
                if browser.lower() == "firefox"
                else COOKIES_HOWTO
            )
            return (
                f"YouTube требует подтверждения «я не бот», хотя cookies берутся из {browser}. "
                f"Проверь, что в этом браузере выполнен вход на youtube.com и что он полностью "
                f"закрыт во время загрузки — иначе база cookies заблокирована. {tail}"
            )
        return f"YouTube требует подтверждения «я не бот» — без cookies это видео не отдаётся. {COOKIES_HOWTO}"
    if "403" in low and "forbidden" in low:
        return (
            f"YouTube выдал ссылки на {url}, но отказался отдавать по ним данные "
            "(403 Forbidden). Почти всегда это значит, что не выдан proof-of-origin "
            "токен: ссылка без него подписывается, но не работает. Проверь, что "
            "служба токенов отвечает — открой http://127.0.0.1:4416/ping. Если не "
            "отвечает, запусти проект через scripts\run.ps1, он поднимает её сам. "
            "И держи yt-dlp свежим: месячной давности выпуск уже не качает."
        )
    if "video unavailable" in low:
        return "Видео недоступно: удалено, скрыто автором или заблокировано в этом регионе."
    if "private video" in low:
        return "Видео приватное. Нужны cookies аккаунта, у которого есть доступ."
    if "age" in low and "confirm" in low:
        return "Возрастное ограничение. Укажи браузер в поле «Cookies из браузера»."
    if "members-only" in low or "join this channel" in low:
        return "Видео только для спонсоров канала. Нужны cookies аккаунта с подпиской."
    if "requested format is not available" in low:
        return (
            "YouTube не отдал ни одной дорожки этого видео — только раскадровки. "
            "Так отвечают на запросы с адреса, попавшего под ограничение по частоте: "
            "подожди и не запускай всё разом. Если то же самое на заведомо рабочем "
            "видео — дело точно в адресе, смени сервер VPN."
        )
    if "unable to download webpage" in low or "getaddrinfo" in low or "timed out" in low:
        return f"Не удалось соединиться с {url}. Проверь сеть."
    if "unsupported url" in low:
        return f"yt-dlp не знает такой сайт: {url}"

    # Незнакомый случай — отдаём как есть, но без ссылок на wiki и советов про флаги.
    cleaned = re.split(r"\s*(?:See |Also see |Use --)", raw)[0].strip()
    return f"не удалось скачать {url}: {cleaned or raw}"


def _download(url: str, target_dir: Path, config: dict[str, Any],
              ctx: JobContext) -> tuple[Path, dict[str, Any]]:
    try:
        import yt_dlp
    except ImportError as exc:  # pragma: no cover
        raise IngestError(f"yt-dlp не установлен: {exc}") from exc

    global _last_youtube_call

    target_dir.mkdir(parents=True, exist_ok=True)
    last_reported = 0.0

    def hook(status: dict[str, Any]) -> None:
        nonlocal last_reported
        if status.get("status") != "downloading":
            return
        total = status.get("total_bytes") or status.get("total_bytes_estimate") or 0
        done = status.get("downloaded_bytes") or 0
        if not total:
            return
        fraction = done / total
        if fraction - last_reported >= 0.02:
            last_reported = fraction
            ctx.progress(fraction, f"загрузка {fraction * 100:.0f}%")

    wanted_format = config.get("format") or "bestvideo[height<=?1080]+bestaudio/best"
    options: dict[str, Any] = {
        "outtmpl": str(target_dir / "%(id)s.%(ext)s"),
        "format": wanted_format,
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "logger": _YtLogger(ctx),
        "noprogress": True,  # прогресс идёт в панель через hook, а не в stdout воркера
        "progress_hooks": [hook],
        "ffmpeg_location": str(Path(settings.resolve_ffmpeg()).parent),
        "retries": 5,
        "fragment_retries": 5,
    }
    # Ограничение размера куска заставляет качать диапазонами вместо одного
    # длинного запроса. На некоторых адресах только так и отдаётся: сплошной
    # запрос ловит 403, а короткие диапазоны проходят.
    chunk_mb = float(config.get("http_chunk_size_mb") or 0)
    if chunk_mb > 0:
        options["http_chunk_size"] = int(chunk_mb * 1024 * 1024)
    clients = [c.strip() for c in (config.get("player_clients") or "").split(",") if c.strip()]
    if clients:
        options["extractor_args"] = {"youtube": {"player_client": clients}}

    proxy = (config.get("proxy") or "").strip()
    if proxy:
        # Пустая строка у yt-dlp означает «мимо прокси», в том числе мимо
        # системного: именно это и нужно по слову direct.
        options["proxy"] = "" if proxy.lower() in {"direct", "none", "off"} else proxy

    browser = (config.get("cookies_from_browser") or "").strip()
    if browser:
        options["cookiesfrombrowser"] = (browser,)

    cookies_file = (config.get("cookies_file") or "").strip()
    if cookies_file:
        cookies_path = Path(cookies_file)
        if not cookies_path.exists():
            raise IngestError(f"файл cookies не найден: {cookies_path}")
        options["cookiefile"] = str(cookies_path)

    # С YouTube качаем строго по одному и не чаще раза в MIN_GAP секунд.
    serialize = _is_youtube(url)
    if serialize:
        if _youtube_gate.locked():
            ctx.info("жду очереди: с YouTube качаем по одному ролику за раз")
        _youtube_gate.acquire()
        pause = MIN_GAP - (time.monotonic() - _last_youtube_call)
        if pause > 0:
            time.sleep(pause)
    try:
        # Обе типовые беды лечатся повтором, но по-разному. 403: ссылка подписана
        # под адрес, с которого запрошен плеер, и разваливается, если выход в сеть
        # сменился — нужны свежие ссылки тем же набором клиентов. «Нет формата»:
        # клиент вернул огрызок плеера — тут нужен другой клиент, и ровно один:
        # если дорожек не дал никто, дело не в клиенте, а в адресе выхода.
        queue: list[list[str] | None] = [clients or None]
        if clients != ["android_vr"]:
            queue.append(["android_vr"])

        info = None
        downloader = None
        # Показываем первую ошибку: повторы бьют по тому же адресу и легко доводят
        # YouTube до «подтверди, что не бот». Такое сообщение увело бы в сторону
        # от настоящей причины.
        first_error = ""
        repeated_same = False
        index = 0
        while index < len(queue):
            plan = queue[index]
            if plan:
                options["extractor_args"] = {"youtube": {"player_client": plan}}
            else:
                options.pop("extractor_args", None)
            # На последнем заходе ослабляем строку выбора: 720p на диске полезнее,
            # чем красивое требование 1080p и пустая папка.
            options["format"] = (
                f"{wanted_format}/bestvideo+bestaudio/best/b"
                if index == len(queue) - 1
                else wanted_format
            )
            last_reported = 0.0
            with yt_dlp.YoutubeDL(options) as downloader:
                try:
                    info = downloader.extract_info(url, download=True)
                    break
                except yt_dlp.utils.DownloadError as exc:
                    error = str(exc)
                    first_error = first_error or error
                    if _is_rate_limited(error):
                        raise IngestError(
                            _explain_download_error(url, error, browser)
                        ) from exc
                    if not _is_retryable(error):
                        raise IngestError(
                            _explain_download_error(url, first_error, browser)
                        ) from exc
                    # Свежие ссылки тем же клиентом помогают только против 403 и
                    # только один раз: дальше это просто лишний стук в дверь.
                    if _is_forbidden(error) and not repeated_same:
                        repeated_same = True
                        queue.insert(index + 1, plan)
                    index += 1
                    if index >= len(queue):
                        raise IngestError(
                            _explain_download_error(url, first_error, browser)
                        ) from exc
                    next_plan = queue[index]
                    ctx.info(
                        ("YouTube ответил 403" if _is_forbidden(error) else "клиент не отдал дорожек")
                        + ", пробую ещё раз"
                        + (f" через {', '.join(next_plan)}" if next_plan else "")
                    )
                    time.sleep(5)

        if info is None or downloader is None:  # pragma: no cover — цикл всегда завершается
            raise IngestError(_explain_download_error(url, first_error, browser))
        path = Path(downloader.prepare_filename(info))
    finally:
        if serialize:
            _last_youtube_call = time.monotonic()
            _youtube_gate.release()

    if not path.exists():
        # yt-dlp мог поменять расширение при слиянии дорожек
        matches = sorted(target_dir.glob(f"{info.get('id', '')}.*"))
        if not matches:
            raise IngestError("yt-dlp отчитался об успехе, но файл не найден")
        path = matches[0]

    meta = {
        "id": info.get("id"),
        "title": info.get("title"),
        "uploader": info.get("uploader"),
        "channel_url": info.get("channel_url"),
        "upload_date": info.get("upload_date"),
        "duration": info.get("duration"),
        "webpage_url": info.get("webpage_url"),
        "language": info.get("language"),
    }
    return path, meta


def _take_local(raw: str, ctx: JobContext) -> tuple[Path, dict[str, Any]]:
    """Берёт готовый файл с диска. Копии рядом с загрузками не делаем.

    Файл и так лежит на диске, и вторые несколько гигабайт на каждый проект —
    ощутимая плата ни за что. Взамен файл остаётся чужим: удаление проекта его
    не трогает (см. _remove_project_files в api/projects.py). Обратная сторона —
    если файл переименуют или удалят, пересборка уже смонтированных роликов
    сломается; ошибка при этом внятная, а исходник всегда можно вернуть на
    место.
    """
    path = Path(raw).expanduser()
    if not path.exists():
        raise IngestError(
            f"файл не найден: {path}. Если он на съёмном диске или в сетевой "
            "папке — проверь, что она подключена."
        )
    if not path.is_file():
        raise IngestError(f"это не файл, а папка: {path}")

    ctx.info(f"беру файл с диска: {path}")
    meta = {
        "title": path.stem,
        "source_path": str(path),
        "size_bytes": path.stat().st_size,
    }
    return path, meta


@handler("project.ingest")
def run_ingest(ctx: JobContext) -> None:
    project = ctx.db.get(Project, ctx.job.project_id)
    if project is None:
        raise IngestError(f"проект {ctx.job.project_id} не найден")

    config = config_for_project(ctx.db, project)
    ingest_config = config["ingest"]

    project.status = ProjectStatus.DOWNLOADING
    project.stage_message = "скачиваю исходник"
    project.error = ""
    ctx.db.commit()

    # Плоская нарезка не смотрит в транскрипт, поэтому и звук ей не нужен:
    # лишний проход по часовому файлу здесь просто выбрасывается.
    flat_cut = bool(config.get("chunks", {}).get("enabled"))

    if project.source_kind == "file":
        video_path, meta = _take_local(project.source_url, ctx)
    else:
        ctx.info(f"загружаю {project.source_url}")
        video_path, meta = _download(
            project.source_url, settings.sources_dir, ingest_config, ctx.stage(0.0, 0.6)
        )
        ctx.info(f"скачано: {video_path.name}")

    info = probe_media(video_path)
    if not info.has_video:
        raise IngestError("в скачанном файле нет видеодорожки")
    if not info.has_audio:
        raise IngestError(
            "в исходнике нет звуковой дорожки — монтаж собирает ролик со звуком "
            "и без неё не соберётся"
        )

    project.video_path = str(video_path)
    project.duration = info.duration
    project.width = info.width
    project.height = info.height
    project.fps = info.fps
    project.source_meta = meta
    if not project.title:
        project.title = meta.get("title") or safe_filename(video_path.stem)
    ctx.db.commit()
    ctx.progress(0.65, "исходник проверен")

    if flat_cut:
        ctx.info("плоская нарезка: транскрипции не будет, звук не извлекаю")
    elif ingest_config.get("extract_audio", True):
        audio_path = settings.audio_dir / f"{video_path.stem}.wav"
        ctx.info("извлекаю звук для транскрипции")
        extract_audio(video_path, audio_path)
        project.audio_path = str(audio_path)
        ctx.db.commit()
    ctx.progress(0.8, "звук извлечён")

    thumb = settings.thumbs_dir / f"project_{project.id}.jpg"
    try:
        extract_thumbnail(video_path, thumb, at_second=min(3.0, info.duration / 2))
    except Exception as exc:  # noqa: BLE001 — превью не критично
        ctx.info(f"не удалось снять превью: {exc}")

    if ingest_config.get("detect_subtitles", True):
        ctx.info("проверяю, есть ли вшитые субтитры")
        project.stage_message = "ищу текст в кадре"
        ctx.db.commit()
        ctx.progress(0.85, "ищу текст в кадре")
        detected, details = has_burned_subtitles(video_path, info.duration)
        project.has_burned_subtitles = detected
        project.source_meta = {**project.source_meta, "text_detection": details}
        if detected is None:
            ctx.info("определить не удалось — выставь флаг вручную в панели")
        else:
            ctx.info(f"вшитые субтитры: {'есть' if detected else 'нет'}")
        ctx.db.commit()

    project.stage_message = "готов к нарезке" if flat_cut else "готов к транскрипции"
    ctx.db.commit()
    ctx.progress(1.0, "загрузка завершена")

    enqueue(
        ctx.db,
        "project.chunk" if flat_cut else "project.transcribe",
        project_id=project.id,
        priority=ctx.job.priority,
        payload={"auto": ctx.payload.get("auto", True)},
    )
