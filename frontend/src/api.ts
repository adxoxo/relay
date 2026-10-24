// Typed fetch helpers for the relay backend.
// All requests use credentials 'same-origin'. JSON errors are surfaced as ApiError.
// A single global 401 handler lets the app show the login screen.

import type {
  ActivityView,
  ApiErrorBody,
  CheckRunView,
  ConversationView,
  CreateSessionRequest,
  FileMeta,
  FilePage,
  FileText,
  FolderListing,
  PreviewView,
  PreviewsList,
  ProjectsView,
  SaveResult,
  SearchView,
  SessionView,
  StateView,
} from "./types";

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly body: ApiErrorBody | null;

  constructor(status: number, code: string, message: string, body: ApiErrorBody | null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.body = body;
  }
}

let unauthorizedHandler: (() => void) | null = null;

// The app registers a handler that shows the login screen on any 401.
export function onUnauthorized(handler: () => void): void {
  unauthorizedHandler = handler;
}

async function parseError(res: Response): Promise<ApiError> {
  let body: ApiErrorBody | null = null;
  try {
    const data = (await res.json()) as ApiErrorBody;
    if (data && typeof data === "object") {
      body = data;
    }
  } catch {
    body = null;
  }
  const code = body?.error ?? `http_${res.status}`;
  const message = body?.message ?? res.statusText ?? "Request failed";
  return new ApiError(res.status, code, message, body);
}

interface RequestOptions {
  method?: string;
  body?: unknown;
  // A raw body (sent as is) instead of JSON.
  raw?: { data: BodyInit; contentType: string };
  signal?: AbortSignal;
  // When true, a 401 does not trigger the global login handler (used by /api/auth/me).
  quietUnauthorized?: boolean;
}

async function request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = {};
  let payload: BodyInit | undefined;
  if (opts.raw !== undefined) {
    headers["Content-Type"] = opts.raw.contentType;
    payload = opts.raw.data;
  } else if (opts.body !== undefined) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(opts.body);
  }
  let res: Response;
  try {
    res = await fetch(path, {
      method: opts.method ?? (opts.body !== undefined || opts.raw !== undefined ? "POST" : "GET"),
      credentials: "same-origin",
      headers,
      body: payload,
      signal: opts.signal,
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") {
      throw err;
    }
    throw new ApiError(0, "network_error", "Cannot reach relay.", null);
  }

  if (res.status === 401) {
    if (!opts.quietUnauthorized && unauthorizedHandler) {
      unauthorizedHandler();
    }
    throw await parseError(res);
  }
  if (!res.ok) {
    throw await parseError(res);
  }
  if (res.status === 204) {
    return undefined as T;
  }
  const text = await res.text();
  if (!text) {
    return undefined as T;
  }
  return JSON.parse(text) as T;
}

// Auth

export async function login(token: string): Promise<void> {
  await request<void>("/api/auth/login", { body: { token } });
}

export async function logout(): Promise<void> {
  await request<void>("/api/auth/logout", { method: "POST" });
}

export interface MeView {
  authenticated: boolean;
  via: "cookie" | "bearer";
  tailscale_login: string | null;
}

export async function me(): Promise<MeView> {
  return request<MeView>("/api/auth/me", { quietUnauthorized: true });
}

// State and sessions

export async function getState(signal?: AbortSignal): Promise<StateView> {
  return request<StateView>("/api/state", { signal });
}

export async function getSession(id: string): Promise<SessionView> {
  return request<SessionView>(`/api/sessions/${encodeURIComponent(id)}`);
}

export async function createSession(req: CreateSessionRequest): Promise<SessionView> {
  return request<SessionView>("/api/sessions", { body: req });
}

export interface EndSessionRequest {
  tmux_session_id: string;
  tmux_created: number;
  pane_count: number;
}

export interface EndSessionResult {
  ended: boolean;
  already: boolean;
}

export async function endSession(
  id: string,
  req: EndSessionRequest,
): Promise<EndSessionResult> {
  return request<EndSessionResult>(`/api/sessions/${encodeURIComponent(id)}/end`, {
    body: req,
  });
}

export async function sendPrompt(id: string, text: string): Promise<void> {
  await request<{ sent: boolean }>(`/api/sessions/${encodeURIComponent(id)}/prompt`, {
    body: { text },
  });
}

export async function interruptSession(id: string): Promise<void> {
  await request<void>(`/api/sessions/${encodeURIComponent(id)}/interrupt`, {
    method: "POST",
  });
}

export async function resumeSession(id: string): Promise<SessionView> {
  return request<SessionView>(`/api/sessions/${encodeURIComponent(id)}/resume`, {
    method: "POST",
  });
}

export async function getProjects(env: string): Promise<ProjectsView> {
  return request<ProjectsView>(`/api/envs/${encodeURIComponent(env)}/projects`);
}

// Conversation and approval

export async function getConversation(
  id: string,
  limit = 200,
  signal?: AbortSignal,
): Promise<ConversationView> {
  return request<ConversationView>(
    `/api/sessions/${encodeURIComponent(id)}/conversation?limit=${limit}`,
    { signal },
  );
}

export async function sendApproval(
  id: string,
  requestId: string,
  decision: "allow" | "deny",
): Promise<void> {
  await request<void>(`/api/sessions/${encodeURIComponent(id)}/approval`, {
    body: { request_id: requestId, decision },
  });
}

