import { useState } from "react";
import { api } from "../api";
import { useAsync } from "../hooks/useLiveState";
import { SchedulePicker } from "./SchedulePicker";
import type { Asset, Config } from "../types";

/**
 * Форма строится по структуре конфига, а не по захардкоженному списку полей:
 * добавили ключ на бэкенде — он сразу появился в панели.
 */

const LABELS: Record<string, string> = {
  ingest: "Загрузка исходника",
  transcribe: "Транскрипция",
  analyze: "Поиск фрагментов",
  caption: "Тексты для площадок",
  background: "Фон под видео",
  cut: "Нарезка",
  edit: "Монтаж",
  publish: "Публикация",

  output: "Параметры вывода",
  framing: "Кадрирование",
  speed: "Скорость",
  zoom: "Приближение",
  mirror: "Зеркалирование",
  mask: "Маска",
  banner: "Баннер",
  chromakey: "Хромакей",
  title: "Заголовок",
  subtitles: "Субтитры",
  youtube: "YouTube",
  instagram: "Instagram",
  tiktok: "TikTok",
  schedule: "Расписание",

  enabled: "Включено",
  disable_comment: "Запретить комментарии",
  disable_duet: "Запретить дуэты",
  disable_stitch: "Запретить склейки",
  max_height: "Максимальная высота",
  format: "Формат yt-dlp",
  cookies_from_browser: "Cookies из браузера",
  cookies_file: "Файл cookies",
  player_clients: "Клиенты YouTube",
  proxy: "Прокси",
  http_chunk_size_mb: "Качать кусками по, МБ",
  extract_audio: "Извлекать звук",
  detect_subtitles: "Определять вшитые субтитры",
  provider: "Провайдер",
  model: "Модель",
  language: "Язык",
  word_timestamps: "Таймкоды по словам",
  vad_filter: "Отсекать тишину (VAD)",
  beam_size: "Ширина поиска",
  min_duration: "Минимум, с",
  max_duration: "Максимум, с",
  target_count: "Сколько искать",
  min_score: "Порог оценки",
  merge_adjacent: "Склеивать соседние",
  max_merge_gap: "Макс. пауза при склейке, с",
  output_language: "Язык заголовков",
  chunk_minutes: "Размер куска, мин",
  extra_instructions: "Дополнительные указания модели",
  padding_before: "Отступ до, с",
  padding_after: "Отступ после, с",
  snap_to_words: "Подгонять к границам слов",
  crossfade: "Кроссфейд на стыках, с",
  width: "Ширина",
  height: "Высота",
  fps: "Кадров в секунду",
  crf: "CRF (меньше — качественнее)",
  x264_preset: "Пресет x264",
  audio_bitrate: "Битрейт звука",
  hardware_encoder: "Аппаратный кодек",
  mode: "Режим",
  focus_x: "Фокус по X (0–1)",
  focus_y: "Фокус по Y (0–1)",
  blur_strength: "Сила размытия фона",
  factor: "Коэффициент",
  apply_to: "К чему применять",
  randomize: "Случайно из диапазона",
  min: "Минимум",
  max: "Максимум",
  pitch_correction: "Сохранять высоту голоса",
  end_factor: "Конечный коэффициент",
  only_if_no_subtitles: "Только если нет субтитров",
  lut_asset_id: "LUT (ассет)",
  brightness: "Яркость",
  contrast: "Контраст",
  saturation: "Насыщенность",
  gamma: "Гамма",
  sharpen: "Резкость",
  vignette: "Виньетка",
  asset_id: "Файл (ассет)",
  crop: "Обрезка исходного файла",
  hashtag_count: "Сколько хэштегов",
  title_max_chars: "Длина заголовка, символов",
  before_publish: "Переписывать перед публикацией",
  daily_limit: "Лимит в сутки на аккаунт",
  window_start: "Не публиковать раньше",
  window_end: "Не публиковать позже",
  clip_scale: "Ширина вставки",
  clip_y: "Отступ сверху",
  corner_radius: "Скругление углов",
  dim: "Затемнение фона",
  blur: "Размытие фона",
  audio_gain: "Громкость фона",
  fit_y: "Положение по вертикали",
  pad_color: "Цвет полос",
  opacity: "Непрозрачность",
  fit: "Вписывание",
  blend: "Режим наложения",
  color: "Цвет",
  similarity: "Допуск цвета",
  despill: "Убирать зелёный ореол",
  position: "Позиция",
  x: "Смещение X",
  y: "Смещение Y",
  scale: "Масштаб",
  start: "Начало, с",
  duration: "Длительность, с (0 — до конца)",
  loop: "Зацикливать",
  only_if_absent: "Только если заголовка нет",
  text: "Текст (пусто — из фрагмента)",
  font_asset_id: "Шрифт (ассет)",
  font_size: "Размер шрифта",
  border_width: "Толщина обводки",
  border_color: "Цвет обводки",
  box: "Подложка",
  box_color: "Цвет подложки",
  box_padding: "Отступ подложки",
  margin: "Отступ от края",
  max_chars_per_line: "Символов в строке",
  burn: "Выжигать в кадр",
  outline_color: "Цвет обводки",
  outline: "Обводка",
  uppercase: "Верхний регистр",
  highlight_active_word: "Подсвечивать слово",
  auto: "Публиковать автоматически",
  privacy: "Доступ",
  category_id: "Категория YouTube",
  made_for_kids: "Контент для детей",
  title_suffix: "Суффикс заголовка",
  description_template: "Шаблон описания",
  default_tags: "Теги по умолчанию",
  share_to_feed: "Дублировать в ленту",
  caption_template: "Шаблон подписи",
  spacing_minutes: "Интервал на аккаунт, мин",
  start_offset_minutes: "Отложить старт, мин",
};

