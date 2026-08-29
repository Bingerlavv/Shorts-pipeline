import { useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "../api";
import { DeleteProjectDialog } from "../components/DeleteProjectDialog";
import { dropPath, setDeep } from "../components/ConfigEditor";
import { OverrideEditor } from "../components/OverrideEditor";
import { SchedulePicker } from "../components/SchedulePicker";
import { PublishTargets } from "../components/PublishTargets";
import { ReelMap } from "../components/ReelMap";
import SegmentCard from "../components/SegmentCard";
import { StageTracker } from "../components/StageTracker";
import {
  Banner,
  Empty,
  Modal,
  Progress,
  Readout,
  StatusPill,
  formatDuration,
  jobLabel,
  statusLabel,
} from "../components/ui";
import { useAsync } from "../hooks/useLiveState";
import type { Account, LiveState, Preset, Project, Segment, SegmentStatus } from "../types";

const FILTERS: { key: SegmentStatus | "all"; label: string }[] = [
  { key: "all", label: "Все" },
  { key: "candidate", label: "Кандидаты" },
  { key: "approved", label: "В работе" },
  { key: "rendered", label: "Смонтированные" },
  { key: "published", label: "Опубликованные" },
  { key: "rejected", label: "Отклонённые" },
];

const SUBTITLE_CHOICES: { value: boolean | null; label: string }[] = [
  { value: true, label: "Есть" },
  { value: false, label: "Нет" },
  { value: null, label: "Не знаю" },
];

export default function ProjectPage({ live }: { live: LiveState }) {
  const { id } = useParams();
  const navigate = useNavigate();
  const projectId = Number(id);

  const project = useAsync<Project>(() => api.projects.get(projectId), [projectId]);
  const segments = useAsync<Segment[]>(() => api.projects.segments(projectId), [projectId]);
  const accounts = useAsync<Account[]>(() => api.accounts.list(), []);

  const [filter, setFilter] = useState<SegmentStatus | "all">("all");
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [overrides, setOverrides] = useState<Record<string, any>>({});
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);

  const liveProject = live.projects.find((item) => item.id === projectId);
  const liveSegments = useMemo(
    () => new Map(live.segments.map((segment) => [segment.id, segment])),
    [live.segments],
  );
  const activeJob = live.jobs.find(
    (job) => job.project_id === projectId && job.status === "running",
  );

  // Список фрагментов перезагружаем, когда сервер сообщил о смене статуса.
  const liveSignature = live.segments
    .filter((segment) => segment.project_id === projectId)
    .map((segment) => `${segment.id}:${segment.status}:${segment.has_render}`)
    .join("|");
  const [lastSignature, setLastSignature] = useState(liveSignature);
  if (liveSignature !== lastSignature) {
    setLastSignature(liveSignature);
    segments.reload();
    project.reload();
  }

  const data = project.data;

  const act = async (action: () => Promise<unknown>) => {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await action();
      project.reload();
      segments.reload();
    } catch (exc) {
      setError((exc as Error).message);
    } finally {
      setBusy(false);
    }
  };

  if (project.error) return <Banner tone="err">{project.error}</Banner>;
  if (!data) return <Empty>Открываю проект…</Empty>;

  const status = liveProject?.status ?? data.status;
  const statusOf = (segment: Segment) =>
    liveSegments.get(segment.id)?.status ?? segment.status;

  const all = segments.data ?? [];
  const visible = all.filter((segment) => filter === "all" || statusOf(segment) === filter);
  const counts = all.reduce<Record<string, number>>((acc, segment) => {
    const key = statusOf(segment);
    acc[key] = (acc[key] ?? 0) + 1;
    return acc;
  }, {});

  const scrollToSegment = (segmentId: number) => {
    setFilter("all");
    requestAnimationFrame(() =>
      document
        .getElementById(`segment-${segmentId}`)
        ?.scrollIntoView({ behavior: "smooth", block: "center" }),
    );
  };

  return (
    <>
      <div className="page-head">
        <div style={{ minWidth: 0 }}>
          <Link to="/projects" className="label" style={{ borderBottom: "none" }}>
            ← Все проекты
          </Link>
          <h1>{data.title || "Без названия"}</h1>
          <p className="truncate">
            {/* Локальный путь ссылкой быть не может: браузер такой переход не
                делает, а вид кликабельного текста обещает обратное. */}
            {data.source_kind === "file" ? (
              <span className="mono">{data.source_url}</span>
            ) : (
              <a href={data.source_url} target="_blank" rel="noreferrer">
                {data.source_url}
              </a>
            )}
          </p>
        </div>
        <div className="row">
          <StatusPill status={status} />
          <button onClick={() => setSettingsOpen(true)}>Настройки проекта</button>
          <button className="danger" onClick={() => setDeleteOpen(true)}>
            Удалить проект
          </button>
        </div>
      </div>

      {(liveProject?.error || data.error) && (
        <Banner tone="err">{liveProject?.error || data.error}</Banner>
      )}
      {error && <Banner tone="err">{error}</Banner>}
      {notice && <Banner tone="info">{notice}</Banner>}

      {all.length > 0 && data.duration > 0 && (
        <div className="card" style={{ marginBottom: 16 }}>
          <ReelMap
            duration={data.duration}
            segments={all}
            statusOf={statusOf}
            onPick={scrollToSegment}
          />
        </div>
      )}

      <div className="grid cols-3" style={{ marginBottom: 16 }}>
        <Readout
          label="Исходник"
          value={formatDuration(data.duration)}
          note={
            data.width
              ? `${data.width}×${data.height} · ${data.fps.toFixed(0)} кадр/с`
              : "ещё не загружен"
          }
        />
        <Readout
          label="Текст в кадре"
          value={
            data.has_burned_subtitles === null
              ? "Не определено"
              : data.has_burned_subtitles
                ? "Есть"
                : "Нет"
          }
          note="От этого зависит зеркалирование"
        >
          <div className="row" style={{ marginTop: 9 }}>
            {SUBTITLE_CHOICES.map((choice) => (
              <button
                key={String(choice.value)}
                className="small ghost"
                disabled={busy || data.has_burned_subtitles === choice.value}
                onClick={() =>
                  act(() =>
                    api.projects.update(projectId, { has_burned_subtitles: choice.value }),
                  )
                }
              >
                {choice.label}
              </button>
            ))}
          </div>
        </Readout>
        <Readout
          label="Фрагменты"
          value={all.length}
          note={
            Object.entries(counts)
              .map(([key, count]) => `${statusLabel(key)} ${count}`)
              .join(" · ") || "пока пусто"
          }
        />
      </div>

      <div className="card" style={{ marginBottom: 20 }}>
        <StageTracker
          project={data}
          segments={all}
          activeJob={activeJob}
          busy={busy}
          onRun={(stage) => act(() => api.projects.runStage(projectId, stage))}
          onRenderApproved={() => act(() => api.projects.renderApproved(projectId))}
          onPublishAll={() =>
            act(async () => {
              const result = await api.projects.publishAll(projectId);
              setNotice(`${result.note}. Аккаунты: ${result.accounts.join(", ")}`);
            })
          }
        />
        <div className="row between" style={{ marginTop: 10 }}>
          <span />
          <span className="muted" style={{ fontSize: 12.5 }}>
            {liveProject?.stage_message || data.stage_message}
          </span>
        </div>
        {activeJob && (
          <div style={{ marginTop: 14 }}>
            <div className="row between" style={{ fontSize: 12, marginBottom: 5 }}>
              <span>{jobLabel(activeJob.type)}</span>
              <span className="muted">{activeJob.message}</span>
            </div>
            <Progress value={activeJob.progress} />
          </div>
        )}
      </div>

      <div className="card" style={{ marginBottom: 14 }}>
        <PublishTargets
          accounts={accounts.data ?? []}
          value={data.account_ids}
          onChange={(next) =>
            act(async () => {
              await api.projects.setAccounts(projectId, next);
              project.reload();
            })
          }
        />
      </div>

      <div className="row" style={{ marginBottom: 14 }}>
        {FILTERS.map((item) => (
          <button
            key={item.key}
            className={`small chip${filter === item.key ? " on" : ""}`}
            onClick={() => setFilter(item.key)}
          >
            {item.label}
            {item.key !== "all" && counts[item.key] ? ` ${counts[item.key]}` : ""}
          </button>
        ))}
      </div>

      {segments.error && <Banner tone="err">{segments.error}</Banner>}

      {visible.length === 0 ? (
        <Empty>
          {all.length === 0
            ? "Фрагментов пока нет. Запусти поиск, когда расшифровка будет готова."
            : "В этом фильтре пусто. Выбери другой."}
        </Empty>
      ) : (
        <div className="grid" style={{ gap: 12 }}>
          {visible.map((segment) => (
            <SegmentCard
              key={segment.id}
              segment={segment}
              accounts={accounts.data ?? []}
              linkedAccountIds={data.account_ids}
              sourceDuration={data.duration}
              liveStatus={liveSegments.get(segment.id)?.status}
              progress={
                live.jobs.find(
                  (job) => job.segment_id === segment.id && job.status === "running",
                )?.progress
              }
              onChanged={() => {
                segments.reload();
                project.reload();
              }}
            />
          ))}
        </div>
      )}

      {deleteOpen && (
        <DeleteProjectDialog
          project={{ ...data, segment_count: all.length }}
          onClose={() => setDeleteOpen(false)}
          onDeleted={() => navigate("/projects", { replace: true })}
        />
      )}

      {settingsOpen && (
        <ProjectSettings
          project={data}
          onClose={() => setSettingsOpen(false)}
          onSaved={() => {
            setSettingsOpen(false);
            project.reload();
          }}
          overrides={overrides}
          setOverrides={setOverrides}
        />
      )}
    </>
  );
}

