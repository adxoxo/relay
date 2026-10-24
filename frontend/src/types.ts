// Data contract mirrors ARCHITECTURE.md sections 6 and 8.
// These types describe the JSON that the relay backend returns.

export type EnvId = string;

export type EnvStatus = "running" | "stopped" | "missing" | "error";

export interface EnvView {
  id: EnvId;
  label: string;
  container: string;
  available: boolean;
  status: EnvStatus;
  network: string | null;
  projects_root: string;
  error: string | null;
  snapshot_at: number | null;
  agents: { claude: boolean; codex: boolean };
  provisioned: boolean;
}

export type SessionKind = "claude" | "codex" | "shell" | "other";

export type SessionStatus = "live" | "ended" | "interrupted";

export type SessionState =
  | "working"
  | "needs_input"
  | "idle"
  | "exited"
  | "unknown"
  | string;

export type StateSource = "hook" | "notify" | "terminal" | "screen" | "codex_rollout" | null;

export type EndReason =
  | "ended_by_user"
  | "closed"
  | "container_stopped"
  | "container_restarted"
  | "server_restarted"
  | string;

export interface PendingRequest {
  id: string;
  source: string;
  tool: string;
  summary: string;
  detail: string | null;
  match: string[];
  created_at: number;
  answerable: boolean;
}

export interface SessionCapabilities {
  terminal: boolean;
  prompt: boolean;
  approve: boolean;
  interrupt: boolean;
  conversation: boolean;
  resume: boolean;
  end: boolean;
}

export interface SessionTmux {
  session_id: string;
  created: number | null;
  pane_id: string;
  pane_count: number;
  socket: string;
  server_pid: number;
}

export interface SessionView {
  id: string;
  env: EnvId;
  env_label: string;
  name: string;
  tmux_name: string;
  kind: SessionKind;
  origin: string;
  status: SessionStatus;
  state: SessionState;
  state_source: StateSource;
  state_at: number | null;
  project_dir: string;
  project: string;
  last_message: string | null;
  activity_at: number | null;
  foreground: string | null;
  pending: PendingRequest | null;
  agent_session_id: string | null;
  created_at: number | null;
  ended_at: number | null;
  end_reason: EndReason | null;
  tmux: SessionTmux;
  capabilities: SessionCapabilities;
  notes: string[];
}

export interface StateCounts {
  sessions: number;
  working: number;
  needs_input: number;
  finished_unread: number;
  by_env: Record<string, number>;
}

export interface StateView {
  generated_at: number;
  version: string;
  host: { name: string };
  tailscale_hostname: string | null;
  envs: EnvView[];
  sessions: SessionView[];
  counts: StateCounts;
  activity_unread: number;
}

export type ConvRole = "user" | "assistant" | "tool";

export interface ConvToolInfo {
  name: string;
  summary: string;
}

export interface ConvItem {
  id: string;
  role: ConvRole;
  text: string;
  ts: number | null;
  tool: ConvToolInfo | null;
  phase?: "commentary" | "final_answer";
}

export interface ConversationView {
  supported: boolean;
  reason: string | null;
  items: ConvItem[];
  active_started_at?: number | null;
  active_prompt_id?: string | null;
  state_at?: number | null;
}

export type ActivityKind = string;

export interface ActivityItem {
  id: number;
  kind: ActivityKind;
  title: string;
  body: string | null;
  session_id: string | null;
  env: string | null;
  attention: boolean;
  read: boolean;
  created_at: number;
}

export interface ActivityView {
  items: ActivityItem[];
  unread: number;
}

export type CheckKind = "smoke" | "test_command";

export type CheckStatus = "running" | "passed" | "failed" | "error";

export interface CheckArtifact {
  name: string;
  url: string;
}

export interface CheckRunView {
  id: string;
  preview_id: string;
  kind: CheckKind;
  status: CheckStatus;
  target_url: string | null;
  summary: string | null;
  started_at: number | null;
  finished_at: number | null;
  result: Record<string, unknown> | null;
  artifacts: CheckArtifact[];
}

export type PreviewServerStatus = string;

export interface PreviewServer {
  status: PreviewServerStatus;
  started_at: number | null;
  ready_at: number | null;
  error: string | null;
}

export type PhoneStatus = "verified" | "not_set_up" | "failed" | "blocked";

export interface PhoneView {
  status: PhoneStatus;
  url: string | null;
  gateway_port: number | null;
  target: string | null;
  checked_at: number | null;
  message: string | null;
}

export interface DetectedEndpoint {
  key: string;
  env: EnvId;
  port: number;
  host_port: number | null;
  reachable: boolean;
  network: "host" | "published" | "unpublished" | "docker";
  protocol: "http" | "https";
  source: "process" | "docker";
  // html_entry: a static server whose served folder has an index.html.
  process: { pid: number | null; name: string | null; tool: string | null; started_at: number | null; html_entry?: boolean };
  project: string | null;
  folder: string | null;
  docker: { name: string; image: string; container_port: number; link: "compose" | "mount" | "registered" } | null;
  phone?: PhoneView;
}

