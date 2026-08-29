import { api } from "../api";
import { useAsync } from "../hooks/useLiveState";
import type { Config } from "../types";
import { ConfigEditor } from "./ConfigEditor";

const SECTION_LABELS: Record<string, string> = {
  ingest: "Загрузка",
  transcribe: "Транскрипция",
  analyze: "Поиск фрагментов",
  cut: "Нарезка",
  edit: "Монтаж",
  publish: "Публикация",
};

/**
 * Переопределения хранятся как разница с пресетом, поэтому изначально это
 * пустой объект — редактировать нечего. Раздел добавляется целиком, со
 * значениями из пресета: дальше правится только то, что нужно.
 */
export function OverrideEditor({
  value,
  onChange,
}: {
  value: Config;
  onChange: (next: Config) => void;
}) {
  const schema = useAsync<Config>(() => api.presets.schema(), []);
  const sections = Object.keys(schema.data ?? {});
  const missing = sections.filter((section) => !(section in value));

  return (
    <div>
      {Object.keys(value).length === 0 && (
        <p className="muted" style={{ fontSize: 12.5 }}>
          Ничего не переопределено — всё берётся из пресета.
        </p>
      )}

      <ConfigEditor config={value} onChange={onChange} />

      {missing.length > 0 && (
        <div className="row" style={{ marginTop: 10 }}>
          <span className="muted" style={{ fontSize: 12.5 }}>
            Добавить раздел:
          </span>
          {missing.map((section) => (
            <button
              key={section}
              className="small"
              onClick={() =>
                onChange({ ...value, [section]: structuredClone(schema.data![section]) })
              }
            >
              {SECTION_LABELS[section] ?? section}
            </button>
          ))}
        </div>
      )}

      {Object.keys(value).length > 0 && (
        <div className="row" style={{ marginTop: 10 }}>
          <span className="muted" style={{ fontSize: 12.5 }}>
            Убрать раздел:
          </span>
          {Object.keys(value).map((section) => (
            <button
              key={section}
              className="small danger"
              onClick={() => {
                const next = { ...value };
                delete next[section];
                onChange(next);
              }}
            >
              {SECTION_LABELS[section] ?? section}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