const HINTS: Record<string, string> = {
  "chunks.enabled":
    "Ролик режется на куски равной длины без распознавания речи и без модели. Субтитры и подрезка по паузам в этом режиме недоступны — их брать неоткуда.",
  "chunks.duration":
    "Ноль — не резать вовсе: весь ролик уйдёт одним куском, только перекадрированным. Пропуски в начале и конце при этом учитываются.",
  "edit.speed.randomize":
    "На каждый фрагмент берётся своё значение из диапазона — ролики отличаются друг от друга.",
  "edit.mirror.only_if_no_subtitles":
    "Если в кадре есть текст, зеркалирование сделает его нечитаемым. Флаг определяется автоматически при загрузке и правится в карточке проекта.",
  "analyze.chunk_minutes":
    "Длинный транскрипт режется на куски с нахлёстом. Больше кусок — дороже запрос, но меньше шансов разорвать момент.",
  "analyze.extra_instructions":
    "Тема канала, тон, запреты. Уходит в промпт как есть.",
  "edit.banner.chromakey.similarity":
    "0.1 — вырезается только точный цвет, 0.4 — захватывает полутона вместе с краями объекта.",
  "publish.youtube.privacy":
    "private, unlisted или public. Начинай с private, пока не проверишь результат.",
  "edit.output.hardware_encoder":
    "Пусто — libx264 на процессоре. h264_nvenc задействует NVIDIA и ускоряет рендер в разы.",
  "transcribe.model":
    "Модель распознавания речи, не языковая. Пусто — берётся из .env " +
    "(сейчас large-v3). Крупнее — точнее и медленнее. " +
    "Модель для поиска фрагментов задаётся отдельно, в SHORTS_LLM_MODEL.",
  "analyze.provider":
    "Чем искать фрагменты. Пусто — как в SHORTS_LLM_PROVIDER из .env. " +
    "Можно искать сильной облачной моделью, а тексты писать локальной.",
  "analyze.model":
    "Имя модели у выбранного провайдера. Пусто — модель по умолчанию.",
  "caption.provider":
    "Чем писать тексты. Пусто — как в .env. Не обязан совпадать с тем, " +
    "что ищет фрагменты.",
  "caption.enabled":
    "Отдельный запрос к нейросети: по расшифровке фрагмента пишет заголовок, " +
    "описание и хэштеги с расчётом на охват. Поиск фрагментов тоже даёт тексты, " +
    "но там задача другая — отобрать материал.",
  "caption.before_publish":
    "Переписывать всегда. По умолчанию текст создаётся только если заголовка нет, " +
    "чтобы не затирать правки, сделанные руками после ревью.",
  "caption.extra_instructions":
    "Своими словами: тема канала, тон, что упоминать нельзя. Уходит в промпт как есть.",
  "publish.schedule.enabled":
    "Разносит публикации во времени. Интервал считается по аккаунту: один ролик " +
    "может уйти на все площадки разом, а два разных ролика на один аккаунт — нет.",
  "publish.schedule.spacing_minutes":
    "Минимум между двумя публикациями на одном аккаунте.",
  "publish.schedule.daily_limit":
    "Сколько роликов в сутки уходит на один аккаунт. 0 — без ограничения. " +
    "Полезно против лимитов площадок: у Instagram это 25 в сутки.",
  "publish.schedule.window_start":
    "Время в формате 10:00 по часам этого компьютера. Пусто — публиковать в любой час.",
  "publish.schedule.window_end":
    "Позже этого времени публикация переносится на завтра. Окно через полночь не поддерживается.",
  "edit.zoom.apply_to":
    "Только для режима полос. frame — приближается готовый кадр: видео крупнее, " +
    "полосы тоньше. source — обрезается исходник: в кадр попадает меньше сцены, " +
    "а полосы остаются той же высоты.",
  "edit.banner.crop.enabled":
    "Отрезать пустые поля исходного файла. Доли его собственного размера: " +
    "x и y — где начинается нужная область, width и height — её размеры. " +
    "Без обрезки масштаб приходится задирать, а видимая часть баннера всё равно мелкая.",
  "edit.banner.chromakey.mode":
    "chromakey — для зелёного фона: не смотрит на яркость и на цветной заливке " +
    "выедает белый текст. colorkey сравнивает полный цвет и подходит баннерам " +
    "на однотонном фоне.",
  "edit.background.enabled":
    "Формат, где исходник уменьшается и ложится поверх постороннего видеоряда. " +
    "Обычное кадрирование при этом не применяется.",
  "edit.background.clip_scale":
    "Доля ширины кадра, которую занимает вставка. 0.86 — как на типовых шортсах.",
  "edit.background.clip_y":
    "Где верхний край вставки, доля высоты кадра. 0.1 — чуть ниже верха.",
  "edit.background.corner_radius":
    "Радиус скругления в пикселях, 0 — прямые углы.",
  "edit.background.audio":
    "clip — слышна только речь. mix — фон подмешивается тихой подложкой.",
  "edit.framing.fit_y":
    "Только для режима fit: 0.5 — по центру, меньше — выше, чтобы сверху " +
    "осталось место под заголовок.",
  "edit.framing.pad_color":
    "Цвет полос сверху и снизу в режиме fit: black, white или #101014.",
  "ingest.cookies_from_browser":
    "Нужно, когда YouTube требует «подтверди, что не бот», а также для приватных, " +
    "возрастных и спонсорских видео. На Windows работает только firefox: Chrome и Edge " +
    "с версии 127 шифруют cookies так, что прочитать их не выходит. Браузер должен быть " +
    "закрыт во время загрузки.",
  "ingest.cookies_file":
    "Путь к cookies.txt в формате Netscape — запасной путь, если браузер прочитать нельзя. " +
    "Выгружается расширением вроде «Get cookies.txt LOCALLY».",
  "publish.tiktok.mode":
    "draft — ролик приходит во «Входящие» приложения TikTok, опубликовать его " +
    "нужно одним касанием с телефона. Работает сразу. direct — сразу в профиль, " +
    "без человека, но пока приложение не прошло аудит TikTok, выложенное так видно " +
    "только автору, то есть охвата не будет.",
  "publish.tiktok.privacy":
    "Только для режима direct и только из того, что разрешено самому аккаунту. " +
    "Пусто — взять самый закрытый из доступных.",
  "ingest.proxy":
    "Пусто — как настроено в системе. Адрес вида http://127.0.0.1:10809 или " +
    "socks5://127.0.0.1:10808 закрепляет один путь: YouTube привязывает ссылки " +
    "на видео к адресу, с которого запрошен плеер, и переключение узла на ходу " +
    "даёт 403. Слово direct заставляет качать мимо системного прокси.",
  "ingest.http_chunk_size_mb":
    "0 — качать одним запросом, как обычно. Если загрузка падает с 403, а список " +
    "качеств при этом читается, поставь 1–5: файл пойдёт короткими диапазонами, " +
    "которые такие адреса обычно пропускают. Медленнее, но доходит.",
  "ingest.player_clients":
    "Порядок клиентов, которыми yt-dlp притворяется. Разным клиентам YouTube отдаёт " +
    "разные наборы качеств и по-разному требует подтверждения. Менять стоит, только " +
    "если загрузка перестала работать.",
};

