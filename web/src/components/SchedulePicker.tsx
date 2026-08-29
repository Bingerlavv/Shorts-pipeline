import { useEffect, useState } from "react";
import { api } from "../api";

const WEEKDAYS = [
  { value: 1, short: "Пн" },
  { value: 2, short: "Вт" },
  { value: 3, short: "Ср" },
  { value: 4, short: "Чт" },
  { value: 5, short: "Пт" },
  { value: 6, short: "Сб" },
  { value: 7, short: "Вс" },
];

type Schedule = Record<string, any>;

function clock(value: unknown): string {
  const text = String(value ?? "").trim();
  return /^\d{1,2}:\d{2}$/.test(text) ? text.padStart(5, "0") : "";
}

/**
 * Настройка сетки выхода: когда именно уходят ролики.
 *
 * Отдельный экран, а не строчки общего редактора, потому что настройки здесь
 * связаны между собой. «Каждые 180 минут» и «в 10:00, 15:00 и 20:00» — разные
 * способы думать о расписании, и показывать поля от обоих сразу значит
 * предлагать заполнить то, что всё равно не сработает.
 */
export function SchedulePicker({
  value,
  onChange,
}: {
  value: Schedule;
  onChange: (next: Schedule) => void;
}) {
  const [preview, setPreview] = useState<string[]>([]);
  const [previewError, setPreviewError] = useState("");
  const [newTime, setNewTime] = useState("12:00");

  const enabled = Boolean(value.enabled);
  const mode: string = value.mode === "spacing" ? "spacing" : "times";
  const times: string[] = Array.isArray(value.times) ? value.times.map(clock).filter(Boolean) : [];
  const weekdays: number[] = Array.isArray(value.weekdays) ? value.weekdays.map(Number) : [];

  const set = (patch: Schedule) => onChange({ ...value, ...patch });

  // Предпросмотр считает сервер — той же функцией, что и настоящее расписание.
  // Пауза нужна, чтобы правка часов не била в API на каждое нажатие.
  useEffect(() => {
    if (!enabled) {
      setPreview([]);
      return;
    }
    const timer = setTimeout(() => {
      api.publications
        .schedulePreview({ schedule: value, count: 8 })
        .then((slots) => {
          setPreview(slots);
          setPreviewError("");
        })
        .catch((exc) => setPreviewError((exc as Error).message));
    }, 350);
    return () => clearTimeout(timer);
  }, [JSON.stringify(value), enabled]);

  const addTime = () => {
    const next = clock(newTime);
    if (!next || times.includes(next)) return;
    set({ times: [...times, next].sort() });
  };

  const toggleDay = (day: number) =>
    set({
      weekdays: weekdays.includes(day)
        ? weekdays.filter((item) => item !== day)
        : [...weekdays, day].sort((a, b) => a - b),
    });

  return (
    <div className="schedule">
      <label className="check">
        <input
          type="checkbox"
          checked={enabled}
          onChange={(event) => set({ enabled: event.target.checked })}
        />
        Выкладывать по расписанию
      </label>
      <div className="config-hint">
        Выключено — ролик уходит сразу после монтажа. Времена считаются по часам этого
        компьютера и отдельно для каждого аккаунта.
      </div>

      {enabled && (
        <>
          <div className="schedule-modes">
            {[
              { key: "times", title: "По часам", note: "10:00, 15:00, 20:00" },
              { key: "spacing", title: "Через интервал", note: "каждые N минут" },
            ].map((item) => (
              <button
                key={item.key}
                type="button"
                className={`schedule-mode${mode === item.key ? " on" : ""}`}
                onClick={() => set({ mode: item.key })}
              >
                <b>{item.title}</b>
                <small>{item.note}</small>
              </button>
            ))}
          </div>

          {mode === "times" ? (
            <div className="field wide">
              <label>Время выхода</label>
              <div className="chips">
                {times.map((item) => (
                  <span key={item} className="chip">
                    {item}
                    <button
                      type="button"
                      onClick={() => set({ times: times.filter((t) => t !== item) })}
                      aria-label={`Убрать ${item}`}
                    >
                      ×
                    </button>
                  </span>
                ))}
                <input
                  type="time"
                  value={newTime}
                  onChange={(event) => setNewTime(event.target.value)}
                  style={{ width: 110 }}
                />
                <button type="button" className="small" onClick={addTime}>
                  Добавить
                </button>
              </div>
              {times.length === 0 && (
                <div className="config-hint">
                  Ни одного часа не задано — расписание работать не будет, ролики уйдут
                  по интервалу.
                </div>
              )}
            </div>
          ) : (
            <div className="row">
              <div className="field">
                <label>Интервал, минут</label>
                <input
                  type="number"
                  min={0}
                  value={Number(value.spacing_minutes ?? 0)}
                  onChange={(event) => set({ spacing_minutes: Number(event.target.value) })}
                />
              </div>
              <div className="field">
                <label>Не раньше</label>
                <input
                  type="time"
                  value={clock(value.window_start)}
                  onChange={(event) => set({ window_start: event.target.value })}
                />
              </div>
              <div className="field">
                <label>Не позже</label>
                <input
                  type="time"
                  value={clock(value.window_end)}
                  onChange={(event) => set({ window_end: event.target.value })}
                />
              </div>
            </div>
          )}

          <div className="field wide">
            <label>Дни недели</label>
            <div className="chips">
              {WEEKDAYS.map((day) => (
                <button
                  key={day.value}
                  type="button"
                  className={`chip toggle${
                    weekdays.length === 0 || weekdays.includes(day.value) ? " on" : ""
                  }`}
                  onClick={() => toggleDay(day.value)}
                >
                  {day.short}
                </button>
              ))}
            </div>
            <div className="config-hint">
              {weekdays.length === 0
                ? "Отмечены все — значит выходим каждый день."
                : `Только ${weekdays.length} ${weekdays.length === 1 ? "день" : "дня(ей)"} в неделю.`}
            </div>
          </div>

          <div className="row">
            <div className="field">
              <label>Первая публикация через, минут</label>
              <input
                type="number"
                min={0}
                value={Number(value.start_offset_minutes ?? 0)}
                onChange={(event) => set({ start_offset_minutes: Number(event.target.value) })}
              />
            </div>
            <div className="field">
              <label>Не больше в сутки</label>
              <input
                type="number"
                min={0}
                value={Number(value.daily_limit ?? 0)}
                onChange={(event) => set({ daily_limit: Number(event.target.value) })}
              />
            </div>
          </div>
          <div className="config-hint">
            Лимит считается на аккаунт. 0 — без ограничения, тогда в сутки уходит столько,
            сколько помещается в сетку.
          </div>

          <div className="field wide">
            <label>Ближайшие выходы</label>
            {previewError ? (
              <div className="config-hint">Не удалось посчитать: {previewError}</div>
            ) : preview.length === 0 ? (
              <div className="config-hint">Считаю…</div>
            ) : (
              <ol className="slots">
                {preview.map((iso) => {
                  const moment = new Date(iso);
                  return (
                    <li key={iso}>
                      <span className="slot-day">
                        {moment.toLocaleDateString("ru-RU", {
                          weekday: "short",
                          day: "2-digit",
                          month: "2-digit",
                        })}
                      </span>
                      <b>
                        {moment.toLocaleTimeString("ru-RU", {
                          hour: "2-digit",
                          minute: "2-digit",
                        })}
                      </b>
                    </li>
                  );
                })}
              </ol>
            )}
            <div className="config-hint">
              Так лягут восемь ближайших роликов, если очередь на аккаунте пуста.
              Уже поставленные публикации сдвинут сетку дальше.
            </div>
          </div>
        </>
      )}
    </div>
  );
}
