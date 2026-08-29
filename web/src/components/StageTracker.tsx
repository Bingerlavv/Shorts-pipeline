import type { Project, Segment } from "../types";

type StageState = "done" | "running" | "next" | "skipped" | "waiting" | "blocked";

type Stage = {
  key: string;
  title: string;
  /** Что здесь уже получилось. Пусто — шаг ещё не проходили. */
  result: string;
  state: StageState;
  action?: { label: string; run: () => void; disabled?: boolean; hint?: string };
  extra?: { label: string; run: () => void; disabled?: boolean; hint?: string };
};

const MARK: Record<StageState, string> = {
  done: "✓",
  running: "⟳",
  next: "→",
  skipped: "–",
  waiting: "·",
  blocked: "·",
};

/**
 * Конвейер как цепочка, а не как панель кнопок.
 *
 * Раньше шесть действий лежали в ряд одинаковыми кнопками: по ним нельзя было
 * понять ни что уже сделано, ни что делать дальше. Порядок шагов и есть главная
 * подсказка — здесь он виден, а действие живёт на своём шаге.
 */
export function StageTracker({
  project,
  segments,
  activeJob,
  busy,
  onRun,
  onRenderApproved,
  onPublishAll,
}: {
  project: Project;
  segments: Segment[];
  activeJob?: { type: string };
  busy: boolean;
  onRun: (stage: string) => void;
  onRenderApproved: () => void;
  onPublishAll: () => void;
}) {
  const running = activeJob?.type ?? "";
  const countOf = (statuses: string[]) =>
    segments.filter((item) => statuses.includes(item.status)).length;

  const downloaded = project.duration > 0;
  const transcribed = project.has_transcript;
  const found = segments.length > 0;
  const inWork = countOf(["approved", "rendering", "rendered", "publishing", "published"]);
  const rendered = segments.filter((item) => item.render_path).length;
  const published = countOf(["published"]);

  const done = [downloaded, transcribed, found, inWork > 0, rendered > 0, published > 0];

  // Следующий шаг ищем после самого дальнего пройденного, а не просто первый
  // непройденный. Иначе проект, нарезанный на равные куски, звал бы распознать
  // речь — хотя он этот шаг законно миновал и давно смонтирован.
  const lastDone = done.lastIndexOf(true);
  const nextIndex = done.findIndex((value, index) => !value && index > lastDone);

  const state = (index: number, blocked = false): StageState => {
    if (done[index]) return "done";
    if (index < lastDone) return "skipped";
    if (blocked) return "blocked";
    return index === nextIndex ? "next" : "waiting";
  };

  const stages: Stage[] = [
    {
      key: "ingest",
      title: "Исходник",
      result: downloaded
        ? `${Math.round(project.duration / 60)} мин${project.width ? `, ${project.height}p` : ""}`
        : "не загружен",
      state: running === "project.ingest" ? "running" : state(0),
      action: {
        label: downloaded ? "Загрузить заново" : "Загрузить",
        run: () => onRun("ingest"),
        disabled: busy,
      },
    },
    {
      key: "transcribe",
      title: "Речь",
      result: transcribed ? "распознана" : found ? "не понадобилась" : "не распознана",
      state: running === "project.transcribe" ? "running" : state(1, !downloaded),
      action: {
        label: "Распознать",
        run: () => onRun("transcribe"),
        disabled: busy || !downloaded,
        hint: downloaded ? undefined : "Сначала нужен исходник",
      },
    },
    {
      key: "analyze",
      title: "Фрагменты",
      result: found ? `найдено ${segments.length}` : "не искали",
      state: running === "project.analyze" ? "running" : state(2, !downloaded),
      action: {
        label: "Найти",
        run: () => onRun("analyze"),
        disabled: busy || !transcribed,
        hint: transcribed ? undefined : "Нужна распознанная речь",
      },
      // Плоская нарезка — не дополнение, а замена поиску: режет на равные куски
      // без распознавания. Поэтому стоит рядом, а не отдельной кнопкой в общем ряду.
      extra: {
        label: "Порезать на равные",
        run: () => onRun("chunks"),
        disabled: busy || !downloaded,
        hint: "Без распознавания речи: ровные куски подряд. Длина — в настройках проекта",
      },
    },
    {
      key: "review",
      title: "Ревью",
      result: inWork > 0 ? `в работе ${inWork}` : found ? `ждут ${countOf(["candidate"])}` : "нечего смотреть",
      state: state(3, !found),
    },
    {
      key: "render",
      title: "Монтаж",
      result: rendered > 0 ? `готово ${rendered}` : "не монтировали",
      state: running === "segment.render" ? "running" : state(4, inWork === 0),
      action: {
        label: "Смонтировать взятые",
        run: onRenderApproved,
        disabled: busy || inWork === 0,
        hint: inWork > 0 ? undefined : "Сначала отправь фрагменты в работу",
      },
    },
    {
      key: "publish",
      title: "Публикация",
      result: published > 0 ? `вышло ${published}` : "не публиковали",
      state: running === "segment.publish" ? "running" : state(5, rendered === 0),
      action: {
        label: "Опубликовать",
        run: onPublishAll,
        disabled: busy || rendered === 0,
        hint: rendered > 0 ? undefined : "Сначала смонтируй ролики",
      },
    },
  ];

  return (
    <ol className="chain">
      {stages.map((stage, index) => (
        <li key={stage.key} className={`chain-step is-${stage.state}`}>
          <div className="chain-top">
            <span className="chain-mark" aria-hidden="true">
              {MARK[stage.state]}
            </span>
            <b>{stage.title}</b>
            <span className="chain-num">{index + 1}</span>
          </div>
          <span className="chain-result">{stage.result}</span>
          {stage.action && (
            <button
              className={`small${stage.state === "next" ? " primary" : " ghost"}`}
              disabled={stage.action.disabled}
              title={stage.action.hint}
              onClick={stage.action.run}
            >
              {stage.action.label}
            </button>
          )}
          {stage.extra && (
            <button
              className="small ghost"
              disabled={stage.extra.disabled}
              title={stage.extra.hint}
              onClick={stage.extra.run}
            >
              {stage.extra.label}
            </button>
          )}
        </li>
      ))}
    </ol>
  );
}