function BackgroundPicker({
  path,
  value,
  onChange,
  hint,
}: {
  path: string[];
  value: number[];
  onChange: (path: string[], next: unknown) => void;
  hint?: string;
}) {
  const assets = useAsync<Asset[]>(() => api.assets.list("background"), []);
  const items = assets.data ?? [];

  const toggle = (id: number) =>
    onChange(path, value.includes(id) ? value.filter((item) => item !== id) : [...value, id]);

  return (
    <div className="field wide">
      <label>Фоновые ролики — отмеченные участвуют в жеребьёвке</label>
      {items.length === 0 ? (
        <div className="config-hint">
          Ни одного фона не загружено. Добавь их на странице «Материалы», тип «background».
        </div>
      ) : (
        <div className="bg-picker">
          {items.map((asset) => (
            <label key={asset.id} className="check">
              <input
                type="checkbox"
                checked={value.includes(asset.id)}
                onChange={() => toggle(asset.id)}
              />
              <span className="truncate">{asset.name}</span>
            </label>
          ))}
        </div>
      )}
      <div className="config-hint">
        Выбрано: {value.length} из {items.length}.{hint ? ` ${hint}` : ""}
      </div>
    </div>
  );
}

const SELECTS: Record<string, string[]> = {
  "publish.tiktok.mode": ["draft", "direct"],
  "publish.tiktok.privacy": [
    "",
    "PUBLIC_TO_EVERYONE",
    "MUTUAL_FOLLOW_FRIENDS",
    "FOLLOWER_OF_CREATOR",
    "SELF_ONLY",
  ],
  "transcribe.provider": ["auto", "local", "openai", "deepgram", "aitunnel"],
  "analyze.provider": ["", "anthropic", "openai", "ollama", "aitunnel"],
  "caption.provider": ["", "anthropic", "openai", "ollama", "aitunnel"],
  // Список моделей распознавания фиксированный: faster-whisper принимает только
  // эти имена. Свободный ввод сюда уже приводил к тому, что вписывали название
  // языковой модели, и стадия падала.
  "transcribe.model": [
    "",
    "large-v3",
    "large-v3-turbo",
    "turbo",
    "medium",
    "small",
    "base",
    "tiny",
    "distil-large-v3",
  ],
  "edit.framing.mode": ["crop", "blur_pad", "fit"],
  "edit.banner.chromakey.mode": ["chromakey", "colorkey"],
  "edit.background.audio": ["clip", "mix"],
  "edit.zoom.mode": ["static", "kenburns"],
  "edit.zoom.apply_to": ["frame", "source"],
  "edit.mask.fit": ["cover", "contain", "stretch"],
  "edit.mask.blend": ["normal", "screen", "overlay", "multiply", "softlight"],
  "edit.banner.position": ["top", "bottom", "center", "custom"],
  "edit.title.position": ["top", "center", "bottom"],
  "edit.subtitles.position": ["bottom", "center", "top"],
  "edit.output.x264_preset": ["ultrafast", "veryfast", "fast", "medium", "slow", "slower"],
  "edit.output.hardware_encoder": ["", "h264_nvenc", "hevc_nvenc"],
  "publish.youtube.privacy": ["private", "unlisted", "public"],
  "analyze.output_language": ["de", "en", "ru"],
};

