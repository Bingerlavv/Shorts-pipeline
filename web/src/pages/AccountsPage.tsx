import { useState } from "react";
import { ApiError, api } from "../api";
import { Banner, Empty, Modal, formatDate } from "../components/ui";
import { useAsync } from "../hooks/useLiveState";
import type { Account, Project } from "../types";

export default function AccountsPage() {
  const accounts = useAsync<Account[]>(() => api.accounts.list(), []);
  const projects = useAsync<Project[]>(() => api.projects.list(), []);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [igOpen, setIgOpen] = useState(false);
  const [editing, setEditing] = useState<Account | null>(null);

  const setProjects = async (account: Account, ids: number[]) => {
    setError("");
    try {
      await api.accounts.setProjects(account.id, ids);
      accounts.reload();
    } catch (exc) {
      setError((exc as Error).message);
    }
  };

  const connectYoutube = async () => {
    setError("");
    try {
      const { url } = await api.accounts.youtubeAuthUrl();
      // Окно само закроется после колбэка; список обновим по возвращении фокуса.
      window.open(url, "_blank", "width=520,height=680");
      setNotice("Заверши вход в открывшемся окне, затем обнови список.");
    } catch (exc) {
      setError((exc as Error).message);
    }
  };

  // Адрес возврата нужен до входа: без него приложение TikTok не пропустит,
  // а понять это по ошибке площадки почти невозможно.
  const tiktokRedirect = useAsync<{ redirect_uri: string }>(
    () => api.accounts.tiktokRedirectUri(),
    [],
  );

  const connectTiktok = async () => {
    setError("");
    try {
      const { url } = await api.accounts.tiktokAuthUrl();
      window.open(url, "_blank", "width=520,height=680");
      setNotice("Заверши вход в открывшемся окне, затем обнови список.");
    } catch (exc) {
      setError((exc as Error).message);
    }
  };

  const verify = async (account: Account) => {
    setError("");
    setNotice("");
    try {
      const result = await api.accounts.verify(account.id);
      setNotice(`${account.name}: ${result.message}`);
      accounts.reload();
    } catch (exc) {
      setError((exc as Error).message);
    }
  };

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Аккаунты</h1>
          <p>
            Токены хранятся зашифрованными ключом SHORTS_SECRET_KEY. При смене ключа
            аккаунты придётся подключить заново.
          </p>
        </div>
        <div className="row">
          <button onClick={connectYoutube}>Подключить YouTube</button>
          <button onClick={connectTiktok}>Подключить TikTok</button>
          <button onClick={() => setIgOpen(true)}>Подключить Instagram</button>
          <button onClick={accounts.reload}>Обновить</button>
        </div>
      </div>

      {tiktokRedirect.data && (
        <div className="config-hint" style={{ marginBottom: 12 }}>
          Для TikTok заводи приложение типа <b>Desktop</b> — тогда внешний домен не нужен.
          В поле Redirect URI вставь этот адрес:{" "}
          <code>{tiktokRedirect.data.redirect_uri}</code>{" "}
          <button
            className="small ghost"
            onClick={() => navigator.clipboard?.writeText(tiktokRedirect.data!.redirect_uri)}
          >
            Скопировать
          </button>
        </div>
      )}

      {error && <Banner tone="err">{error}</Banner>}
      {notice && <Banner tone="info">{notice}</Banner>}

      {(accounts.data ?? []).length === 0 ? (
        <Empty>
          Аккаунтов нет. Для YouTube нужен OAuth-клиент типа Desktop app в Google Cloud
          Console; Instagram подключается логином и паролем — или токеном Graph API,
          если аккаунт Business и привязан к странице Facebook.
        </Empty>
      ) : (
        <div className="card" style={{ padding: 0 }}>
          <table>
            <thead>
              <tr>
                <th>Площадка</th>
                <th>Аккаунт</th>
                <th>Состояние</th>
                <th>Что публикуем сюда</th>
                <th className="nowrap">Подключён</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {(accounts.data ?? []).map((account) => (
                <tr key={account.id}>
                  <td>{account.platform}</td>
                  <td>
                    {account.name}
                    <div className="muted mono" style={{ fontSize: 11.5 }}>
                      {account.external_id}
                    </div>
                  </td>
                  <td>
                    {account.is_active ? (
                      <span className="pill ok">активен</span>
                    ) : (
                      <span className="pill err">отключён</span>
                    )}
                    {account.last_error && (
                      <div style={{ color: "var(--err)", fontSize: 12 }}>{account.last_error}</div>
                    )}
                  </td>
                  <td>
                    {account.project_ids.length === 0 ? (
                      <span className="muted">ничего</span>
                    ) : (
                      <span className="truncate">
                        {account.project_ids
                          .map(
                            (id) =>
                              (projects.data ?? []).find((item) => item.id === id)?.title ||
                              `проект ${id}`,
                          )
                          .join(", ")}
                      </span>
                    )}
                    <div>
                      <button className="small" onClick={() => setEditing(account)}>
                        Выбрать проекты
                      </button>
                    </div>
                  </td>
                  <td className="muted nowrap">{formatDate(account.created_at)}</td>
                  <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                    <button className="small" onClick={() => verify(account)}>
                      Проверить
                    </button>{" "}
                    <button
                      className="small"
                      onClick={async () => {
                        await api.accounts.toggle(account.id);
                        accounts.reload();
                      }}
                    >
                      {account.is_active ? "Выключить" : "Включить"}
                    </button>{" "}
                    <button
                      className="small danger"
                      onClick={async () => {
                        if (!confirm(`Удалить аккаунт «${account.name}»?`)) return;
                        setError("");
                        try {
                          await api.accounts.remove(account.id);
                        } catch (exc) {
                          // 409 — у аккаунта есть история публикаций. Сервер
                          // присылает её объём: спрашиваем ещё раз, уже зная цену.
                          const failure = exc as ApiError;
                          if (
                            failure.status !== 409 ||
                            !confirm(`${failure.message}

Всё равно удалить?`)
                          ) {
                            if (failure.status !== 409) setError(failure.message);
                            return;
                          }
                          await api.accounts.remove(account.id, true);
                        }
                        accounts.reload();
                      }}
                    >
                      Удалить
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {editing && (
        <Modal
          title={`Что публикуем в «${editing.name}»`}
          onClose={() => setEditing(null)}
        >
          <p className="muted" style={{ marginTop: 0 }}>
            Отмеченные проекты уходят в этот аккаунт целиком: каждый смонтированный
            ролик — и автопрогоном, и кнопкой «Опубликовать». Пресет отвечает только
            за оформление и расписание.
          </p>
          <div className="bg-picker" style={{ maxHeight: 320, overflowY: "auto" }}>
            {(projects.data ?? []).map((project) => {
              const chosen = editing.project_ids.includes(project.id);
              return (
                <label key={project.id} className="check">
                  <input
                    type="checkbox"
                    checked={chosen}
                    onChange={() => {
                      const next = chosen
                        ? editing.project_ids.filter((id) => id !== project.id)
                        : [...editing.project_ids, project.id];
                      setEditing({ ...editing, project_ids: next });
                      setProjects(editing, next);
                    }}
                  />
                  <span className="truncate">
                    {project.title || project.source_url}
                    <span className="muted"> · роликов {project.segment_count}</span>
                  </span>
                </label>
              );
            })}
          </div>
          {(projects.data ?? []).length === 0 && (
            <Empty>Проектов пока нет — добавь первый на странице «Проекты».</Empty>
          )}
        </Modal>
      )}

      {igOpen && (
        <InstagramConnect
          onClose={() => setIgOpen(false)}
          onConnected={() => {
            setIgOpen(false);
            accounts.reload();
          }}
        />
      )}
    </>
  );
}

function InstagramConnect({
  onClose,
  onConnected,
}: {
  onClose: () => void;
  onConnected: () => void;
}) {
  const [mode, setMode] = useState<"login" | "graph">("login");

  return (
    <Modal title="Подключение Instagram" onClose={onClose}>
      <div className="row" style={{ marginBottom: 14 }}>
        <button
          className={`small${mode === "login" ? " primary" : ""}`}
          onClick={() => setMode("login")}
        >
          Логин и пароль
        </button>
        <button
          className={`small${mode === "graph" ? " primary" : ""}`}
          onClick={() => setMode("graph")}
        >
          Graph API (токен)
        </button>
      </div>

      {mode === "login" ? (
        <InstagramLoginForm onConnected={onConnected} />
      ) : (
        <InstagramGraphForm onConnected={onConnected} />
      )}
    </Modal>
  );
}

function InstagramLoginForm({ onConnected }: { onConnected: () => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [needsCode, setNeedsCode] = useState(false);
  const [sessionid, setSessionid] = useState("");
  const [showSessionid, setShowSessionid] = useState(false);
  const [advanced, setAdvanced] = useState(false);
  const [totpSeed, setTotpSeed] = useState("");
  const [proxy, setProxy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const result = await api.accounts.instagramLogin({
        username: username.trim(),
        password,
        sessionid: sessionid.trim(),
        verification_code: code.trim(),
        totp_seed: totpSeed.trim(),
        proxy: proxy.trim(),
      });
      if (result.status === "ok") {
        onConnected();
        return;
      }
      if (result.status === "two_factor_required") {
        setNeedsCode(true);
        setNotice("Введи код из приложения-аутентификатора или из SMS и повтори вход.");
      } else {
        // Проверку входа из панели не пройти — остаётся принести готовую
        // сессию из браузера, который её уже прошёл.
        setShowSessionid(true);
        setError(result.message);
      }
    } catch (exc) {
      setError((exc as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>
        Обычный аккаунт, без Business-режима и приложения Meta. Ролик уходит файлом,
        публичный адрес сервера не нужен. Пароль хранится зашифрованным ключом
        SHORTS_SECRET_KEY — он нужен, чтобы восстановить сессию, когда Instagram её
        сбросит.
      </p>

      <div className="field">
        <label>Логин</label>
        <input
          value={username}
          onChange={(event) => setUsername(event.target.value)}
          placeholder="username"
          autoComplete="off"
        />
      </div>
      <div className="field">
        <label>Пароль</label>
        <input
          type="password"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          autoComplete="off"
        />
      </div>
      {needsCode && (
        <div className="field">
          <label>Код двухфакторной аутентификации</label>
          <input
            value={code}
            onChange={(event) => setCode(event.target.value)}
            placeholder="123456"
            inputMode="numeric"
            autoComplete="off"
          />
        </div>
      )}

      <div className="row">
        <button className="small" onClick={() => setAdvanced(!advanced)}>
          {advanced ? "Скрыть дополнительное" : "Прокси и постоянный код 2FA"}
        </button>
        <button className="small" onClick={() => setShowSessionid(!showSessionid)}>
          Войти по sessionid
        </button>
      </div>

      {showSessionid && (
        <div className="field" style={{ marginTop: 12 }}>
          <label>sessionid из браузера</label>
          <input
            value={sessionid}
            onChange={(event) => setSessionid(event.target.value)}
            placeholder="12345678%3AAbCdEf..."
            autoComplete="off"
          />
          <div className="muted" style={{ fontSize: 12 }}>
            Путь для случая, когда Instagram требует подтвердить вход: браузер эту
            проверку уже прошёл. Войди на instagram.com, затем F12 → Application →
            Storage → Cookies → https://www.instagram.com и скопируй значение куки{" "}
            <span className="mono">sessionid</span> из столбца Value. Через{" "}
            <span className="mono">document.cookie</span> её не видно — она HttpOnly.
            Логин и пароль тогда не нужны. Учти: сессия умрёт, если выйти из аккаунта
            в том же браузере, а продлить её без пароля нечем.
          </div>
        </div>
      )}

      {advanced && (
        <div style={{ marginTop: 12 }}>
          <div className="field">
            <label>Секрет 2FA (base32)</label>
            <input
              value={totpSeed}
              onChange={(event) => setTotpSeed(event.target.value)}
              placeholder="показывается при настройке двухфакторки"
              autoComplete="off"
            />
            <div className="muted" style={{ fontSize: 12 }}>
              Если сохранить, коды будут генерироваться сами при каждом входе.
            </div>
          </div>
          <div className="field">
            <label>Прокси</label>
            <input
              value={proxy}
              onChange={(event) => setProxy(event.target.value)}
              placeholder="194.71.107.74:11368:логин:пароль"
              autoComplete="off"
            />
            <div className="muted" style={{ fontSize: 12 }}>
              Можно вставлять как есть от продавца — <code>host:port:логин:пароль</code>,
              с любой схемой впереди или без неё. Привычный вид{" "}
              <code>http://логин:пароль@host:port</code> тоже подойдёт.
            </div>
            <div className="muted" style={{ fontSize: 12 }}>
              Весь трафик аккаунта пойдёт через него. Менять адрес от запуска к запуску
              не стоит — для Instagram это выглядит подозрительно. Свой прокси заодно
              даёт аккаунту отдельную очередь загрузки: без него ролики уходят строго
              по одному, все аккаунты по очереди.
            </div>
          </div>
        </div>
      )}

      <div style={{ marginTop: 12 }}>
        <button
          className="primary"
          disabled={busy || (!sessionid.trim() && (!username.trim() || !password))}
          onClick={submit}
        >
          {busy ? "Вхожу…" : "Войти и подключить"}
        </button>
      </div>

      {error && (
        <div style={{ marginTop: 12 }}>
          <Banner tone="err">{error}</Banner>
        </div>
      )}
      {notice && (
        <div style={{ marginTop: 12 }}>
          <Banner tone="info">{notice}</Banner>
        </div>
      )}

      <Banner tone="warn">
        Instagram не одобряет автоматическую публикацию и может потребовать
        подтверждение входа или ограничить аккаунт. Публикуй единицы роликов в сутки
        и держи паузы между ними.
      </Banner>
    </>
  );
}

function InstagramGraphForm({ onConnected }: { onConnected: () => void }) {
  const [token, setToken] = useState("");
  const [found, setFound] = useState<any[] | null>(null);
  const [longLived, setLongLived] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const discover = async () => {
    setBusy(true);
    setError("");
    try {
      const result = await api.accounts.instagramDiscover(token.trim());
      setLongLived(result.access_token);
      setFound(result.accounts);
    } catch (exc) {
      setError((exc as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const connect = async (account: any) => {
    setBusy(true);
    setError("");
    try {
      await api.accounts.instagramConnect({
        access_token: account.page_access_token || longLived,
        ig_user_id: account.ig_user_id,
        username: account.username,
        page_name: account.page_name,
      });
      onConnected();
    } catch (exc) {
      setError((exc as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>
        Meta не даёт пройти OAuth с localhost, поэтому токен вставляется вручную. Возьми
        его в Graph API Explorer, выбрав своё приложение и права{" "}
        <span className="mono">instagram_basic</span>,{" "}
        <span className="mono">instagram_content_publish</span>,{" "}
        <span className="mono">pages_show_list</span>,{" "}
        <span className="mono">pages_read_engagement</span>. Сервер сам обменяет его на
        60-дневный.
      </p>

      <div className="field">
        <label>User Access Token</label>
        <input
          value={token}
          onChange={(event) => setToken(event.target.value)}
          placeholder="EAAG..."
        />
      </div>
      <button className="primary" disabled={busy || token.trim().length < 20} onClick={discover}>
        {busy ? "Проверяю…" : "Найти аккаунты"}
      </button>

      {error && (
        <div style={{ marginTop: 12 }}>
          <Banner tone="err">{error}</Banner>
        </div>
      )}

      {found && (
        <div style={{ marginTop: 16 }}>
          <h3 style={{ fontSize: 14 }}>Найденные аккаунты</h3>
          {found.length === 0 && <p className="muted">Ничего не найдено.</p>}
          {found.map((account) => (
            <div className="row between card" key={account.ig_user_id} style={{ marginBottom: 8 }}>
              <div>
                <strong>@{account.username || account.ig_user_id}</strong>
                <div className="muted" style={{ fontSize: 12 }}>
                  страница: {account.page_name}
                </div>
              </div>
              <button className="small primary" disabled={busy} onClick={() => connect(account)}>
                Подключить
              </button>
            </div>
          ))}
        </div>
      )}

      <Banner tone="warn">
        Для публикации Reels серверу нужен публичный адрес: Instagram скачивает ролик
        сам. Задай SHORTS_PUBLIC_BASE_URL в .env.
      </Banner>
    </>
  );
}
