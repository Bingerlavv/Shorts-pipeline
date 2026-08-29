import type { ReactNode } from "react";
import { useEffect } from "react";

const PILL_TONE: Record<string, string> = {
  new: "neutral",
  candidate: "neutral",
  rejected: "neutral",
  cancelled: "neutral",
  queued: "neutral",
  downloading: "info",
  transcribing: "info",
  analyzing: "info",
  rendering: "info",
  running: "info",
  publishing: "info",
  uploading: "info",
  approved: "mark",
  ready: "warn",
  pending: "warn",
  scheduled: "warn",
  rendered: "warn",
  done: "ok",
  published: "ok",
  succeeded: "ok",
  failed: "err",
};

const PILL_LABEL: Record<string, string> = {
  new: "новый",
  downloading: "качаю",
  transcribing: "распознаю",
  analyzing: "анализ",
  ready: "на ревью",
  rendering: "монтаж",
  done: "готово",
  failed: "ошибка",
  candidate: "кандидат",
  approved: "в работу",
  rejected: "отклонён",
  rendered: "смонтирован",
  publishing: "публикую",
  published: "опубликован",
  queued: "в очереди",
  running: "идёт",
  succeeded: "успешно",
  cancelled: "отменена",
  pending: "ожидает",
  scheduled: "по расписанию",
  uploading: "загрузка",
};

export function statusLabel(status: string): string {
  return PILL_LABEL[status] ?? status;
}

export function StatusPill({ status }: { status: string }) {
  return (
    <span className={`pill ${PILL_TONE[status] ?? "neutral"}`}>
      {PILL_LABEL[status] ?? status}
    </span>
  );
}

export function Progress({ value, failed }: { value: number; failed?: boolean }) {
  return (
    <div className={`progress${failed ? " err" : ""}`}>
      <span style={{ width: `${Math.round(Math.min(1, Math.max(0, value)) * 100)}%` }} />
    </div>
  );
}

/** Оценка как уровень на приборе: пять делений читаются быстрее числа. */
export function ScoreMeter({ value }: { value: number }) {
  const lit = Math.max(0, Math.min(5, Math.round(value * 5)));
  return (
    <span
      className={`meter${lit >= 4 ? " high" : ""}`}
      title={`оценка ${value.toFixed(2)}`}
      aria-label={`оценка ${value.toFixed(2)} из 1`}
    >
      {[0, 1, 2, 3, 4].map((i) => (
        <i key={i} className={i < lit ? "on" : ""} />
      ))}
    </span>
  );
}

/**
 * След фрагмента на исходнике. Показывает, из какого места плёнки он вырезан
 * и склеен ли из нескольких кусков — того, чего не видно ни по таймкоду,
 * ни по превью.
 */
export function SourceStrip({
  ranges,
  duration,
  quiet,
}: {
  ranges: number[][];
  duration: number;
  quiet?: boolean;
}) {
  if (!duration || duration <= 0) return null;
  return (
    <div className={`strip${quiet ? " quiet" : ""}`} aria-hidden="true">
      {ranges.map(([start, end], index) => (
        <u
          key={index}
          style={{
            left: `${(start / duration) * 100}%`,
            width: `${Math.max(0.35, ((end - start) / duration) * 100)}%`,
          }}
        />
      ))}
    </div>
  );
}

export function Banner({ tone, children }: { tone: "err" | "warn" | "info"; children: ReactNode }) {
  return <div className={`banner ${tone}`}>{children}</div>;
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

export function Readout({
  label,
  value,
  note,
  children,
}: {
  label: string;
  value: ReactNode;
  note?: ReactNode;
  children?: ReactNode;
}) {
  return (
    <div className="card readout">
      <span className="label">{label}</span>
      <b>{value}</b>
      {note && <small>{note}</small>}
      {children}
    </div>
  );
}

export function Modal({
  title,
  onClose,
  children,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
}) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(event) => event.stopPropagation()}>
        <div className="row between" style={{ marginBottom: 16 }}>
          <h2>{title}</h2>
          <button className="small ghost" onClick={onClose}>
            Закрыть
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}

const JOB_LABEL: Record<string, string> = {
  "project.ingest": "Скачивание исходника",
  "project.transcribe": "Распознавание речи",
  "project.analyze": "Поиск фрагментов",
  "segment.caption": "Тексты для площадок",
  "segment.render": "Монтаж фрагмента",
  "segment.publish": "Публикация",
};

export function jobLabel(type: string): string {
  return JOB_LABEL[type] ?? type;
}

export function formatDuration(seconds: number): string {
  if (!seconds || seconds < 0) return "—";
  const total = Math.round(seconds);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  if (hours > 0)
    return `${hours}:${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
  return `${minutes}:${String(secs).padStart(2, "0")}`;
}

/** Таймкод положения в исходнике — всегда мм:сс, чтобы столбцы совпадали. */
export function formatTime(seconds: number): string {
  const total = Math.max(0, seconds);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = Math.floor(total % 60);
  const base = `${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
  return hours > 0 ? `${hours}:${base}` : base;
}

export function formatDate(iso: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}
