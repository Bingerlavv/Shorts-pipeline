import { useState } from "react";
import { api } from "../api";
import type { Account, Segment } from "../types";
import { OverrideEditor } from "./OverrideEditor";
import {
  Modal,
  Progress,
  ScoreMeter,
  SourceStrip,
  StatusPill,
  formatDate,
  formatTime,
} from "./ui";

/** ISO с сервера -> значение для input[type=datetime-local] в местном времени. */
function toLocalInput(iso: string | null): string {
  if (!iso) return "";
  const moment = new Date(iso);
  const shifted = new Date(moment.getTime() - moment.getTimezoneOffset() * 60000);
  return shifted.toISOString().slice(0, 16);
}

const MARKED = new Set(["approved", "rendering", "rendered", "publishing", "published"]);

export default function SegmentCard({
  segment,
  accounts,
  linkedAccountIds,
  sourceDuration,
  liveStatus,
  progress,
  onChanged,
}: {
  segment: Segment;
  accounts: Account[];
  /** Аккаунты, привязанные к проекту: в них ролик уходит одной кнопкой. */
  linkedAccountIds: number[];
  sourceDuration: number;
  liveStatus?: string;
  progress?: number;
  onChanged: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [overridesOpen, setOverridesOpen] = useState(false);
  const [preview, setPreview] = useState(false);
  const [timeOpen, setTimeOpen] = useState(false);
  // datetime-local работает в местном времени и без зоны, поэтому храним строку
  // в его формате, а на сервер отдаём ISO.
  const [publishAt, setPublishAt] = useState(() => toLocalInput(segment.publish_at));
  const [title, setTitle] = useState(segment.title_de);
  const [description, setDescription] = useState(segment.description_de);
  const [hashtags, setHashtags] = useState((segment.hashtags ?? []).join(" "));
  const [overrides, setOverrides] = useState(segment.edit_overrides ?? {});
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const linkedIds = new Set(linkedAccountIds);
  const linked = accounts.filter(
    (account) => account.is_active && linkedIds.has(account.id),
  );

  const status = liveStatus ?? segment.status;
  const ranges = segment.source_ranges?.length
    ? segment.source_ranges
    : [[segment.start, segment.end]];
  const duration = ranges.reduce((sum, [start, end]) => sum + (end - start), 0);

  const act = async (action: () => Promise<unknown>) => {
    setBusy(true);
    setError("");
    try {
      await action();
      onChanged();
    } catch (exc) {
      setError((exc as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const save = () =>
    act(async () => {
      await api.segments.update(segment.id, {
        title_de: title,
        description_de: description,
        hashtags: hashtags.split(/[\s,]+/).filter(Boolean),
      });
      setEditing(false);
    });

  const saveOverrides = () =>
    act(async () => {
      await api.segments.update(segment.id, { edit_overrides: overrides });
      setOverridesOpen(false);
    });

  const publish = (accountId: number) =>
    act(() =>
      api.publications.create({
        segment_id: segment.id,
        account_id: accountId,
        privacy: "private",
        start_now: true,
      }),
    );

  // Основной путь: ролик уходит во все аккаунты, привязанные к проекту. Кнопки
  // по аккаунтам остаются для разовых отправок «вот этот — только сюда».
  const publishLinked = () => act(() => api.publications.publishSegment(segment.id));

  return (
    <article className={`segment is-${status}`} id={`segment-${segment.id}`}>
      <div
        className={`frame${segment.thumb_path ? "" : " blank"}${
          segment.render_path ? " playable" : ""
        }`}
        onClick={segment.render_path ? () => setPreview(true) : undefined}
        role={segment.render_path ? "button" : undefined}
        tabIndex={segment.render_path ? 0 : undefined}
        onKeyDown={(event) => {
          if (segment.render_path && (event.key === "Enter" || event.key === " ")) {
            event.preventDefault();
            setPreview(true);
          }
        }}
        aria-label={segment.render_path ? "Посмотреть готовый ролик" : undefined}
      >
        {segment.thumb_path ? (
          <>
            <img src={api.segments.thumbUrl(segment.id, segment.updated_at)} alt="" loading="lazy" />
            <div className="slate">
              <span>{formatTime(segment.start)}</span>
              <span>{duration.toFixed(0)}с</span>
            </div>
            {segment.render_path && <span className="play" aria-hidden="true" />}
          </>
        ) : (
          <span>кадра нет — не смонтирован</span>
        )}
        {MARKED.has(status) && <div className="mark" aria-hidden="true" />}
      </div>

      <div style={{ minWidth: 0 }}>
        <div className="row between" style={{ alignItems: "flex-start", gap: 12 }}>
          <h3 style={{ flex: 1 }}>{segment.title_de || "Без заголовка"}</h3>
          <StatusPill status={status} />
        </div>

        <div className="meta">
          <span>
            {formatTime(segment.start)} → {formatTime(segment.end)}
          </span>
          <span>{duration.toFixed(1)} с</span>
          <ScoreMeter value={segment.score} />
          {ranges.length > 1 && <span className="splice">склейка ×{ranges.length}</span>}
        </div>

        <div className="seg-strip">
          <span className="label">Место в исходнике</span>
          <SourceStrip ranges={ranges} duration={sourceDuration} quiet={status === "rejected"} />
        </div>

        {progress !== undefined && (status === "rendering" || status === "publishing") && (
          <div style={{ margin: "10px 0" }}>
            <Progress value={progress} />
          </div>
        )}

        {segment.hook && <blockquote className="quote">«{segment.hook}»</blockquote>}
        {segment.reason && (
          <p className="muted" style={{ fontSize: 12.5, margin: "7px 0 0" }}>
            {segment.reason}
          </p>
        )}
        {segment.hashtags?.length > 0 && <p className="tags">{segment.hashtags.join("  ")}</p>}

        {(segment.error || error) && (
          <p style={{ color: "var(--rust)", fontSize: 12.5, margin: "0 0 9px" }}>
            {segment.error || error}
          </p>
        )}

        <div className="row">
          {segment.status !== "approved" && (
            <button
              className="small primary"
              disabled={busy}
              onClick={() => act(() => api.segments.approve(segment.id))}
            >
              В работу
            </button>
          )}
          {segment.status !== "rejected" && (
            <button
              className="small ghost"
              disabled={busy}
              onClick={() => act(() => api.segments.reject(segment.id))}
            >
              Отклонить
            </button>
          )}
          <button
            className="small"
            disabled={busy || status === "rendering"}
            onClick={() => act(() => api.segments.render(segment.id))}
          >
            {segment.render_path ? "Пересобрать" : "Смонтировать"}
          </button>
          <button className="small ghost" onClick={() => setEditing(true)}>
            Тексты
          </button>
          <button
            className="small ghost"
            disabled={busy || !segment.transcript_text}
            title={
              segment.transcript_text
                ? "Переписать заголовок, описание и хэштеги нейросетью"
                : "У фрагмента нет расшифровки"
            }
            onClick={() => act(() => api.segments.caption(segment.id))}
          >
            Написать заново
          </button>
          <button className="small ghost" onClick={() => setOverridesOpen(true)}>
            Монтаж
          </button>
          <button
            className={`small ghost${segment.publish_at ? " on" : ""}`}
            onClick={() => setTimeOpen(true)}
            title={
              segment.publish_at
                ? `Выйдет ${formatDate(segment.publish_at)}`
                : "Задать точное время выхода вместо расписания"
            }
          >
            {segment.publish_at ? `⏰ ${formatDate(segment.publish_at)}` : "Время"}
          </button>
          {segment.render_path && (
            <a className="btn" href={api.segments.renderUrl(segment.id, segment.updated_at)} download>
              Скачать
            </a>
          )}
          {segment.render_path && linked.length > 0 && (
            <button
              className="small primary"
              disabled={busy}
              onClick={publishLinked}
              title={`Отправить в аккаунты проекта: ${linked
                .map((account) => account.name)
                .join(", ")}`}
            >
              Опубликовать ({linked.length})
            </button>
          )}
          {segment.render_path &&
            accounts
              .filter((account) => account.is_active)
              .map((account) => (
                <button
                  key={account.id}
                  className="small"
                  disabled={busy}
                  onClick={() => publish(account.id)}
                  title={
                    linkedIds.has(account.id)
                      ? `${account.name}: привязан к проекту, уйдёт и общей кнопкой`
                      : `Разовая отправка в ${account.name}`
                  }
                >
                  {linkedIds.has(account.id) ? "★" : "→"} {account.name}
                </button>
              ))}
          <span className="spacer" />
          <button
            className="small danger"
            disabled={busy}
            onClick={() => {
              if (confirm("Удалить фрагмент? Смонтированный файл останется на диске.")) {
                act(() => api.segments.remove(segment.id));
              }
            }}
          >
            Удалить
          </button>
        </div>

      </div>

      {preview && (
        <Modal title={segment.title_de || "Готовый ролик"} onClose={() => setPreview(false)}>
          <video
            src={api.segments.renderUrl(segment.id, segment.updated_at)}
            controls
            autoPlay
            poster={segment.thumb_path ? api.segments.thumbUrl(segment.id, segment.updated_at) : undefined}
            className="preview-video"
          />
        </Modal>
      )}

      {timeOpen && (
        <Modal title="Когда выпустить" onClose={() => setTimeOpen(false)}>
          <div className="field">
            <label>Точное время выхода</label>
            <input
              type="datetime-local"
              value={publishAt}
              onChange={(event) => setPublishAt(event.target.value)}
            />
            <div className="config-hint">
              Пусто — время посчитает расписание проекта: ближайший свободный час
              с учётом того, что уже стоит в очереди на аккаунт. Заданное время
              расписание не двигает, ролик уйдёт ровно в него.
            </div>
          </div>
          <div className="row" style={{ marginTop: 12 }}>
            <button
              className="primary"
              disabled={busy || !publishAt}
              onClick={() =>
                act(async () => {
                  await api.segments.update(segment.id, {
                    publish_at: new Date(publishAt).toISOString(),
                  });
                  setTimeOpen(false);
                })
              }
            >
              Сохранить
            </button>
            <button
              disabled={busy || !segment.publish_at}
              onClick={() =>
                act(async () => {
                  await api.segments.update(segment.id, { clear_publish_at: true });
                  setPublishAt("");
                  setTimeOpen(false);
                })
              }
            >
              Вернуть к расписанию
            </button>
            <button onClick={() => setTimeOpen(false)}>Отмена</button>
          </div>
        </Modal>
      )}

      {editing && (
        <Modal title="Тексты фрагмента" onClose={() => setEditing(false)}>
          <div className="field">
            <label>Заголовок в кадре</label>
            <input value={title} onChange={(event) => setTitle(event.target.value)} />
          </div>
          <div className="field">
            <label>Описание</label>
            <textarea
              value={description}
              onChange={(event) => setDescription(event.target.value)}
            />
          </div>
          <div className="field">
            <label>Хэштеги через пробел</label>
            <input value={hashtags} onChange={(event) => setHashtags(event.target.value)} />
          </div>
          {segment.transcript_text && (
            <details>
              <summary className="label" style={{ cursor: "pointer", marginBottom: 8 }}>
                Расшифровка фрагмента
              </summary>
              <p className="muted" style={{ fontSize: 13 }}>
                {segment.transcript_text}
              </p>
            </details>
          )}
          {error && <p style={{ color: "var(--rust)" }}>{error}</p>}
          <div className="row" style={{ marginTop: 14 }}>
            <button className="primary" disabled={busy} onClick={save}>
              Сохранить
            </button>
            <button className="ghost" onClick={() => setEditing(false)}>
              Отмена
            </button>
          </div>
        </Modal>
      )}

      {overridesOpen && (
        <Modal title="Монтаж этого фрагмента" onClose={() => setOverridesOpen(false)}>
          <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>
            Здесь только отличия от пресета проекта. Пусто — всё берётся из пресета.
          </p>
          <OverrideEditor value={overrides} onChange={setOverrides} />
          <details style={{ marginTop: 12 }}>
            <summary className="label" style={{ cursor: "pointer", marginBottom: 8 }}>
              Показать как JSON
            </summary>
            <textarea
              className="mono"
              style={{ minHeight: 170 }}
              value={JSON.stringify(overrides, null, 2)}
              onChange={(event) => {
                try {
                  setOverrides(JSON.parse(event.target.value));
                  setError("");
                } catch {
                  setError("JSON пока не разбирается — правка не применена");
                }
              }}
            />
          </details>
          {error && <p style={{ color: "var(--rust)" }}>{error}</p>}
          <div className="row" style={{ marginTop: 14 }}>
            <button className="primary" disabled={busy} onClick={saveOverrides}>
              Сохранить
            </button>
            <button className="ghost" onClick={() => setOverrides({})}>
              Очистить
            </button>
            <button className="ghost" onClick={() => setOverridesOpen(false)}>
              Отмена
            </button>
          </div>
        </Modal>
      )}
    </article>
  );
}
