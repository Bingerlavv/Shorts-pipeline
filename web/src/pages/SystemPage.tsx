import { api } from "../api";
import { Banner, Empty, statusLabel } from "../components/ui";
import { useAsync } from "../hooks/useLiveState";
import type { SystemStatus } from "../types";

const STORAGE_LABEL: Record<string, string> = {
  free_gb: "Свободно на диске",
  total_gb: "Объём диска",
  sources_mb: "Исходники",
  renders_mb: "Готовые ролики",
  clips_mb: "Черновые нарезки",
  thumbs_mb: "Кадры-превью",
  audio_mb: "Извлечённый звук",
  assets_mb: "Материалы",
};

/** Гигабайты и мегабайты приходят разными ключами — единицу берём из имени. */
function storageValue(key: string, value: unknown): string {
  const unit = key.endsWith("_gb") ? " ГБ" : key.endsWith("_mb") ? " МБ" : "";
  return `${value}${unit}`;
}

export default function SystemPage() {
  const status = useAsync<SystemStatus>(() => api.system.status(), []);

  if (status.error) return <Banner tone="err">{status.error}</Banner>;
  if (!status.data) return <Empty>Опрашиваю сервер…</Empty>;

  const data = status.data;

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Диагностика</h1>
          <p>Что настроено и работает, а что требует внимания.</p>
        </div>
        <button onClick={status.reload}>Обновить</button>
      </div>

      {!data.secret_key_set && (
        <Banner tone="err">
          SHORTS_SECRET_KEY не задан — подключить аккаунты площадок не получится.
          Сгенерируй ключ и добавь его в .env.
        </Banner>
      )}
      {!data.ffmpeg.available && (
        <Banner tone="err">
          ffmpeg не найден: {data.ffmpeg.hint}. Без него не работают ни нарезка, ни
          монтаж.
        </Banner>
      )}
      {data.ytdlp?.hint && <Banner tone="warn">yt-dlp {data.ytdlp.hint}</Banner>}
      {data.web_build?.stale && <Banner tone="warn">{data.web_build.hint}</Banner>}
      {!data.public_base_url && (
        <Banner tone="warn">
          SHORTS_PUBLIC_BASE_URL пуст. Публикация в Instagram через Graph API не
          пройдёт: он скачивает ролик по публичной ссылке. Аккаунтов, подключённых
          логином и паролем, это не касается — им адрес не нужен.
        </Banner>
      )}

      <div className="grid cols-2">
        <div className="card">
          <h3 style={{ marginTop: 0, fontSize: 14 }}>Окружение</h3>
          <table>
            <tbody>
              <tr>
                <td className="muted">Версия</td>
                <td className="mono">{data.version}</td>
              </tr>
              <tr>
                <td className="muted">ffmpeg</td>
                <td className="mono" style={{ wordBreak: "break-all" }}>
                  {data.ffmpeg.available ? data.ffmpeg.version : "не найден"}
                </td>
              </tr>
              <tr>
                <td className="muted">yt-dlp</td>
                <td className="mono">
                  {data.ytdlp?.available ? data.ytdlp.version : "не установлен"}
                </td>
              </tr>
              <tr>
                <td className="muted">Модель LLM</td>
                <td className="mono">{data.llm_selected}</td>
              </tr>
              <tr>
                <td className="muted">Публичный адрес</td>
                <td className="mono">{data.public_base_url || "не задан"}</td>
              </tr>
            </tbody>
          </table>
        </div>

        <div className="card">
          <h3 style={{ marginTop: 0, fontSize: 14 }}>Хранилище</h3>
          <table>
            <tbody>
              {Object.entries(data.storage).map(([key, value]) => (
                <tr key={key}>
                  <td className="muted">{STORAGE_LABEL[key] ?? key}</td>
                  <td className="mono nowrap">{storageValue(key, value)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="card">
          <h3 style={{ marginTop: 0, fontSize: 14 }}>Распознавание речи</h3>
          {data.stt_providers.map((provider) => (
            <div key={provider.name} className="row between" style={{ padding: "6px 0" }}>
              <span className="mono">{provider.name}</span>
              {provider.available ? (
                <span className="pill ok">доступен</span>
              ) : (
                <span className="muted" style={{ fontSize: 12, textAlign: "right", maxWidth: "70%" }}>
                  {provider.reason}
                </span>
              )}
            </div>
          ))}
        </div>

        <div className="card">
          <h3 style={{ marginTop: 0, fontSize: 14 }}>Языковые модели</h3>
          {data.llm_providers.map((provider) => (
            <div key={provider.name} className="row between" style={{ padding: "6px 0" }}>
              <span className="mono">
                {provider.selected && (
                  <span className="pill mark" style={{ marginRight: 6 }}>
                    выбран
                  </span>
                )}
                {provider.name}
                {provider.model ? ` · ${provider.model}` : ""}
                {provider.installed?.length ? (
                  <div className="muted" style={{ fontSize: 11.5, marginTop: 2 }}>
                    скачано: {provider.installed.join(", ")}
                  </div>
                ) : null}
              </span>
              {provider.available ? (
                <span className="pill ok">доступен</span>
              ) : (
                <span className="muted" style={{ fontSize: 12, textAlign: "right", maxWidth: "60%" }}>
                  {provider.reason}
                </span>
              )}
            </div>
          ))}
        </div>

        <div className="card">
          <h3 style={{ marginTop: 0, fontSize: 14 }}>Очередь</h3>
          <div className="row">
            {Object.entries(data.queue).map(([key, count]) => (
              <span key={key} className="pill neutral">
                {statusLabel(key)}: {count}
              </span>
            ))}
          </div>
        </div>
      </div>
    </>
  );
}