// Общая таблица подписей ищет по имени поля, а одинаковые имена в разных
// разделах значат разное: "color" — и группа настроек цвета, и цвет ключа;
// "duration" — и сколько секунд висит баннер, и длина куска при нарезке.
// Из-за второго совпадения нарезка была подписана «0 — до конца» (это про
// баннер), человек ставил ноль и получал упавшую стадию. Такие поля
// разводятся по полному пути.
const PATH_LABELS: Record<string, string> = {
  "edit.color": "Цвет и фильтр",
  "edit.banner.chromakey.color": "Цвет ключа",
  "edit.title.color": "Цвет текста",
  "edit.subtitles.color": "Цвет текста",
  "chunks.enabled": "Резать на куски вместо поиска фрагментов",
  "chunks.duration": "Длина куска, с (0 — весь ролик одним куском)",
  "chunks.overlap": "Нахлёст с предыдущим куском, с",
  "chunks.min_tail": "Хвост короче этого приклеить к предыдущему, с",
  "chunks.skip_start": "Пропустить в начале, с",
  "chunks.skip_end": "Пропустить в конце, с",
  "chunks.limit": "Взять не больше кусков (0 — сколько выйдет)",
  "chunks.title_template": "Шаблон заголовка",
};

const label = (path: string[]) => {
  const dotted = path.join(".");
  const key = path[path.length - 1] ?? "";
  return PATH_LABELS[dotted] ?? LABELS[key] ?? key;
};

