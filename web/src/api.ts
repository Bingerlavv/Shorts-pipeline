import type {
  Account,
  Asset,
  Config,
  Job,
  JobDetail,
  Preset,
  Project,
  Publication,
  Segment,
  SystemStatus,
  Transcript,
  Worker,
} from "./types";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers:
      init?.body instanceof FormData
        ? init?.headers
        : { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });

  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      // FastAPI отдаёт либо строку в detail, либо список ошибок валидации.
      if (typeof body.detail === "string") detail = body.detail;
      else if (Array.isArray(body.detail))
        detail = body.detail.map((e: any) => `${e.loc?.join(".")}: ${e.msg}`).join("; ");
    } catch {
      /* тело не JSON — оставляем статус */
    }
    throw new ApiError(detail, response.status);
  }

  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

const get = <T,>(path: string) => request<T>(path);
const post = <T,>(path: string, body?: unknown) =>
  request<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });
const patch = <T,>(path: string, body: unknown) =>
  request<T>(path, { method: "PATCH", body: JSON.stringify(body) });
const put = <T,>(path: string, body: unknown) =>
  request<T>(path, { method: "PUT", body: JSON.stringify(body) });
const del = <T,>(path: string) => request<T>(path, { method: "DELETE" });

export const api = {
  projects: {
    list: () => get<Project[]>("/api/projects"),
    get: (id: number) => get<Project>(`/api/projects/${id}`),
    create: (body: {
      source_url: string;
      title?: string;
      preset_id?: number | null;
      auto_publish?: boolean;
      config_overrides?: Record<string, unknown>;
      start_now?: boolean;
    }) => post<Project>("/api/projects", body),
    update: (id: number, body: Partial<Project>) => patch<Project>(`/api/projects/${id}`, body),
    remove: (id: number, deleteFiles: boolean) =>
      del<void>(`/api/projects/${id}?delete_files=${deleteFiles}`),
    segments: (id: number) => get<Segment[]>(`/api/projects/${id}/segments`),
    transcript: (id: number) => get<Transcript>(`/api/projects/${id}/transcript`),
    runStage: (id: number, stage: string, auto = true) =>
      post<{ job_id: number }>(`/api/projects/${id}/run/${stage}?auto=${auto}`),
    renderApproved: (id: number) =>
      post<{ queued: number }>(`/api/projects/${id}/render-approved`),
    publishAll: (id: number) =>
      post<{ queued: number; note: string; accounts: string[] }>(
        `/api/projects/${id}/publish`,
      ),
    setAccounts: (id: number, accountIds: number[]) =>
      put<Project>(`/api/projects/${id}/accounts`, { account_ids: accountIds }),
  },
  segments: {
    get: (id: number) => get<Segment>(`/api/segments/${id}`),
    update: (id: number, body: Partial<Segment> & { clear_publish_at?: boolean }) =>
      patch<Segment>(`/api/segments/${id}`, body),
    approve: (id: number) => post<Segment>(`/api/segments/${id}/approve`),
    reject: (id: number) => post<Segment>(`/api/segments/${id}/reject`),
    remove: (id: number) => del<void>(`/api/segments/${id}`),
    caption: (id: number) => post<{ job_id: number }>(`/api/segments/${id}/caption`, {}),
    render: (id: number, autoPublish = false) =>
      post<{ job_id: number }>(`/api/segments/${id}/render?auto_publish=${autoPublish}`),
    create: (body: { project_id: number; start: number; end: number; title_de?: string }) =>
      post<Segment>("/api/segments", body),
    // Метка версии обязательна: id в SQLite переиспользуются после удаления,
    // и браузер показывал бы новому фрагменту закэшированное превью старого.
    renderUrl: (id: number, version?: string) =>
      `/api/segments/${id}/render${version ? `?v=${encodeURIComponent(version)}` : ""}`,
    thumbUrl: (id: number, version?: string) =>
      `/api/segments/${id}/thumb${version ? `?v=${encodeURIComponent(version)}` : ""}`,
  },
  presets: {
    list: () => get<Preset[]>("/api/presets"),
    schema: () => get<Config>("/api/presets/schema"),
    create: (body: Omit<Preset, "id" | "created_at" | "updated_at">) =>
      post<Preset>("/api/presets", body),
    update: (id: number, body: Omit<Preset, "id" | "created_at" | "updated_at">) =>
      put<Preset>(`/api/presets/${id}`, body),
    remove: (id: number) => del<void>(`/api/presets/${id}`),
    clone: (id: number) => post<Preset>(`/api/presets/${id}/clone`, {}),
  },
  assets: {
    list: (kind?: string) => get<Asset[]>(`/api/assets${kind ? `?kind=${kind}` : ""}`),
    upload: (kind: string, file: File, name: string) => {
      const form = new FormData();
      form.append("kind", kind);
      form.append("name", name);
      form.append("file", file);
      return request<Asset>("/api/assets", { method: "POST", body: form });
    },
    remove: (id: number) => del<void>(`/api/assets/${id}`),
    fileUrl: (id: number) => `/api/assets/${id}/file`,
  },
  accounts: {
    list: () => get<Account[]>("/api/accounts"),
    setProjects: (id: number, projectIds: number[]) =>
      put<Account>(`/api/accounts/${id}/projects`, { project_ids: projectIds }),
    youtubeAuthUrl: () => get<{ url: string }>("/api/accounts/youtube/auth-url"),
    tiktokAuthUrl: () => get<{ url: string }>("/api/accounts/tiktok/auth-url"),
    tiktokRedirectUri: () =>
      get<{ redirect_uri: string }>("/api/accounts/tiktok/redirect-uri"),
    tiktokBrowser: (body: {
      name: string;
      proxy?: string;
      login_now?: boolean;
      locale?: string;
      timezone?: string;
    }) => post<Account>("/api/accounts/tiktok/browser", body),
    tiktokRelogin: (id: number) =>
      post<{ ok: boolean; message: string }>(`/api/accounts/${id}/tiktok-login`),
    instagramDiscover: (accessToken: string) =>
      post<{ access_token: string; accounts: any[] }>("/api/accounts/instagram/discover", {
        access_token: accessToken,
        exchange_long_lived: true,
      }),
    instagramConnect: (body: {
      access_token: string;
      ig_user_id: string;
      username?: string;
      page_name?: string;
    }) => post<Account>("/api/accounts/instagram/connect", body),
    instagramLogin: (body: {
      username: string;
      password: string;
      sessionid?: string;
      verification_code?: string;
      totp_seed?: string;
      proxy?: string;
    }) =>
      post<{ status: string; message: string; account: Account | null }>(
        "/api/accounts/instagram/login",
        body,
      ),
    setWorker: (id: number, workerId: number | null) =>
      put<Account>(`/api/accounts/${id}/worker`, { worker_id: workerId }),
    verify: (id: number) => post<{ ok: boolean; message: string }>(`/api/accounts/${id}/verify`),
    toggle: (id: number) => post<Account>(`/api/accounts/${id}/toggle`),
    remove: (id: number, force = false) =>
      del<void>(`/api/accounts/${id}${force ? "?force=true" : ""}`),
  },
  publications: {
    list: (segmentId?: number) =>
      get<Publication[]>(`/api/publications${segmentId ? `?segment_id=${segmentId}` : ""}`),
    create: (body: {
      segment_id: number;
      account_id: number;
      privacy?: string;
      scheduled_at?: string | null;
      start_now?: boolean;
    }) => post<Publication>("/api/publications", body),
    // Для календаря: всё, что выходит в заданном промежутке. Для прошедших
    // публикаций сервер смотрит на фактическое время, а не на запланированное.
    range: (since: Date, until: Date, accountId?: number | null) => {
      const params = new URLSearchParams({
        since: since.toISOString(),
        until: until.toISOString(),
        limit: "1000",
      });
      if (accountId) params.set("account_id", String(accountId));
      return get<Publication[]>(`/api/publications?${params}`);
    },
    retry: (id: number) => post<{ job_id: number }>(`/api/publications/${id}/retry`),
    // Времена считает сервер той же функцией, что и настоящее расписание:
    // считать их ещё раз в панели — верный способ разойтись с конвейером.
    schedulePreview: (body: {
      schedule: Record<string, unknown>;
      account_id?: number | null;
      count?: number;
    }) => post<string[]>("/api/publications/schedule-preview", body),
    publishSegment: (segmentId: number) =>
      post<{ queued: number; note: string; accounts: string[] }>(
        `/api/publications/segment/${segmentId}`,
      ),
    remove: (id: number) => del<void>(`/api/publications/${id}`),
  },
  jobs: {
    list: (params = "") => get<Job[]>(`/api/jobs${params}`),
    get: (id: number) => get<JobDetail>(`/api/jobs/${id}`),
    cancel: (id: number) => post<{ cancelled: boolean; note: string }>(`/api/jobs/${id}/cancel`),
    retry: (id: number) => post<{ job_id: number }>(`/api/jobs/${id}/retry`),
    clearCompleted: () => del<{ deleted: number }>("/api/jobs/completed"),
    clearFailed: () => del<{ deleted: number }>("/api/jobs/failed"),
  },
  workers: {
    list: () => get<Worker[]>("/api/workers"),
    toggle: (id: number) => post<Worker>(`/api/workers/${id}/toggle`),
    remove: (id: number, force = false) =>
      del<void>(`/api/workers/${id}${force ? "?force=true" : ""}`),
    jobs: (id: number) =>
      get<{ running: { id: number; type: string; progress: number; message: string; project_id: number | null }[] }>(
        `/api/workers/${id}/jobs`,
      ),
  },
  system: {
    status: () => get<SystemStatus>("/api/system/status"),
  },
};