export interface PreviewView {
  id: string;
  env: EnvId;
  name: string;
  project_dir: string;
  command: string;
  port: number;
  host_port: number | null;
  health_path: string;
  test_command: string | null;
  listening: boolean;
  server: PreviewServer | null;
  // tailnet: only a verified gateway route; never a generated URL.
  urls: { local: string | null; tailnet: string | null };
  last_check: CheckRunView | null;
  source?: "managed" | "detected" | null;
  detected?: DetectedEndpoint | null;
  phone?: PhoneView | null;
}

export interface ScanSourceStatus {
  status: "ok" | "partial" | "error" | "unavailable" | "off";
  message: string | null;
}

export interface PreviewsList {
  previews: PreviewView[];
  detected: DetectedEndpoint[];
  app_choices?: PreviewAppGroup[];
  scan: { at: number; envs: Record<string, ScanSourceStatus>; docker: ScanSourceStatus };
}

export interface PreviewAppCandidate {
  id: string;
  kind: "registered" | "process" | "docker";
  tool: string | null;
  frontend: boolean;
  name: string | null;
  port: number;
  host_port: number | null;
  status: "running" | "starting" | "stopped";
  preview_id: string | null;
  endpoint_key: string | null;
}

export interface PreviewAppGroup {
  key: string;
  env: string;
  project_dir: string;
  project: string;
  state: "saved" | "suggested" | "needs_choice";
  app: string | null;
  saved: {
    kind: PreviewAppCandidate["kind"];
    snapshot: { name?: string | null; tool?: string | null; port?: number; host_port?: number | null };
    chosen_at: number;
    available: boolean;
  } | null;
  candidates: PreviewAppCandidate[];
}

// Files (ARCHITECTURE.md section 6, files)

export type FileKind = "markdown" | "code" | "text" | "image" | "video" | "binary" | "file";
export type FileMode = "edit" | "paged" | "download" | "image" | "video";

export interface FileEntry {
  name: string;
  path: string;
  type: "dir" | "file" | "other" | "link" | "link_broken" | "link_outside";
  link: boolean;
  size?: number;
  mtime?: number;
  items?: number | null;
  kind?: FileKind;
  lang?: string | null;
}

export interface FolderListing {
  path: string;
  root: string;
  entries: FileEntry[];
  truncated: boolean;
}

export interface SearchResult {
  name: string;
  path: string;
  parent: string;
  kind: FileKind;
  lang: string | null;
}

export interface SearchView {
  query: string;
  results: SearchResult[];
  total: number;
  limit: number;
  partial: boolean;
  reason: string | null;
}

export interface FileMeta {
  path: string;
  name: string;
  size: number;
  mtime: number;
  revision: string;
  writable: boolean;
  hard_links: number;
  kind: FileKind;
  lang: string | null;
  mode: FileMode;
  mime: string | null;
  active: boolean;
  reason?: "binary" | "too_large";
  limits: { edit: number; paged: number };
}

export interface FileText extends FileMeta {
  text: string;
  invalid_utf8: boolean;
  line_ending: "lf" | "crlf" | "mixed";
  editable: boolean;
  edit_blocked: "invalid_utf8" | "mixed_line_endings" | "read_only" | null;
}

export interface FilePage extends FileMeta {
  page: number;
  pages: number;
  page_lines: number;
  page_bytes: number;
  start_line: number;
  end_line: number;
  total_lines: number;
  continued: boolean;
  continues: boolean;
  text: string;
  invalid_utf8: boolean;
}

export interface SaveResult {
  path: string;
  revision: string;
  size: number;
  mtime: number;
  hard_links_detached: boolean;
}

export interface ProjectEntry {
  name: string;
  path: string;
}

export interface ProjectsView {
  root: string;
  projects: ProjectEntry[];
}

// The JSON error envelope from every endpoint.
export interface ApiErrorBody {
  error: string;
  message: string;
  [key: string]: unknown;
}

// Stream frames on WS /api/stream.
export type StreamTopic = "sessions" | "activity" | "envs" | "previews" | "checks";

export type StreamFrame =
  | { type: "hello"; version: string }
  | { type: "changed"; topics: StreamTopic[] }
  | { type: "ping" };

// Permission mode for a new Claude session.
export type PermissionMode = "manual" | "acceptEdits" | "auto" | "plan";

export interface CreateSessionRequest {
  env: EnvId;
  kind: "claude" | "codex" | "shell";
  name?: string;
  project?: string;
  prompt?: string;
  permission_mode?: PermissionMode;
}
