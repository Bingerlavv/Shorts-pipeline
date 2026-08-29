export type ProjectStatus =
  | "new"
  | "downloading"
  | "transcribing"
  | "analyzing"
  | "ready"
  | "rendering"
  | "done"
  | "failed";

export type SegmentStatus =
  | "candidate"
  | "approved"
  | "rejected"
  | "rendering"
  | "rendered"
  | "publishing"
  | "published"
  | "failed";

export type JobStatus = "queued" | "running" | "succeeded" | "failed" | "cancelled";

export type PublicationStatus =
  | "pending"
  | "scheduled"
  | "uploading"
  | "published"
  | "failed";

export type Config = Record<string, any>;

export interface Project {
  id: number;
  title: string;
  source_url: string;
  /** "url" — скачано по ссылке, "file" — файл с диска, взятый на месте */
  source_kind: string;
  status: ProjectStatus;
  stage_message: string;
  error: string;
  duration: number;
  width: number;
  height: number;
  fps: number;
  has_burned_subtitles: boolean | null;
  preset_id: number | null;
  auto_publish: boolean;
  config_overrides: Config;
  source_meta: Record<string, any>;
  created_at: string;
  updated_at: string;
  segment_count: number;
  has_transcript: boolean;
  /** Куда публикуется проект. Хранится связью, а не в config_overrides. */
  account_ids: number[];
}

export interface Segment {
  id: number;
  project_id: number;
  start: number;
  end: number;
  source_ranges: number[][];
  title_de: string;
  description_de: string;
  hashtags: string[];
  hook: string;
  transcript_text: string;
  score: number;
  reason: string;
  status: SegmentStatus;
  error: string;
  clip_path: string;
  render_path: string;
  thumb_path: string;
  render_meta: Record<string, any>;
  edit_overrides: Config;
  /** Точное время выхода. null — считать по расписанию. */
  publish_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface Preset {
  id: number;
  name: string;
  description: string;
  is_default: boolean;
  config: Config;
  created_at: string;
  updated_at: string;
}

export interface Asset {
  id: number;
  kind: string;
  name: string;
  mime: string;
  size: number;
  meta: Record<string, any>;
  created_at: string;
}

export interface Account {
  id: number;
  platform: string;
  name: string;
  external_id: string;
  meta: Record<string, any>;
  is_active: boolean;
  last_error: string;
  created_at: string;
  /** Проекты, которые публикуются в этот аккаунт. */
  project_ids: number[];
}

export interface Publication {
  id: number;
  segment_id: number;
  // Приходит только из выборок API — в самой публикации проекта нет.
  project_id: number | null;
  account_id: number;
  platform: string;
  status: PublicationStatus;
  title: string;
  description: string;
  privacy: string;
  scheduled_at: string | null;
  published_at: string | null;
  remote_id: string;
  remote_url: string;
  error: string;
  created_at: string;
}

export interface Job {
  id: number;
  type: string;
  status: JobStatus;
  priority: number;
  attempts: number;
  max_attempts: number;
  progress: number;
  message: string;
  error: string;
  project_id: number | null;
  segment_id: number | null;
  publication_id: number | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface JobDetail extends Job {
  payload: Record<string, any>;
  log: string;
}

export interface Transcript {
  id: number;
  language: string;
  provider: string;
  model: string;
  segments: { start: number; end: number; text: string }[];
  full_text: string;
}

export interface ProviderStatus {
  selected?: boolean;
  installed?: string[];
  name: string;
  available: boolean;
  reason: string;
  model: string;
}

export interface SystemStatus {
  version: string;
  ffmpeg: { available: boolean; path: string; version: string; hint: string };
  ytdlp: { available: boolean; version: string; hint: string };
  web_build: { built: boolean; stale: boolean; built_at?: string; hint: string };
  storage: Record<string, number | string>;
  stt_providers: ProviderStatus[];
  llm_providers: ProviderStatus[];
  llm_selected: string;
  public_base_url: string;
  secret_key_set: boolean;
  queue: Record<string, number>;
}

/** Снимок, приходящий по SSE. */
export interface LiveState {
  jobs: {
    id: number;
    type: string;
    status: JobStatus;
    progress: number;
    message: string;
    error: string;
    project_id: number | null;
    segment_id: number | null;
  }[];
  projects: {
    id: number;
    status: ProjectStatus;
    stage_message: string;
    error: string;
    title: string;
  }[];
  segments: {
    id: number;
    project_id: number;
    status: SegmentStatus;
    error: string;
    has_render: boolean;
  }[];
}