function NumberField({
  label,
  value,
  hint,
  onCommit,
}: {
  label: string;
  value: number;
  hint?: string;
  onCommit: (next: number) => void;
}) {
  // Поле держит собственный текст, а наружу отдаёт только осмысленное число.
  // Раньше значение шло напрямую из input, а Number("") — это 0, не NaN:
  // стоило стереть «60», чтобы вписать «1320», как в конфиг уже уезжал ноль.
  // Для длины куска это означало упавшую нарезку, для crf или fps — тихо
  // испорченный монтаж.
  const [raw, setRaw] = useState(String(value));
  const [editing, setEditing] = useState(false);

  // Значение могло смениться снаружи: другой пресет, сброс формы.
  if (!editing && raw !== String(value)) {
    setRaw(String(value));
  }

  const isFraction = !Number.isInteger(value) || Math.abs(value) < 3;

  return (
    <div className="field">
      <label>{label}</label>
      <input
        type="number"
        step={isFraction ? 0.01 : 1}
        value={raw}
        onFocus={() => setEditing(true)}
        onChange={(event) => {
          const next = event.target.value;
          setRaw(next);
          if (next.trim() === "") return; // пустое поле — ещё не число
          const parsed = Number(next);
          if (!Number.isNaN(parsed)) onCommit(parsed);
        }}
        onBlur={() => {
          setEditing(false);
          // Ушли, ничего не вписав, — возвращаем то, что было.
          if (raw.trim() === "" || Number.isNaN(Number(raw))) setRaw(String(value));
        }}
      />
      {hint && <div className="config-hint">{hint}</div>}
    </div>
  );
}


