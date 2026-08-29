import { useState } from "react";
import { api } from "../api";
import { ConfigEditor } from "../components/ConfigEditor";
import { Banner, Empty, Modal } from "../components/ui";
import { useAsync } from "../hooks/useLiveState";
import type { Config, Preset } from "../types";

export default function PresetsPage() {
  const presets = useAsync<Preset[]>(() => api.presets.list(), []);
  const schema = useAsync<Config>(() => api.presets.schema(), []);
  const [editing, setEditing] = useState<Preset | "new" | null>(null);
  const [error, setError] = useState("");

  const remove = async (preset: Preset) => {
    if (!confirm(`Удалить пресет «${preset.name}»?`)) return;
    try {
      await api.presets.remove(preset.id);
      presets.reload();
    } catch (exc) {
      setError((exc as Error).message);
    }
  };

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Пресеты</h1>
          <p>
            Пресет задаёт поведение всех модулей: как искать моменты, как их резать и
            монтировать, куда публиковать. Проект и отдельный фрагмент могут
            переопределить любую ветку.
          </p>
        </div>
        <button
          className="primary"
          disabled={!schema.data}
          onClick={() => setEditing("new")}
        >
          Новый пресет
        </button>
      </div>

      {error && <Banner tone="err">{error}</Banner>}
      {presets.error && <Banner tone="err">{presets.error}</Banner>}

      {(presets.data ?? []).length === 0 ? (
        <Empty>Пресетов нет. Базовый создаётся автоматически при первом запуске сервера.</Empty>
      ) : (
        <div className="grid cols-2">
          {(presets.data ?? []).map((preset) => (
            <div className="card" key={preset.id}>
              <div className="row between">
                <strong>{preset.name}</strong>
                {preset.is_default && <span className="pill info">основной</span>}
              </div>
              <p className="muted" style={{ fontSize: 12.5 }}>
                {preset.description || "без описания"}
              </p>
              <div className="row">
                <button className="small" onClick={() => setEditing(preset)}>
                  Изменить
                </button>
                <button
              className="small ghost"
              onClick={async () => {
                await api.presets.clone(preset.id);
                presets.reload();
              }}
              title="Копия со всеми настройками — удобно, чтобы поменять только аккаунты"
            >
              Дублировать
            </button>
            <button className="small danger" onClick={() => remove(preset)}>
                  Удалить
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      {editing && schema.data && (
        <PresetForm
          preset={editing === "new" ? null : editing}
          schema={schema.data}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            presets.reload();
          }}
        />
      )}
    </>
  );
}

/**
 * Достраивает конфиг умолчаниями: значения пресета важнее, но отсутствующие
 * ветки берутся из схемы. Списки не сливаем поэлементно — выбранные аккаунты
 * или фоны должны остаться ровно теми, что сохранил пользователь.
 */
function withDefaults(schema: unknown, saved: unknown): any {
  if (saved === undefined) return structuredClone(schema);
  const plain = (v: unknown) =>
    typeof v === "object" && v !== null && !Array.isArray(v);
  if (!plain(schema) || !plain(saved)) return structuredClone(saved);

  const out: Record<string, unknown> = {};
  const base = schema as Record<string, unknown>;
  const over = saved as Record<string, unknown>;
  for (const key of new Set([...Object.keys(base), ...Object.keys(over)])) {
    out[key] = key in base ? withDefaults(base[key], over[key]) : structuredClone(over[key]);
  }
  return out;
}

function PresetForm({
  preset,
  schema,
  onClose,
  onSaved,
}: {
  preset: Preset | null;
  schema: Config;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [name, setName] = useState(preset?.name ?? "");
  const [description, setDescription] = useState(preset?.description ?? "");
  const [isDefault, setIsDefault] = useState(preset?.is_default ?? false);
  const [config, setConfig] = useState<Config>(
    // Умолчания подмешиваются под сохранённый конфиг. Иначе в пресете, созданном
    // до появления новой настройки, её поля просто не отрисуются: форма строится
    // по структуре объекта. Конвейер такие ключи и так берёт из умолчаний, но в
    // редакторе их не было видно, и настройка казалась несуществующей.
    withDefaults(schema, preset?.config),
  );
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const save = async () => {
    setBusy(true);
    setError("");
    try {
      const body = { name, description, is_default: isDefault, config };
      if (preset) await api.presets.update(preset.id, body);
      else await api.presets.create(body);
      onSaved();
    } catch (exc) {
      setError((exc as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal title={preset ? `Пресет «${preset.name}»` : "Новый пресет"} onClose={onClose}>
      <div className="field">
        <label>Название</label>
        <input value={name} onChange={(event) => setName(event.target.value)} />
      </div>
      <div className="field">
        <label>Описание</label>
        <input value={description} onChange={(event) => setDescription(event.target.value)} />
      </div>
      <label
        style={{ display: "flex", gap: 8, alignItems: "center", color: "var(--text)", marginBottom: 14 }}
      >
        <input
          type="checkbox"
          checked={isDefault}
          onChange={(event) => setIsDefault(event.target.checked)}
        />
        Использовать по умолчанию для новых проектов
      </label>

      <ConfigEditor config={config} onChange={setConfig} />

      {error && <Banner tone="err">{error}</Banner>}
      <div className="row" style={{ marginTop: 14 }}>
        <button className="primary" disabled={busy || !name.trim()} onClick={save}>
          Сохранить
        </button>
        <button onClick={() => setConfig(structuredClone(schema))}>Сбросить к умолчанию</button>
        <button onClick={onClose}>Отмена</button>
      </div>
    </Modal>
  );
}
