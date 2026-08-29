import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { DeleteProjectDialog } from "../components/DeleteProjectDialog";
import { Banner, Empty, StatusPill, formatDate, formatDuration } from "../components/ui";
import { useAsync } from "../hooks/useLiveState";
import type { LiveState, Preset, Project } from "../types";

export default function ProjectsPage({ live }: { live: LiveState }) {
  const projects = useAsync<Project[]>(() => api.projects.list(), []);
  const presets = useAsync<Preset[]>(() => api.presets.list(), []);

  const [url, setUrl] = useState("");
  const [presetId, setPresetId] = useState<string>("");
  const [autoPublish, setAutoPublish] = useState(false);
  // Плоская нарезка задаётся здесь, а не только в пресете: режим выбирают под
  // конкретное видео («вот это просто порежь по минуте»), а не раз и навсегда.
  const [flatCut, setFlatCut] = useState(false);
  const [chunkSeconds, setChunkSeconds] = useState("60");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [toDelete, setToDelete] = useState<Project | null>(null);

  const liveById = new Map(live.projects.map((project) => [project.id, project]));

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const seconds = Number(chunkSeconds);
      await api.projects.create({
        source_url: url.trim(),
        preset_id: presetId ? Number(presetId) : null,
        auto_publish: autoPublish,
        config_overrides: {
          // Режим пишем всегда, в том числе выключенным: иначе включённая в
          // пресете плоская нарезка молча применилась бы к видео, которое
          // добавляли ради поиска моментов.
          chunks: flatCut
            ? { enabled: true, duration: seconds > 0 ? seconds : 60 }
            : { enabled: false },
        },
        start_now: true,
      });
      setUrl("");
      projects.reload();
    } catch (exc) {
      setError((exc as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Проекты</h1>
          <p>
            Вставь ссылку на видео или путь к файлу на диске — сервер распознает
            речь, найдёт самостоятельные моменты и подготовит их к монтажу. Либо
            включи плоскую нарезку, и видео просто разойдётся на куски равной
            длины, без распознавания и без модели.
          </p>
        </div>
        <button onClick={projects.reload}>Обновить</button>
      </div>

      <form className="card" onSubmit={submit} style={{ marginBottom: 20 }}>
        <div className="row" style={{ alignItems: "flex-end" }}>
          <div style={{ flex: "2 1 340px" }}>
            <label>Ссылка или файл</label>
            <input
              value={url}
              onChange={(event) => setUrl(event.target.value)}
              placeholder="https://youtube.com/watch?v=… или D:\видео\эфир.mp4"
              required
            />
          </div>
          <div style={{ flex: "1 1 180px" }}>
            <label>Пресет</label>
            <select value={presetId} onChange={(event) => setPresetId(event.target.value)}>
              <option value="">По умолчанию</option>
              {(presets.data ?? []).map((preset) => (
                <option key={preset.id} value={preset.id}>
                  {preset.name}
                  {preset.is_default ? " (основной)" : ""}
                </option>
              ))}
            </select>
          </div>
          <label className="check" style={{ marginBottom: 7 }}>
            <input
              type="checkbox"
              checked={flatCut}
              onChange={(event) => setFlatCut(event.target.checked)}
            />
            Плоская нарезка
          </label>
          {flatCut && (
            <div style={{ flex: "0 1 120px" }}>
              <label>Кусок, с</label>
              <input
                type="number"
                min={5}
                step={5}
                value={chunkSeconds}
                onChange={(event) => setChunkSeconds(event.target.value)}
              />
            </div>
          )}
          <label className="check" style={{ marginBottom: 7 }}>
            <input
              type="checkbox"
              checked={autoPublish}
              onChange={(event) => setAutoPublish(event.target.checked)}
            />
            Автопрогон
          </label>
          <button className="primary" disabled={busy || !url.trim()} style={{ marginBottom: 1 }}>
            {busy ? "Добавляю…" : "Добавить"}
          </button>
        </div>
        {flatCut && (
          <div className="config-hint" style={{ marginTop: 8 }}>
            Куски режутся подряд, по границам времени, а не по смыслу: слово на
            стыке будет разрезано. Речь не распознаётся, поэтому субтитры и
            подрезка по паузам в этом режиме недоступны. Остальные настройки
            пресета — кадрирование, баннер, фон — работают как обычно.
          </div>
        )}
        {autoPublish && (
          <div className="config-hint" style={{ marginTop: 8 }}>
            Автопрогон смонтирует все найденные фрагменты без ревью и сразу отправит их
            в аккаунты, к которым привязан проект. Привязка задаётся в карточке проекта
            или на странице «Аккаунты» — здесь её выбирать не нужно.
          </div>
        )}
        {error && (
          <div style={{ marginTop: 12 }}>
            <Banner tone="err">{error}</Banner>
          </div>
        )}
      </form>

      {projects.error && <Banner tone="err">{projects.error}</Banner>}

      {projects.loading && !projects.data ? (
        <Empty>Загружаю…</Empty>
      ) : (projects.data ?? []).length === 0 ? (
        <Empty>Пока ни одного проекта. Добавь первую ссылку выше.</Empty>
      ) : (
        <div className="card table-wrap" style={{ padding: 0 }}>
          <table>
            <thead>
              <tr>
                <th>Название</th>
                <th>Статус</th>
                <th className="nowrap">Фрагменты</th>
                <th className="nowrap">Исходник</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {(projects.data ?? []).map((project) => {
                const liveInfo = liveById.get(project.id);
                const status = liveInfo?.status ?? project.status;
                const message = liveInfo?.stage_message ?? project.stage_message;
                const error = liveInfo?.error ?? project.error;
                return (
                  <tr key={project.id}>
                    <td>
                      <Link
                        to={`/projects/${project.id}`}
                        className="name"
                        title={project.title || project.source_url}
                      >
                        {project.title || project.source_url}
                      </Link>
                      <div className={`sub${error ? " err" : ""}`} title={error || message}>
                        {project.source_kind === "file" && !error && !message
                          ? `файл: ${project.source_url}`
                          : error || message || project.source_url}
                      </div>
                    </td>
                    <td>
                      <StatusPill status={status} />
                    </td>
                    <td className="mono">{project.segment_count}</td>
                    <td className="nowrap">
                      <span className="mono">{formatDuration(project.duration)}</span>
                      <div className="muted" style={{ fontSize: 11.5 }}>
                        {formatDate(project.created_at)}
                      </div>
                    </td>
                    <td style={{ textAlign: "right" }}>
                      <button
                        className="small danger"
                        onClick={() => setToDelete(project)}
                        title="Удалить проект"
                      >
                        Удалить
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {toDelete && (
        <DeleteProjectDialog
          project={toDelete}
          onClose={() => setToDelete(null)}
          onDeleted={() => {
            setToDelete(null);
            projects.reload();
          }}
        />
      )}
    </>
  );
}
