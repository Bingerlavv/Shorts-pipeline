import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { dropPath, setDeep } from "./ConfigEditor";
import { SchedulePicker } from "./SchedulePicker";
import type { Preset, Project } from "../types";

type Schedule = Record<string, any>;

/** Короткое человеческое описание сетки выхода — для свёрнутого вида. */
function summarize(schedule: Schedule | undefined): string {
  if (!schedule || !schedule.enabled) return "Ролики уходят сразу после монтажа";

  const days =
    Array.isArray(schedule.weekdays) && schedule.weekdays.length
      ? `${schedule.weekdays.length} дн/нед`
      : "каждый день";
  const first = Number(schedule.start_offset_minutes) || 0;
  const firstNote = first ? `, первый через ${first} мин` : "";
  const limit = Number(schedule.daily_limit) || 0;
  const limitNote = limit ? `, до ${limit}/сут` : "";

  if (schedule.mode === "spacing") {
    const step = Number(schedule.spacing_minutes) || 0;
    const win =
      schedule.window_start || schedule.window_end
        ? ` ${schedule.window_start || "…"}–${schedule.window_end || "…"}`
        : "";
    return `Каждые ${step} мин${win} — ${days}${firstNote}${limitNote}`;
  }

  const times = (Array.isArray(schedule.times) ? schedule.times : []).join(" · ");
  return `По часам ${times || "(не заданы)"} — ${days}${firstNote}${limitNote}`;
}

/**
 * Планирование выхода прямо в проекте, из верхней части страницы.
 *
 * Свёрнуто — одна строка с текущей сеткой. Развёрнуто — тот же SchedulePicker,
 * что и в настройках, но привязанный к переопределениям проекта и с
 * автосохранением: правишь часы — они сразу уходят на сервер.
 */
export function ReleasePlan({
  project,
  presets,
  onSaved,
}: {
  project: Project;
  presets: Preset[];
  onSaved?: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [overrides, setOverrides] = useState<Record<string, any>>(
    project.config_overrides ?? {},
  );
  const [saveState, setSaveState] = useState<"idle" | "saving" | "ok" | "err">("idle");
  const timer = useRef<ReturnType<typeof setTimeout>>();

  // Проект перечитали снаружи (монтаж, настройки) — подхватываем свежие overrides.
  const external = JSON.stringify(project.config_overrides ?? {});
  useEffect(() => {
    setOverrides(project.config_overrides ?? {});
  }, [external]);

  const preset = presets.find((item) => item.id === project.preset_id);
  const own: Schedule | undefined = overrides.publish?.schedule;
  const fromPreset: Schedule | undefined = preset?.config?.publish?.schedule;
  const effective = own ?? fromPreset ?? {};

  const persist = (next: Record<string, any>) => {
    setOverrides(next);
    setSaveState("saving");
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(async () => {
      try {
        await api.projects.update(project.id, { config_overrides: next });
        setSaveState("ok");
        onSaved?.();
      } catch {
        setSaveState("err");
      }
    }, 600);
  };

  const setSchedule = (schedule: Schedule) =>
    persist(setDeep(overrides, ["publish", "schedule"], schedule));

  return (
    <div className="card" style={{ marginBottom: 14 }}>
      <div className="row between">
        <div>
          <b>Расписание выхода</b>
          <div className="muted" style={{ fontSize: 12.5 }}>
            {summarize(effective)}
            {!own && effective.enabled ? " · из пресета" : ""}
          </div>
        </div>
        <div className="row" style={{ gap: 10 }}>
          {saveState === "saving" && (
            <span className="muted" style={{ fontSize: 12 }}>
              сохраняю…
            </span>
          )}
          {saveState === "ok" && (
            <span className="muted" style={{ fontSize: 12 }}>
              сохранено
            </span>
          )}
          {saveState === "err" && (
            <span style={{ fontSize: 12, color: "var(--err)" }}>не сохранилось</span>
          )}
          <button className="small" onClick={() => setOpen((value) => !value)}>
            {open ? "Свернуть" : own ? "Изменить" : "Настроить"}
          </button>
        </div>
      </div>

      {open && (
        <div style={{ marginTop: 12 }}>
          {!own ? (
            <button
              className="small ghost"
              onClick={() => setSchedule({ ...effective, enabled: true })}
            >
              Задать своё расписание для этого проекта
            </button>
          ) : (
            <>
              <SchedulePicker
                value={overrides.publish?.schedule ?? {}}
                onChange={setSchedule}
              />
              <button
                className="small ghost"
                style={{ marginTop: 10 }}
                onClick={() => persist(dropPath(overrides, ["publish", "schedule"]))}
              >
                Убрать своё — вернуться к пресету
              </button>
            </>
          )}
        </div>
      )}
    </div>
  );
}