// Activity

export async function getActivity(params: {
  limit?: number;
  before_id?: number;
  attention?: boolean;
  unread?: boolean;
  session_id?: string;
} = {}): Promise<ActivityView> {
  const q = new URLSearchParams();
  if (params.limit != null) q.set("limit", String(params.limit));
  if (params.before_id != null) q.set("before_id", String(params.before_id));
  if (params.attention) q.set("attention", "1");
  if (params.unread) q.set("unread", "1");
  if (params.session_id) q.set("session_id", params.session_id);
  const query = q.toString();
  return request<ActivityView>(`/api/activity${query ? `?${query}` : ""}`);
}

export async function markActivityRead(params: {
  ids?: number[];
  session_id?: string;
  all?: boolean;
}): Promise<{ updated: number }> {
  return request<{ updated: number }>("/api/activity/read", { body: params });
}

// Previews and checks

// Registered previews merged with detected web endpoints (a cached scan).
export async function getPreviews(): Promise<PreviewsList> {
  return request<PreviewsList>("/api/previews");
}

export async function rescanPreviews(): Promise<PreviewsList> {
  return request<PreviewsList>("/api/previews/scan", { method: "POST" });
}

export async function savePreviewApp(env: string, project_dir: string, candidate: string): Promise<PreviewsList> {
  return request<PreviewsList>("/api/previews/app-choice", { method: "PUT", body: { env, project_dir, candidate } });
}

export async function clearPreviewApp(env: string, project_dir: string): Promise<PreviewsList> {
  return request<PreviewsList>("/api/previews/app-choice", { method: "DELETE", body: { env, project_dir } });
}

export interface PreviewInput {
  env: string;
  name: string;
  project: string;
  command: string;
  port: number;
  health_path?: string;
  test_command?: string;
}

export async function createPreview(input: PreviewInput): Promise<PreviewView> {
  return request<PreviewView>("/api/previews", { body: input });
}

export async function deletePreview(id: string): Promise<void> {
  await request<void>(`/api/previews/${encodeURIComponent(id)}`, { method: "DELETE" });
}

export async function startPreview(id: string): Promise<void> {
  await request<void>(`/api/previews/${encodeURIComponent(id)}/start`, {
    method: "POST",
  });
}

export async function stopPreview(id: string): Promise<void> {
  await request<void>(`/api/previews/${encodeURIComponent(id)}/stop`, { method: "POST" });
}

export async function getPreviewLogs(id: string, lines = 200): Promise<{ text: string }> {
  return request<{ text: string }>(
    `/api/previews/${encodeURIComponent(id)}/logs?lines=${lines}`,
  );
}

export async function runPreviewCheck(
  id: string,
  viewports?: Array<"phone" | "desktop">,
): Promise<CheckRunView> {
  return request<CheckRunView>(`/api/previews/${encodeURIComponent(id)}/check`, {
    body: viewports ? { viewports } : {},
  });
}

export async function runPreviewTest(id: string): Promise<CheckRunView> {
  return request<CheckRunView>(`/api/previews/${encodeURIComponent(id)}/test`, {
    method: "POST",
  });
}

// Files

function filesUrl(env: string, route: string, params: Record<string, string | number | boolean>): string {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) q.set(k, String(v));
  return `/api/files/${encodeURIComponent(env)}/${route}?${q.toString()}`;
}

export async function listFolder(
  env: string,
  path: string,
  hidden: boolean,
  signal?: AbortSignal,
): Promise<FolderListing> {
  return request<FolderListing>(filesUrl(env, "list", { path, hidden }), { signal });
}

export async function searchFiles(
  env: string,
  q: string,
  hidden: boolean,
  signal?: AbortSignal,
): Promise<SearchView> {
  return request<SearchView>(filesUrl(env, "search", { q, hidden }), { signal });
}

export async function getFileMeta(env: string, path: string, signal?: AbortSignal): Promise<FileMeta> {
  return request<FileMeta>(filesUrl(env, "meta", { path }), { signal });
}

export async function getFileText(env: string, path: string, signal?: AbortSignal): Promise<FileText> {
  return request<FileText>(filesUrl(env, "text", { path }), { signal });
}

export async function getFilePage(
  env: string,
  path: string,
  page: number,
  revision?: string,
  signal?: AbortSignal,
): Promise<FilePage> {
  const params: Record<string, string | number> = { path, page };
  if (revision) params.revision = revision;
  return request<FilePage>(filesUrl(env, "page", params), { signal });
}

// The body is the raw UTF-8 text. A stale base revision gets 409 revision_changed.
export async function saveFileText(
  env: string,
  path: string,
  text: string,
  baseRevision: string,
): Promise<SaveResult> {
  return request<SaveResult>(filesUrl(env, "text", { path, base_revision: baseRevision }), {
    method: "PUT",
    raw: { data: new Blob([text], { type: "text/plain;charset=utf-8" }), contentType: "text/plain; charset=utf-8" },
  });
}

export function fileRawUrl(env: string, path: string, opts: { download?: boolean; revision?: string } = {}): string {
  const params: Record<string, string | number | boolean> = { path };
  if (opts.download) params.download = 1;
  if (opts.revision) params.v = opts.revision;
  return filesUrl(env, "raw", params);
}
