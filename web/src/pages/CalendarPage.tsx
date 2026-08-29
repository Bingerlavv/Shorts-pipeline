import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { Banner, Empty } from "../components/ui";
import { useAsync } from "../hooks/useLiveState";
import type { Account, Publication } from "../types";

const WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"];

const MONTHS = [
  "январь", "февраль", "март", "апрель", "май", "июнь",
  "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
];

const TONE: Record<string, string> = {
  published: "ok",
  succeeded: "ok",
  scheduled: "warn",
  pending: "warn",
  uploading: "info",
  failed: "err",
  cancelled: "neutral",
};

/** Понедельник той недели, в которую попадает день. */
function weekStart(day: Date): Date {
  const shift = (day.getDay() + 6) % 7;
  return new Date(day.getFullYear(), day.getMonth(), day.getDate() - shift);
}

function sameDay(a: Date, b: Date): boolean {
  return (
    a.getFullYear() === b.getFullYear() &&
    a.getMonth() === b.getMonth() &&
    a.getDate() === b.getDate()
  );
}

function dayKey(value: Date): string {
  return `${value.getFullYear()}-${value.getMonth()}-${value.getDate()}`;
}

export default function CalendarPage() {
  const accounts = useAsync<Account[]>(() => api.accounts.list(), []);
  const [month, setMonth] = useState(() => {
    const now = new Date();
    return new Date(now.getFullYear(), now.getMonth(), 1);
  });
  const [accountId, setAccountId] = useState<number | null>(null);
  const [items, setItems] = useState<Publication[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  // Сетка всегда начинается с понедельника и кончается воскресеньем, поэтому
  // захватывает хвосты соседних месяцев — грузим ровно то, что показываем.
  const gridStart = useMemo(() => weekStart(month), [month]);
  const gridEnd = useMemo(() => {
    const last = new Date(month.getFullYear(), month.getMonth() + 1, 0);
    const end = weekStart(last);
    return new Date(end.getFullYear(), end.getMonth(), end.getDate() + 7);
  }, [month]);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    api.publications
      .range(gridStart, gridEnd, accountId)
      .then((data) => {
        if (!alive) return;
        setItems(data);
        setError("");
      })
      .catch((exc) => alive && setError((exc as Error).message))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [gridStart.getTime(), gridEnd.getTime(), accountId]);

  const nameById = new Map((accounts.data ?? []).map((item) => [item.id, item.name]));

  const byDay = useMemo(() => {
    const map = new Map<string, Publication[]>();
    for (const item of items) {
      const iso = item.published_at ?? item.scheduled_at;
      if (!iso) continue;
      const key = dayKey(new Date(iso));
      const bucket = map.get(key);
      if (bucket) bucket.push(item);
      else map.set(key, [item]);
    }
    for (const bucket of map.values()) {
      bucket.sort((a, b) =>
        (a.published_at ?? a.scheduled_at ?? "").localeCompare(b.published_at ?? b.scheduled_at ?? ""),
      );
    }
    return map;
  }, [items]);

  const days: Date[] = [];
  for (let cursor = new Date(gridStart); cursor < gridEnd; cursor.setDate(cursor.getDate() + 1)) {
    days.push(new Date(cursor));
  }

  const today = new Date();
  const shift = (delta: number) =>
    setMonth(new Date(month.getFullYear(), month.getMonth() + delta, 1));

  const inMonth = items.filter((item) => {
    const iso = item.published_at ?? item.scheduled_at;
    return iso && new Date(iso).getMonth() === month.getMonth();
  });
  const ahead = inMonth.filter((item) => item.status === "scheduled" || item.status === "pending");

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Календарь</h1>
          <p>
            Что и когда уходит на аккаунты. Прошедшие показаны по фактическому времени
            выхода, будущие — по запланированному.
          </p>
        </div>
        <div className="row">
          <select
            value={accountId ?? ""}
            onChange={(event) => setAccountId(event.target.value ? Number(event.target.value) : null)}
          >
            <option value="">Все аккаунты</option>
            {(accounts.data ?? []).map((account) => (
              <option key={account.id} value={account.id}>
                {account.name}
              </option>
            ))}
          </select>
          <button onClick={() => shift(-1)} aria-label="Предыдущий месяц">
            ←
          </button>
          <button onClick={() => setMonth(new Date(today.getFullYear(), today.getMonth(), 1))}>
            Сегодня
          </button>
          <button onClick={() => shift(1)} aria-label="Следующий месяц">
            →
          </button>
        </div>
      </div>

      {error && <Banner tone="err">{error}</Banner>}

      <div className="cal-head">
        <h2>
          {MONTHS[month.getMonth()]} {month.getFullYear()}
        </h2>
        <span className="muted">
          {inMonth.length === 0
            ? "в этом месяце пусто"
            : `всего ${inMonth.length}, из них ждут выхода ${ahead.length}`}
        </span>
      </div>

      <div className="card" style={{ padding: 0, overflow: "hidden" }}>
        <div className="cal-grid cal-weekdays">
          {WEEKDAYS.map((day) => (
            <div key={day} className="cal-weekday">
              {day}
            </div>
          ))}
        </div>
        <div className="cal-grid">
          {days.map((day) => {
            const bucket = byDay.get(dayKey(day)) ?? [];
            const outside = day.getMonth() !== month.getMonth();
            return (
              <div
                key={dayKey(day)}
                className={`cal-day${outside ? " outside" : ""}${
                  sameDay(day, today) ? " today" : ""
                }`}
              >
                <span className="cal-date">{day.getDate()}</span>
                {bucket.map((item) => {
                  const moment = new Date(item.published_at ?? item.scheduled_at ?? "");
                  const time = moment.toLocaleTimeString("ru-RU", {
                    hour: "2-digit",
                    minute: "2-digit",
                  });
                  const account = nameById.get(item.account_id) ?? `аккаунт ${item.account_id}`;
                  const body = (
                    <>
                      <i className={`dot ${TONE[item.status] ?? "neutral"}`} />
                      <b>{time}</b>
                      <span className="truncate">{account}</span>
                    </>
                  );
                  const title = `${item.title || "без заголовка"} → ${account} (${item.platform})${
                    item.error ? `\n${item.error}` : ""
                  }`;
                  return item.project_id ? (
                    <Link
                      key={item.id}
                      className="cal-item"
                      to={`/projects/${item.project_id}#segment-${item.segment_id}`}
                      title={title}
                    >
                      {body}
                    </Link>
                  ) : (
                    <span key={item.id} className="cal-item" title={title}>
                      {body}
                    </span>
                  );
                })}
              </div>
            );
          })}
        </div>
      </div>

      {loading && items.length === 0 && <Empty>Загружаю…</Empty>}

      <div className="cal-legend">
        {[
          ["warn", "ждёт выхода"],
          ["info", "загружается"],
          ["ok", "опубликовано"],
          ["err", "ошибка"],
        ].map(([tone, text]) => (
          <span key={tone}>
            <i className={`dot ${tone}`} /> {text}
          </span>
        ))}
      </div>
    </>
  );
}