function ProjectSettings({
  project,
  onClose,
  onSaved,
  overrides,
  setOverrides,
}: {
  project: Project;
  onClose: () => void;
  onSaved: () => void;
  overrides: Record<string, any>;
  setOverrides: (next: Record<string, any>) => void;
}) {
  const presets = useAsync(() => api.presets.list(), []);
  const [presetId, setPresetId] = useState(project.preset_id ? String(project.preset_id) : "");
  const [autoPublish, setAutoPublish] = useState(project.auto_publish);
  const [title, setTitle] = useState(project.title);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [initialised, setInitialised] = useState(false);

  const ownSchedule = Boolean(overrides.publish?.schedule);
  // Что действует сейчас: своё, иначе пресетное, иначе пусто. Нужно, чтобы
  // включение своей сетки начиналось с текущих значений, а не с чистого листа.
  const effectiveSchedule =
    overrides.publish?.schedule ??
    (presets.data ?? []).find((item: Preset) => String(item.id) === presetId)?.config?.publish
      ?.schedule ??
    {};

  if (!initialised) {
    setInitialised(true);
    setOverrides(project.config_overrides ?? {});
  }

  const save = async () => {
    setBusy(true);
    setError("");
    try {
      await api.projects.update(project.id, {
        title,
        preset_id: presetId ? Number(presetId) : null,
        auto_publish: autoPublish,
        config_overrides: overrides,
      });
      onSaved();
    } catch (exc) {
      setError((exc as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal title="Настройки проекта" onClose={onClose}>
      <div className="field">
        <label>Название</label>
        <input value={title} onChange={(event) => setTitle(event.target.value)} />
      </div>
      <div className="field">
        <label>Пресет</label>
        <select value={presetId} onChange={(event) => setPresetId(event.target.value)}>
          <option value="">По умолчанию</option>
          {(presets.data ?? []).map((preset) => (
            <option key={preset.id} value={preset.id}>
              {preset.name}
            </option>
          ))}
        </select>
      </div>
      <label className="check">
        <input
          type="checkbox"
          checked={autoPublish}
          onChange={(event) => setAutoPublish(event.target.checked)}
        />
        Автопрогон: монтировать и публиковать без ревью
      </label>

      <h3 style={{ marginTop: 20, marginBottom: 4 }}>Расписание выхода</h3>
      <p className="muted" style={{ fontSize: 12.5, marginTop: 0 }}>
        Своё расписание для этого проекта. Выключено — берётся из пресета.
      </p>
      <label className="check" style={{ marginBottom: 10 }}>
        <input
          type="checkbox"
          checked={ownSchedule}
          onChange={(event) => {
            if (event.target.checked) {
              // Отталкиваемся от того, что уже действует, а не от пустоты:
              // иначе включение галочки молча сбрасывало бы настройки пресета.
              setOverrides(setDeep(overrides, ["publish", "schedule"], effectiveSchedule));
            } else {
              setOverrides(dropPath(overrides, ["publish", "schedule"]));
            }
          }}
        />
        Задать расписание отдельно для этого проекта
      </label>
      {ownSchedule && (
        <SchedulePicker
          value={overrides.publish?.schedule ?? {}}
          onChange={(next) => setOverrides(setDeep(overrides, ["publish", "schedule"], next))}
        />
      )}

      <h3 style={{ marginTop: 20, marginBottom: 4 }}>Отличия от пресета</h3>
      <p className="muted" style={{ fontSize: 12.5, marginTop: 0 }}>
        Меняй только то, что должно отличаться у этого видео.
      </p>
      <OverrideEditor value={overrides} onChange={setOverrides} />

      {error && <Banner tone="err">{error}</Banner>}
      <div className="row" style={{ marginTop: 16 }}>
        <button className="primary" disabled={busy} onClick={save}>
          Сохранить
        </button>
        <button className="ghost" onClick={onClose}>
          Отмена
        </button>
      </div>
    </Modal>
  );
}