export function setDeep(config: Config, path: string[], value: unknown): Config {
  if (path.length === 0) return config;
  const [head, ...rest] = path;
  return {
    ...config,
    [head]: rest.length === 0 ? value : setDeep(config[head] ?? {}, rest, value),
  };
}

function Field({
  path,
  value,
  onChange,
}: {
  path: string[];
  value: unknown;
  onChange: (path: string[], value: unknown) => void;
}) {
  const key = path[path.length - 1];
  const dotted = path.join(".");
  const hint = HINTS[dotted];
  const options = SELECTS[dotted];

  if (typeof value === "boolean") {
    return (
      <div className="field">
        <label style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 0 }}>
          <input
            type="checkbox"
            checked={value}
            onChange={(event) => onChange(path, event.target.checked)}
          />
          <span style={{ color: "var(--text)" }}>{label(path)}</span>
        </label>
        {hint && <div className="config-hint">{hint}</div>}
      </div>
    );
  }

  if (options) {
    return (
      <div className="field">
        <label>{label(path)}</label>
        <select
          value={String(value ?? "")}
          onChange={(event) => onChange(path, event.target.value)}
        >
          {options.map((option) => (
            <option key={option} value={option}>
              {option === "" ? "— не задано —" : option}
            </option>
          ))}
        </select>
        {hint && <div className="config-hint">{hint}</div>}
      </div>
    );
  }

  if (typeof value === "number") {
    return (
      <NumberField
        label={label(path)}
        value={value}
        hint={hint}
        onCommit={(next) => onChange(path, next)}
      />
    );
  }

  // Фоны выбираются галочками: помнить номера ассетов невозможно, а список
  // здесь длинный — в этом и смысл функции.
  const full = path.join(".");
  if (full === "edit.background.asset_ids") {
    return (
      <BackgroundPicker path={path} value={(value as number[]) ?? []} onChange={onChange} hint={hint} />
    );
  }
  if (Array.isArray(value)) {
    return (
      <div className="field wide">
        <label>{label(path)} — через запятую</label>
        <input
          value={value.join(", ")}
          onChange={(event) =>
            onChange(
              path,
              event.target.value
                .split(",")
                .map((item) => item.trim())
                .filter(Boolean)
                .map((item) => (/^\d+$/.test(item) ? Number(item) : item)),
            )
          }
        />
        {hint && <div className="config-hint">{hint}</div>}
      </div>
    );
  }

  if (value === null || typeof value === "string") {
    const multiline = key.includes("template") || key.includes("instructions");
    return (
      <div className={`field${multiline ? " wide" : ""}`}>
        <label>{label(path)}</label>
        {multiline ? (
          <textarea
            value={String(value ?? "")}
            onChange={(event) => onChange(path, event.target.value)}
          />
        ) : (
          <input
            value={String(value ?? "")}
            placeholder={value === null ? "не задано" : ""}
            onChange={(event) => onChange(path, event.target.value || (value === null ? null : ""))}
          />
        )}
        {hint && <div className="config-hint">{hint}</div>}
      </div>
    );
  }

  return null;
}

