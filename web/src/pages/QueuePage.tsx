import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import {
  Banner,
  Empty,
  Modal,
  Progress,
  StatusPill,
  formatDate,
  jobLabel,
} from "../components/ui";
import { useAsync } from "../hooks/useLiveState";
import type { Job, JobDetail, LiveState } from "../types";

export default function QueuePage({ live }: { live: LiveState }) {
  const jobs = useAsync<Job[]>(() => api.jobs.list(), []);
  const [detail, setDetail] = useState<JobDetail | null>(null);
  const [error, setError] = useState("");

  const liveById = new Map(live.jobs.map((job) => [job.id, job]));

  // Пока что-то выполняется, полный список подтягиваем при каждом изменении.
  const signature = live.jobs.map((job) => `${job.id}:${job.status}`).join("|");
  const [lastSignature, setLastSignature] = useState(signature);
  if (signature !== lastSignature) {
    setLastSignature(signature);
    jobs.reload();
  }

  const open = async (id: number) => {
    try {
      setDetail(await api.jobs.get(id));
    } catch (exc) {
      setError((exc as Error).message);
    }
  };

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Очередь</h1>
          <p>
            Загрузка, распознавание, монтаж и публикация выполняются воркером. Если
            очередь стоит — проверь, что запущен процесс{" "}
            <span className="mono">python -m app.queue.worker</span>.
          </p>
        </div>
        <div className="row">
          <button onClick={jobs.reload}>Обновить</button>
          <button
            onClick={async () => {
              await api.jobs.clearCompleted();
              jobs.reload();
            }}
          >
            Очистить завершённые
          </button>
          <button
            className="danger"
            title="Удаляет проваленные задачи и снимает ошибки, которые они оставили на проектах"
            onClick={async () => {
              const result = await api.jobs.clearFailed();
              setError(result.deleted ? "" : "Проваленных задач нет");
              jobs.reload();
            }}
          >
            Убрать ошибки
          </button>
        </div>
      </div>

      {error && <Banner tone="err">{error}</Banner>}
      {jobs.error && <Banner tone="err">{jobs.error}</Banner>}

      {(jobs.data ?? []).length === 0 ? (
        <Empty>Очередь пуста.</Empty>
      ) : (
        <div className="card table-wrap" style={{ padding: 0 }}>
          <table>
            <thead>
              <tr>
                <th style={{ width: 60 }}>ID</th>
                <th>Задача</th>
                <th style={{ width: 190 }}>Прогресс</th>
                <th>Статус</th>
                <th className="nowrap">Создана</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {(jobs.data ?? []).map((job) => {
                const liveInfo = liveById.get(job.id);
                const status = liveInfo?.status ?? job.status;
                const progress = liveInfo?.progress ?? job.progress;
                const message = liveInfo?.message ?? job.message;
                const jobError = liveInfo?.error ?? job.error;
                return (
                  <tr key={job.id}>
                    <td className="mono">{job.id}</td>
                    <td>
                      <span>{jobLabel(job.type)}</span>
                      {job.project_id && (
                        <>
                          <span className="muted"> · </span>
                          <Link to={`/projects/${job.project_id}`} style={{ fontSize: 12 }}>
                            проект #{job.project_id}
                          </Link>
                        </>
                      )}
                      <div className={`sub${jobError ? " err" : ""}`} title={jobError || message}>
                        {jobError || message}
                      </div>
                    </td>
                    <td>
                      <Progress value={progress} failed={status === "failed"} />
                      <div className="muted mono" style={{ fontSize: 11 }}>
                        {Math.round(progress * 100)}%
                        {job.attempts > 1 && ` · попытка ${job.attempts}/${job.max_attempts}`}
                      </div>
                    </td>
                    <td>
                      <StatusPill status={status} />
                    </td>
                    <td className="muted nowrap">{formatDate(job.created_at)}</td>
                    <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                      <button className="small" onClick={() => open(job.id)}>
                        Лог
                      </button>{" "}
                      {(status === "queued" || status === "running") && (
                        <button
                          className="small danger"
                          onClick={async () => {
                            await api.jobs.cancel(job.id);
                            jobs.reload();
                          }}
                        >
                          Отменить
                        </button>
                      )}
                      {status === "failed" && (
                        <button
                          className="small"
                          onClick={async () => {
                            await api.jobs.retry(job.id);
                            jobs.reload();
                          }}
                        >
                          Повторить
                        </button>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {detail && (
        <Modal title={`${jobLabel(detail.type)} · задача ${detail.id}`} onClose={() => setDetail(null)}>
          {detail.error && <Banner tone="err">{detail.error}</Banner>}
          <pre className="log">{detail.log || "лог пуст"}</pre>
        </Modal>
      )}
    </>
  );
}
