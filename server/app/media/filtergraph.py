"""Сборка команды монтажа.

Порядок фильтров подобран так, чтобы каждый шаг работал с предсказуемым кадром:

    скорость → кадрирование в 9:16 → зеркало → зум → цвет
    → маска → баннер с хромакеем → заголовок → субтитры

Зеркалить нужно до наложения текста, иначе наш собственный заголовок окажется
зеркальным. Зум по умолчанию идёт после кадрирования: тогда в режиме полос он
приближает готовый кадр — видео крупнее, полосы тоньше. Настройка
zoom.apply_to=source переносит его на исходник, но в режиме полос это лишь
сужает сцену, не меняя размеров кадра.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..utils.text import escape_filter_path, wrap_title
from .fonts import resolve_font

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff"}


@dataclass
class RenderInputs:
    """Всё, что нужно знать конвейеру монтажа про конкретный фрагмент."""

    source: Path
    output: Path
    duration: float
    title_text: str = ""
    source_has_title: bool = False
    source_has_subtitles: bool = False
    subtitles_path: Path | None = None
    mask_path: Path | None = None
    banner_path: Path | None = None
    background_path: Path | None = None
    background_has_audio: bool = False
    lut_path: Path | None = None
    font_path: Path | None = None
    seed: int | None = None


@dataclass
class RenderPlan:
    args: list[str]
    duration: float
    applied: dict[str, Any] = field(default_factory=dict)


def _is_image(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_SUFFIXES


def _pick_speed(config: dict[str, Any], seed: int | None) -> float:
    speed = config.get("speed", {})
    if not speed.get("enabled"):
        return 1.0
    if speed.get("randomize"):
        rng = random.Random(seed)
        low = float(speed.get("min", 1.01))
        high = float(speed.get("max", 1.05))
        if high < low:
            low, high = high, low
        return round(rng.uniform(low, high), 4)
    return float(speed.get("factor", 1.0))


def _atempo_chain(speed: float) -> list[str]:
    """atempo принимает 0.5–2.0 за проход; наши 1.01–1.05 всегда влезают."""
    if abs(speed - 1.0) < 1e-4:
        return []
    remaining = speed
    steps: list[str] = []
    while remaining > 2.0:
        steps.append("atempo=2.0")
        remaining /= 2.0
    while remaining < 0.5:
        steps.append("atempo=0.5")
        remaining /= 0.5
    steps.append(f"atempo={remaining:.4f}")
    return steps


def _framing_filters(config: dict[str, Any], width: int, height: int) -> list[str]:
    """Приводит любой исходник к вертикали width×height."""
    framing = config.get("framing", {})
    mode = framing.get("mode", "crop")
    focus_x = min(1.0, max(0.0, float(framing.get("focus_x", 0.5))))
    focus_y = min(1.0, max(0.0, float(framing.get("focus_y", 0.5))))

    if mode == "fit":
        # Горизонтальный кадр целиком, полосы сверху и снизу. Вертикальное
        # положение регулируется: по центру остаётся мало места под заголовок,
        # поэтому по умолчанию видео смещено вверх.
        fit_y = min(1.0, max(0.0, float(framing.get("fit_y", 0.42))))
        colour = str(framing.get("pad_color", "black")).strip() or "black"
        return [
            f"scale={width}:{height}:force_original_aspect_ratio=decrease",
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)*{fit_y:.3f}:{colour}",
        ]

    # blur_pad ветвит граф, поэтому собирается отдельно в _blur_pad_graph
    # crop (по умолчанию): масштабируем с перекрытием и вырезаем нужную область
    return [
        f"scale={width}:{height}:force_original_aspect_ratio=increase",
        f"crop={width}:{height}:(iw-{width})*{focus_x:.3f}:(ih-{height})*{focus_y:.3f}",
    ]


def _blur_pad_graph(label_in: str, label_out: str, config: dict[str, Any],
                    width: int, height: int) -> str:
    """Горизонтальный кадр по центру, а поля — он же, размытый до неузнаваемости.

    Фон размывается не в полном разрешении, а на уменьшенной копии, которая
    потом растягивается обратно. gblur считает по числу пикселей, и на
    1080×1920 это самый дорогой фильтр всей сборки: уменьшение вшестеро
    убирает 35/36 работы. Разницу ловит только измеритель (SSIM 0.995) —
    размытому фону обратное растягивание лишь добавляет мягкости.
    """
    blur = int(config.get("framing", {}).get("blur_strength", 28))

    # Уменьшать сильнее, чем позволяет сама сигма, смысла нет: у слабого
    # размытия после деления не останется радиуса, и вместо мягкого фона
    # получится каша из растянутых пикселей.
    factor = max(1, min(6, blur // 2))
    if factor > 1:
        small_w = max(2, int(width / factor) // 2 * 2)
        small_h = max(2, int(height / factor) // 2 * 2)
        background = (
            f"scale={small_w}:{small_h}:force_original_aspect_ratio=increase,"
            f"crop={small_w}:{small_h},gblur=sigma={blur / factor:.2f},"
            f"scale={width}:{height}"
        )
    else:
        background = (
            f"scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height}"
        )
        if blur > 0:
            background += f",gblur=sigma={blur}"

    return (
        f"[{label_in}]split=2[bgsrc][fgsrc];"
        f"[bgsrc]{background}[bg];"
        f"[fgsrc]scale={width}:{height}:force_original_aspect_ratio=decrease[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2[{label_out}]"
    )


def _zoom_filters(config: dict[str, Any], width: int, height: int,
                  duration: float, fps: int) -> list[str]:
    zoom = config.get("zoom", {})
    if not zoom.get("enabled"):
        return []

    factor = max(1.0, float(zoom.get("factor", 1.0)))
    if zoom.get("mode") == "kenburns":
        end = max(factor, float(zoom.get("end_factor", factor * 1.1)))
        frames = max(1, int(duration * fps))
        # zoompan работает по номеру кадра `on`; линейно ведём масштаб к end
        expression = f"{factor}+({end}-{factor})*on/{frames}"
        return [
            f"zoompan=z='{expression}':d=1:x='iw/2-(iw/zoom/2)':"
            f"y='ih/2-(ih/zoom/2)':s={width}x{height}:fps={fps}"
        ]

    if factor <= 1.0001:
        return []
    # Статический зум: режем центр и растягиваем обратно — дешевле, чем scale+crop
    return [
        f"crop=iw/{factor:.4f}:ih/{factor:.4f}",
        f"scale={width}:{height}",
    ]


def _zoom_crop_only(config: dict[str, Any]) -> list[str]:
    """Зум для режима с фоном: только обрезка, без возврата к размеру кадра.

    В обычном режиме кадр после зума растягивается обратно до width×height.
    Здесь исходник занимает лишь часть кадра, и его размер задаётся позже, при
    масштабировании под ширину вставки, — иначе картинку растянуло бы на весь
    экран. Плавный зум (kenburns) требует явного размера и тут не применяется.
    """
    zoom = config.get("zoom", {})
    if not zoom.get("enabled"):
        return []
    factor = max(1.0, float(zoom.get("factor", 1.0)))
    if factor <= 1.0001:
        return []
    return [f"crop=iw/{factor:.4f}:ih/{factor:.4f}"]


def _rounded_corners(radius: int) -> str:
    """Скругляет углы вставки, вырезая их в альфа-канале.

    Проверка на попадание в окружность делается только в самих углах — в
    остальной части кадра альфа остаётся непрозрачной, и geq не считает лишнего.
    """
    r = max(1, int(radius))
    inside = (
        f"lte(pow(abs(X-(W/2))-(W/2-{r}),2)+pow(abs(Y-(H/2))-(H/2-{r}),2),pow({r},2))"
    )
    corner = f"gt(abs(X-(W/2)),W/2-{r})*gt(abs(Y-(H/2)),H/2-{r})"
    return (
        "format=rgba,"
        f"geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='if({corner},if({inside},255,0),255)'"
    )


def _background_graph(label_out: str, index: int, config: dict[str, Any],
                      width: int, height: int, fps: int) -> str:
    """Фон на весь кадр: заполнить, при желании размыть и притушить.

    fps идёт первым шагом: фоны нередко сняты на 60 кадрах, а в ролике их 30,
    и размывать кадры, которые тут же будут отброшены, — двойная работа. На
    картинку перестановка не влияет: и gblur, и eq считаются покадрово.
    """
    steps = [
        f"fps={fps}",
        f"scale={width}:{height}:force_original_aspect_ratio=increase",
        f"crop={width}:{height}",
    ]
    blur = float(config.get("blur", 0) or 0)
    if blur > 0:
        steps.append(f"gblur=sigma={blur:.2f}")
    dim = min(1.0, max(0.0, float(config.get("dim", 0) or 0)))
    if dim > 0:
        # Приглушаем фон, чтобы он не спорил со вставкой за внимание.
        steps.append(f"eq=brightness=-{dim:.3f}")
    steps.append("format=yuv420p")
    return f"[{index}:v]{','.join(steps)}[{label_out}]"


def _color_filters(config: dict[str, Any], lut_path: Path | None) -> list[str]:
    color = config.get("color", {})
    if not color.get("enabled"):
        return []

    filters: list[str] = []
    if lut_path and lut_path.exists():
        filters.append(f"lut3d=file='{escape_filter_path(lut_path)}'")

    brightness = float(color.get("brightness", 0.0))
    contrast = float(color.get("contrast", 1.0))
    saturation = float(color.get("saturation", 1.0))
    gamma = float(color.get("gamma", 1.0))
    if any(
        abs(value - default) > 1e-3
        for value, default in (
            (brightness, 0.0), (contrast, 1.0), (saturation, 1.0), (gamma, 1.0)
        )
    ):
        filters.append(
            f"eq=brightness={brightness:.3f}:contrast={contrast:.3f}"
            f":saturation={saturation:.3f}:gamma={gamma:.3f}"
        )

    sharpen = float(color.get("sharpen", 0.0))
    if sharpen > 0.01:
        filters.append(f"unsharp=5:5:{min(1.5, sharpen):.2f}:5:5:0.0")

    if color.get("vignette"):
        filters.append("vignette=PI/5")

    return filters


def _title_filter(config: dict[str, Any], inputs: RenderInputs,
                  width: int, height: int, duration: float) -> str:
    title = config.get("title", {})
    if not title.get("enabled"):
        return ""

    text = (title.get("text") or inputs.title_text or "").strip()
    if not text:
        return ""
    if title.get("only_if_absent", True) and inputs.source_has_title:
        return ""

    wrapped = wrap_title(text, int(title.get("max_chars_per_line", 20)))

    # Текст уходит в файл, а не в параметр text=. Строку фильтра ffmpeg разбирает
    # дважды, и переносы строк с апострофами при этом теряются: «dem\nBanner»
    # превращалось в «demnBanner». С textfile= экранировать нужно только путь.
    text_path = inputs.output.parent / f"{inputs.output.stem}.title.txt"
    text_path.parent.mkdir(parents=True, exist_ok=True)
    text_path.write_text(wrapped, encoding="utf-8")

    font = resolve_font(inputs.font_path or title.get("font_path"))
    font_size = int(title.get("font_size", 64))
    margin = int(title.get("margin", 200))

    position = title.get("position", "top")
    if position == "bottom":
        y = f"h-th-{margin}"
    elif position == "center":
        y = "(h-th)/2"
    else:
        y = str(margin)

    parts = [
        f"fontfile='{escape_filter_path(font)}'",
        f"textfile='{escape_filter_path(text_path)}'",
        f"fontsize={font_size}",
        f"fontcolor={title.get('color', 'white')}",
        "x=(w-tw)/2",
        f"y={y}",
        "line_spacing=12",
        "text_align=C",
    ]

    border = int(title.get("border_width", 0))
    if border > 0:
        parts.append(f"borderw={border}")
        parts.append(f"bordercolor={title.get('border_color', 'black')}")

    if title.get("box"):
        parts.append("box=1")
        parts.append(f"boxcolor={title.get('box_color', 'black@0.45')}")
        parts.append(f"boxborderw={int(title.get('box_padding', 18))}")

    start = float(title.get("start", 0.0))
    length = float(title.get("duration", 0.0))
    if start > 0 or length > 0:
        end = start + length if length > 0 else duration
        parts.append(f"enable='between(t,{start:.2f},{end:.2f})'")

    return "drawtext=" + ":".join(parts)


def _should_mirror(config: dict[str, Any], inputs: RenderInputs) -> bool:
    mirror = config.get("mirror", {})
    if not mirror.get("enabled"):
        return False
    if not mirror.get("only_if_no_subtitles", True):
        return True
    # Зеркалим только если в кадре нет текста: ни вшитого в исходник,
    # ни того, что мы сами собираемся выжечь.
    burning_own = bool(config.get("subtitles", {}).get("enabled")) and inputs.subtitles_path
    return not inputs.source_has_subtitles and not burning_own


def build_render_command(config: dict[str, Any], inputs: RenderInputs) -> RenderPlan:
    """Собирает полный вызов ffmpeg для одного фрагмента."""
    output_cfg = config.get("output", {})
    width = int(output_cfg.get("width", 1080))
    height = int(output_cfg.get("height", 1920))
    fps = int(output_cfg.get("fps", 30))

    speed = _pick_speed(config, inputs.seed)
    out_duration = inputs.duration / speed if speed else inputs.duration

    args: list[str] = ["-i", str(inputs.source)]
    overlay_inputs: list[tuple[str, Path]] = []  # (роль, путь)

    mask_cfg = config.get("mask", {})
    if mask_cfg.get("enabled") and inputs.mask_path and inputs.mask_path.exists():
        overlay_inputs.append(("mask", inputs.mask_path))

    banner_cfg = config.get("banner", {})
    if banner_cfg.get("enabled") and inputs.banner_path and inputs.banner_path.exists():
        overlay_inputs.append(("banner", inputs.banner_path))

    background_cfg = config.get("background", {})
    use_background = bool(
        background_cfg.get("enabled")
        and inputs.background_path
        and inputs.background_path.exists()
    )
    if use_background:
        overlay_inputs.append(("background", inputs.background_path))

    for role, path in overlay_inputs:
        if _is_image(path):
            args += ["-loop", "1", "-i", str(path)]
        else:
            # Фон почти всегда короче фрагмента, поэтому крутим его по кругу.
            loop = banner_cfg.get("loop", True) if role == "banner" else True
            if loop:
                args += ["-stream_loop", "-1"]
            args += ["-i", str(path)]

    index_of = {role: position + 1 for position, (role, _) in enumerate(overlay_inputs)}

    # --- основная видеоцепочка ---
    graph: list[str] = []
    chain: list[str] = []
    applied: dict[str, Any] = {"speed": speed}

    if abs(speed - 1.0) > 1e-4:
        chain.append(f"setpts=PTS/{speed:.4f}")

    framing_mode = config.get("framing", {}).get("mode", "crop")
    # Значение по умолчанию обязано быть здесь, а не в одной из веток: зум ниже
    # смотрит на этот флаг при любом кадрировании, а выставляла его только ветка
    # полос. Режим blur_pad из-за этого падал с UnboundLocalError на каждом
    # ролике — то есть не работал вообще.
    zoom_to_source = False
    if use_background:
        # Исходник не растягивается на весь кадр, а становится вставкой поверх
        # фона, поэтому обычное кадрирование к нему не применяется.
        framing_mode = "background"
        current = None
    elif framing_mode == "blur_pad":
        # Ветвящийся граф нельзя выразить линейной цепочкой — собираем отдельно.
        prefix = ",".join(chain) if chain else "null"
        graph.append(f"[0:v]{prefix}[pre]")
        graph.append(_blur_pad_graph("pre", "framed", config, width, height))
        chain = []
        current = "framed"
    else:
        # В режиме полос порядок решает, что именно приближается. Обрезка
        # исходника до вписывания оставляет полосы прежней высоты и лишь
        # сужает сцену. Обрезка готового кадра (ниже, обычным зумом) делает
        # видео крупнее, а полосы тоньше — обычно хотят именно это.
        zoom_to_source = (
            framing_mode == "fit"
            and config.get("zoom", {}).get("apply_to", "frame") == "source"
        )
        if zoom_to_source:
            chain += _zoom_crop_only(config)
            applied["zoom_applied_to"] = "source"
        chain += _framing_filters(config, width, height)
        current = None
    applied["framing"] = framing_mode

    mirrored = _should_mirror(config, inputs)
    if mirrored:
        chain.append("hflip")
    applied["mirrored"] = mirrored

    if use_background:
        clip_scale = min(1.0, max(0.1, float(background_cfg.get("clip_scale", 0.86))))
        clip_width = int(width * clip_scale) // 2 * 2  # чётная ширина — требование кодека
        chain += _zoom_crop_only(config)
        chain += _color_filters(config, inputs.lut_path)
        chain.append(f"fps={fps}")
        chain.append(f"scale={clip_width}:-2")
        radius = int(background_cfg.get("corner_radius", 0) or 0)
        if radius > 0:
            chain.append(_rounded_corners(radius))
        else:
            chain.append("format=rgba")
        graph.append(f"[0:v]{','.join(chain)}[clipv]")

        bg_index = index_of["background"]
        graph.append(_background_graph("bgv", bg_index, background_cfg, width, height, fps))

        clip_y = min(1.0, max(0.0, float(background_cfg.get("clip_y", 0.1))))
        graph.append(
            f"[bgv][clipv]overlay=(W-w)/2:{clip_y:.4f}*H:shortest=1,"
            f"format=yuv420p[base]"
        )
        applied["background"] = {
            "file": inputs.background_path.name if inputs.background_path else "",
            "clip_scale": clip_scale,
            "corner_radius": radius,
        }
    else:
        # Второй раз к исходнику не применяем: если зум уже отработал выше,
        # здесь он только испортил бы кадр.
        if not zoom_to_source:
            chain += _zoom_filters(config, width, height, out_duration, fps)
            if config.get("zoom", {}).get("enabled"):
                applied["zoom_applied_to"] = "frame"
        chain += _color_filters(config, inputs.lut_path)
        chain.append(f"fps={fps}")
        chain.append("format=yuv420p")

        if current is None:
            graph.append(f"[0:v]{','.join(chain)}[base]")
        else:
            graph.append(f"[{current}]{','.join(chain)}[base]")
    last = "base"

    # --- маска ---
    if "mask" in index_of:
        idx = index_of["mask"]
        opacity = min(1.0, max(0.0, float(mask_cfg.get("opacity", 1.0))))
        fit = mask_cfg.get("fit", "cover")
        if fit == "contain":
            scale = f"scale={width}:{height}:force_original_aspect_ratio=decrease"
        elif fit == "stretch":
            scale = f"scale={width}:{height}"
        else:
            scale = (
                f"scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height}"
            )
        graph.append(
            f"[{idx}:v]fps={fps},{scale},format=rgba,"
            f"colorchannelmixer=aa={opacity:.3f}[maskv]"
        )
        blend = mask_cfg.get("blend", "normal")
        if blend == "normal":
            graph.append(f"[{last}][maskv]overlay=0:0:shortest=1[masked]")
        else:
            graph.append(f"[{last}][maskv]blend=all_mode={blend}[masked]")
        last = "masked"
        applied["mask"] = {"blend": blend, "opacity": opacity}

    # --- баннер с вырезанным хромакеем ---
    if "banner" in index_of:
        idx = index_of["banner"]
        # Порядок здесь решает больше, чем весь остальной граф. Баннер обычно
        # снят крупнее кадра и на 60 кадрах, а на выход идёт уменьшенным и на
        # 30: ключ по исходному кадру считал вчетверо больше пикселей и вдвое
        # больше кадров, чем доходит до зрителя, и съедал больше половины
        # времени монтажа. Поэтому сначала fps, потом обрезка и масштаб, и уже
        # по готовому кадру — ключ.
        steps: list[str] = [f"fps={fps}"]

        # Обрезка идёт следом: дальше и масштаб, и ключ работают уже по
        # содержимому, а не по полотну с пустыми полями.
        crop_cfg = banner_cfg.get("crop", {})
        if crop_cfg.get("enabled"):
            cw = min(1.0, max(0.01, float(crop_cfg.get("width", 1.0))))
            ch = min(1.0, max(0.01, float(crop_cfg.get("height", 1.0))))
            cx = min(1.0 - cw, max(0.0, float(crop_cfg.get("x", 0.0))))
            cy = min(1.0 - ch, max(0.0, float(crop_cfg.get("y", 0.0))))
            steps.append(f"crop=iw*{cw:.4f}:ih*{ch:.4f}:iw*{cx:.4f}:ih*{cy:.4f}")
            applied["banner_crop"] = [cx, cy, cw, ch]

        scale_factor = float(banner_cfg.get("scale", 1.0))
        steps.append(f"scale={int(width * scale_factor)}:-2")

        chroma = banner_cfg.get("chromakey", {})
        if chroma.get("enabled", True):
            color = chroma.get("color", "0x00FF00")
            similarity = float(chroma.get("similarity", 0.18))
            blend_amount = float(chroma.get("blend", 0.08))
            # chromakey сравнивает только цветность и не смотрит на яркость.
            # Для зелёнки это правильно, а на однотонном насыщенном фоне он
            # заодно выедает белый текст — проверено на баннере Playerok:
            # белые буквы становились прозрачными. colorkey сравнивает полный
            # RGB и с плоским фоном справляется без потерь.
            method = "colorkey" if chroma.get("mode") == "colorkey" else "chromakey"
            steps.append(f"{method}={color}:{similarity:.3f}:{blend_amount:.3f}")
            # despill в ffmpeg умеет только зелёный и синий: на фиолетовом или
            # красном фоне он исказил бы цвета логотипа.
            if chroma.get("despill", True) and method == "chromakey":
                steps.append("despill=type=green")
        steps.append("format=rgba")
        graph.append(f"[{idx}:v]{','.join(steps)}[bannerv]")

        position = banner_cfg.get("position", "top")
        if position == "bottom":
            x, y = "(W-w)/2", f"H-h-{int(banner_cfg.get('y', 0))}"
        elif position == "center":
            x, y = "(W-w)/2", "(H-h)/2"
        elif position == "custom":
            x, y = str(int(banner_cfg.get("x", 0))), str(int(banner_cfg.get("y", 0)))
        else:
            x, y = "(W-w)/2", str(int(banner_cfg.get("y", 0)))

        overlay_params = [x, y, "shortest=1"]
        start = float(banner_cfg.get("start", 0.0))
        length = float(banner_cfg.get("duration", 0.0))
        if start > 0 or length > 0:
            end = start + length if length > 0 else out_duration
            overlay_params.append(f"enable='between(t,{start:.2f},{end:.2f})'")
        graph.append(f"[{last}][bannerv]overlay={':'.join(overlay_params)}[bannered]")
        last = "bannered"
        applied["banner"] = {"position": position, "chromakey": chroma.get("enabled", True)}

    # --- заголовок ---
    title_filter = _title_filter(config, inputs, width, height, out_duration)
    if title_filter:
        graph.append(f"[{last}]{title_filter}[titled]")
        last = "titled"
        applied["title"] = True

    # --- субтитры ---
    subtitles_cfg = config.get("subtitles", {})
    if subtitles_cfg.get("enabled") and inputs.subtitles_path and inputs.subtitles_path.exists():
        graph.append(
            f"[{last}]subtitles='{escape_filter_path(inputs.subtitles_path)}'[subbed]"
        )
        last = "subbed"
        applied["subtitles"] = True

    graph.append(f"[{last}]null[vout]")

    # --- аудио ---
    audio_steps = _atempo_chain(speed)
    audio_steps.append("aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo")

    mix_background = (
        use_background
        and background_cfg.get("audio") == "mix"
        and inputs.background_has_audio
    )
    if mix_background:
        gain = min(1.0, max(0.0, float(background_cfg.get("audio_gain", 0.12))))
        graph.append(f"[0:a]{','.join(audio_steps)}[speech]")
        # duration=first обрезает микс по речи: фон зациклен и сам не кончится.
        graph.append(
            f"[{index_of['background']}:a]volume={gain:.3f},"
            "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[bga]"
        )
        graph.append("[speech][bga]amix=inputs=2:duration=first:dropout_transition=0[aout]")
        applied["background_audio"] = gain
    else:
        graph.append(f"[0:a]{','.join(audio_steps)}[aout]")

    args += [
        "-filter_complex", ";".join(graph),
        "-map", "[vout]",
        "-map", "[aout]",
        "-t", f"{out_duration:.3f}",
    ]

    encoder = output_cfg.get("hardware_encoder") or ""
    if encoder:
        args += ["-c:v", encoder, "-cq", str(int(output_cfg.get("crf", 19))), "-b:v", "0"]
    else:
        args += [
            "-c:v", "libx264",
            "-preset", str(output_cfg.get("x264_preset", "medium")),
            "-crf", str(int(output_cfg.get("crf", 19))),
        ]

    args += [
        "-pix_fmt", "yuv420p",
        "-profile:v", "high",
        "-c:a", "aac",
        "-b:a", str(output_cfg.get("audio_bitrate", "192k")),
        "-ar", "48000",
        "-movflags", "+faststart",
        str(inputs.output),
    ]

    return RenderPlan(args=args, duration=out_duration, applied=applied)