function Group({
  path,
  value,
  onChange,
  depth,
}: {
  path: string[];
  value: Config;
  onChange: (path: string[], value: unknown) => void;
  depth: number;
}) {
  // Расписание собрано в отдельный экран: его поля связаны между собой, и
  // построчный редактор показывал бы настройки от обоих режимов сразу.
  if (path.join(".") === "publish.schedule") {
    return (
      <details className="config-group" open>
        <summary>{label(path)}</summary>
        <div className="config-body">
          <SchedulePicker value={value} onChange={(next) => onChange(path, next)} />
        </div>
      </details>
    );
  }

  const entries = Object.entries(value);
  const scalars = entries.filter(([, item]) => typeof item !== "object" || item === null || Array.isArray(item));
  const groups = entries.filter(
    ([, item]) => typeof item === "object" && item !== null && !Array.isArray(item),
  );

  return (
    <details className="config-group" open={depth === 0}>
      <summary>{label(path.length ? path : ["Настройки"])}</summary>
      <div className="config-body">
        {scalars.map(([key, item]) => (
          <Field key={key} path={[...path, key]} value={item} onChange={onChange} />
        ))}
      </div>
      {groups.length > 0 && (
        <div style={{ padding: "0 14px 12px" }}>
          {groups.map(([key, item]) => (
            <Group
              key={key}
              path={[...path, key]}
              value={item as Config}
              onChange={onChange}
              depth={depth + 1}
            />
          ))}
        </div>
      )}
    </details>
  );
}

// Порядок и есть объяснение: разделы идут ровно так, как проходит работа —
// скачали, распознали, нарезали, смонтировали, подписали, выложили. Раньше все
// десять групп лежали одинаковыми складками, и связи с конвейером не читалось.
const STEPS: { keys: string[]; title: string; note: string }[] = [
  { keys: ["ingest"], title: "1. Исходник", note: "откуда и как качаем" },
  {
    keys: ["transcribe", "analyze", "chunks", "cut"],
    title: "2. Разбор",
    note: "речь, поиск моментов, нарезка",
  },
  { keys: ["edit"], title: "3. Монтаж", note: "как выглядит готовый ролик" },
  { keys: ["caption"], title: "4. Тексты", note: "заголовок, описание, хэштеги" },
  { keys: ["publish"], title: "5. Публикация", note: "площадки и расписание" },
];

/**
 * Убирает ветку из переопределений.
 *
 * Не то же самое, что записать в неё пустой объект: пустая ветка осталась бы
 * переопределением и продолжила перекрывать пресет — только теперь ничем.
 */
export function dropPath(config: Config, path: string[]): Config {
  const [head, ...rest] = path;
  if (!(head in config)) return config;
  const next = { ...config };
  if (rest.length === 0) {
    delete next[head];
  } else {
    const child = dropPath(next[head] ?? {}, rest);
    if (Object.keys(child).length === 0) delete next[head];
    else next[head] = child;
  }
  return next;
}

export function ConfigEditor({
  config,
  onChange,
}: {
  config: Config;
  onChange: (next: Config) => void;
}) {
  const update = (path: string[], value: unknown) => onChange(setDeep(config, path, value));
  const [step, setStep] = useState(0);

  const isSection = (key: string) => {
    const value = (config as Record<string, unknown>)[key];
    return typeof value === "object" && value !== null && !Array.isArray(value);
  };

  const steps = STEPS.map((item) => ({ ...item, keys: item.keys.filter(isSection) })).filter(
    (item) => item.keys.length > 0,
  );

  // Ветки, которых нет в списке шагов, показываем в конце: иначе новая настройка
  // в конфиге просто пропала бы из редактора, и найти её было бы негде.
  const known = new Set(steps.flatMap((item) => item.keys));
  const rest = Object.keys(config).filter((key) => isSection(key) && !known.has(key));
  if (rest.length) steps.push({ keys: rest, title: "Прочее", note: "не разложено по шагам" });

  const current = steps[Math.min(step, steps.length - 1)];

  return (
    <div>
      <div className="steps">
        {steps.map((item, index) => (
          <button
            key={item.title}
            type="button"
            className={`step${index === Math.min(step, steps.length - 1) ? " on" : ""}`}
            onClick={() => setStep(index)}
          >
            <b>{item.title}</b>
            <small>{item.note}</small>
          </button>
        ))}
      </div>

      {current?.keys.map((key) => (
        <Group
          key={key}
          path={[key]}
          value={(config as Record<string, unknown>)[key] as Config}
          onChange={update}
          depth={0}
        />
      ))}
    </div>
  );
}
