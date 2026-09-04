import { useEffect, useState } from "react";
import { ApiError, api } from "../api";
import { Banner, Empty, formatDate } from "../components/ui";
import { useAsync } from "../hooks/useLiveState";
import type { Worker } from "../types";

function gigabytes(bytes: number): string {
  if (!bytes) return "—";
  return `${(bytes / 2 ** 30).toFixed(0)} ГБ`;
}

function seenAgo(iso: string | null): string {
  if (!iso) return "не отмечался";
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 60) return "только что";
  if (seconds < 3600) return `${Math.round(seconds / 60)} мин назад`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)} ч назад`;
  return formatDate(iso);
}

/**
 * Парк воркеров: панель ничего не считает сама, она смотрит на реестр.
 *
 * Записи заводят сами воркеры при старте — здесь их можно только включить,
 * выключить (доделает текущее и остановится) или убрать отвалившийся.
 */
export default function WorkersPage() {
  const workers = useAsync<Worker[]>(() => api.workers.list(), []);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  // Воркеры отмечаются раз в 20 секунд — обновляем чаще, чтобы «офлайн»
  // не висел дольше, чем есть на самом деле.
  useEffect(() => {
    const timer = setInterval(() => workers.reload(), 10_000);
    return () => clearInterval(timer);
  }, []);

  const rows = workers.data ?? [];

  const toggle = async (worker: Worker) => {
    setError("");
    setNotice("");
    try {
      const next = await api.workers.toggle(worker.id);
      setNotice(
        next.is_enabled
          ? `«${next.name}» снова берёт задачи`
          : `«${next.name}» доделает текущее и остановится`,
      );
      workers.reload();
    } catch (exc) {
      setError((exc as Error).message);
    }
  };

  const remove = async (worker: Worker) => {
    if (!confirm(`Убрать воркер «${worker.name}» из реестра?`)) return;
    setError("");
    try {
      await api.workers.remove(worker.id);
    } catch (exc) {
      const failure = exc as ApiError;
      if (failure.status !== 409 || !confirm(`${failure.message}\n\nВсё равно удалить?`)) {
        if (failure.status !== 409) setError(failure.message);
        return;
      }
      await api.workers.remove(worker.id, true);
    }
    workers.reload();
  };

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Воркеры</h1>
          <p>
            Машины, которые качают исходники, монтируют и публикуют. Панель сама ничего
            не выполняет — она ставит задачи и смотрит, кто их взял. Файлы проекта
            остаются на том воркере, который его загрузил.
          </p>
        </div>
        <div className="row">
          <button onClick={workers.reload}>Обновить</button>
        </div>
      </div>

      {error && <Banner tone="err">{error}</Banner>}
      {notice && <Banner tone="info">{notice}</Banner>}
      {workers.error && <Banner tone="err">{workers.error}</Banner>}

      {rows.length === 0 ? (
        <Empty>
          Ни один воркер не отметился. Запусти на нужной машине{" "}
          <span className="mono">python -m app.queue.worker</span> из папки{" "}
          <span className="mono">server</span> — он зарегистрируется сам. Имя берётся из{" "}
          <span className="mono">SHORTS_WORKER_NAME</span> (по умолчанию имя хоста), а{" "}
          <span className="mono">SHORTS_WORKER_PUBLIC_URL</span> нужен, чтобы панель
          показывала его превью.
        </Empty>
      ) : (
        <div className="card" style={{ padding: 0 }}>
          <table>
            <thead>
              <tr>
                <th>Воркер</th>
                <th>Состояние</th>
                <th>Умеет</th>
                <th>Работа</th>
                <th>Держит</th>
                <th>Файлы</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((worker) => (
                <tr key={worker.id}>
                  <td>
                    {worker.name}
                    <div className="muted mono" style={{ fontSize: 11.5 }}>
                      {worker.hostname}
                      {worker.version ? ` · v${worker.version}` : ""}
                    </div>
                  </td>
                  <td className="nowrap">
                    {worker.online ? (
                      <span className="pill ok">на связи</span>
                    ) : (
                      <span className="pill err">офлайн</span>
                    )}
                    {!worker.is_enabled && <span className="pill"> выключен</span>}
                    <div className="muted" style={{ fontSize: 12 }}>
                      {seenAgo(worker.last_seen_at)}
                    </div>
                    {worker.last_error && (
                      <div style={{ color: "var(--err)", fontSize: 12 }}>{worker.last_error}</div>
                    )}
                  </td>
                  <td className="muted" style={{ fontSize: 12 }}>
                    {worker.labels.join(", ") || "—"}
                  </td>
                  <td className="nowrap">
                    {worker.running_jobs} / {worker.concurrency}
                    <div className="muted" style={{ fontSize: 12 }}>
                      {worker.queued ? `в очереди ${worker.queued}` : "очередь пуста"}
                    </div>
                  </td>
                  <td className="nowrap muted" style={{ fontSize: 12 }}>
                    проектов {worker.projects} · аккаунтов {worker.accounts}
                    <div>свободно {gigabytes(worker.disk_free)}</div>
                  </td>
                  <td style={{ fontSize: 12 }}>
                    {worker.public_url ? (
                      <span className="mono truncate">{worker.public_url}</span>
                    ) : (
                      <span className="muted">не отдаёт — превью не будет</span>
                    )}
                  </td>
                  <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                    <button className="small" onClick={() => toggle(worker)}>
                      {worker.is_enabled ? "Выключить" : "Включить"}
                    </button>{" "}
                    <button className="small danger" onClick={() => remove(worker)}>
                      Удалить
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="config-hint" style={{ marginTop: 12 }}>
        Выключенный воркер доделывает начатое и больше не берёт новых задач — процесс
        при этом остаётся живым. Проекты закрепляются за воркером на загрузке
        исходника, аккаунты — вручную на странице «Аккаунты»: у них на машине лежит
        профиль браузера.
      </div>
    </>
  );
}
