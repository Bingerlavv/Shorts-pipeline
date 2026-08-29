import type { Segment } from "../types";
import { formatTime } from "./ui";

/**
 * Карта катушки: весь исходник одной полосой, а на ней — всё, что из него
 * взято, цветом состояния. Видно покрытие, пробелы и скопления сразу,
 * без прокрутки списка карточек.
 */
export function ReelMap({
  duration,
  segments,
  statusOf,
  onPick,
}: {
  duration: number;
  segments: Segment[];
  statusOf: (segment: Segment) => string;
  onPick: (id: number) => void;
}) {
  if (!duration || duration <= 0) return null;

  const ticks = 5;
  const covered = segments.reduce(
    (sum, segment) =>
      sum +
      (segment.source_ranges?.length
        ? segment.source_ranges.reduce((inner, [a, b]) => inner + (b - a), 0)
        : segment.end - segment.start),
    0,
  );

  return (
    <div>
      <div className="row between" style={{ marginBottom: 7 }}>
        <span className="label">Катушка исходника</span>
        <span className="mono muted" style={{ fontSize: 11.5 }}>
          взято {((covered / duration) * 100).toFixed(1)}% · {formatTime(duration)}
        </span>
      </div>

      <div className="reel">
        {segments.flatMap((segment) => {
          const ranges = segment.source_ranges?.length
            ? segment.source_ranges
            : [[segment.start, segment.end]];
          const status = statusOf(segment);
          return ranges.map(([start, end], index) => (
            <button
              key={`${segment.id}-${index}`}
              className={`reel-mark s-${status}`}
              style={{
                left: `${(start / duration) * 100}%`,
                width: `${Math.max(0.3, ((end - start) / duration) * 100)}%`,
              }}
              onClick={() => onPick(segment.id)}
              title={`${formatTime(start)} — ${segment.title_de || "без заголовка"}`}
              aria-label={`Перейти к фрагменту на ${formatTime(start)}`}
            />
          ));
        })}
      </div>

      <div className="reel-ticks" aria-hidden="true">
        {Array.from({ length: ticks + 1 }, (_, i) => (
          <span key={i}>{formatTime((duration / ticks) * i)}</span>
        ))}
      </div>
    </div>
  );
}
