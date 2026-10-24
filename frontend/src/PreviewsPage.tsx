// Previews page: one primary web app per workspace and project. The backend
// groups the servers by verified project (the nearest .git folder) and returns
// app_choices: a saved choice, a suggestion (exactly one clear web app), or a
// group that needs a choice. A main card shows the app name, workspace,
// status, Open app, and Details. The original server controls keep their
// rules inside Details: Start/Stop, Run check, Run tests, Logs, and Remove for
// a registered preview, and Register for a detected server. Open app needs a
// gateway route that relay proved in the last minute and a running app; the
// page checks the proof age again at the click. A saved choice that relay
// cannot see shows as stopped. Its snapshot is display text only.

import { useEffect, useMemo, useRef, useState, type MouseEvent, type ReactNode } from "react";
import {
  ApiError,
  clearPreviewApp,
  createPreview,
  deletePreview,
  getPreviewLogs,
  getPreviews,
  rescanPreviews,
  runPreviewCheck,
  runPreviewTest,
  savePreviewApp,
  startPreview,
  stopPreview,
  type PreviewInput,
} from "./api";
import { Modal } from "./Modal";
import { useToast } from "./toast";
import { BackIcon, CheckIcon, ChevronRightIcon, ExternalIcon, PhoneIcon, PlusIcon, RefreshIcon, ServerIcon } from "./icons";
import { clockTime } from "./util";
import type {
  CheckRunView,
  DetectedEndpoint,
  EnvView,
  PhoneView,
  PreviewAppCandidate,
  PreviewAppGroup,
  PreviewView,
  PreviewsList,
} from "./types";

interface PreviewsPageProps {
  envs: EnvView[];
  reloadKey: number;
}

// One server: a registered preview (with an optional detected endpoint) or a detected endpoint only.
interface Server {
  key: string; // "reg:<preview id>" or the endpoint key
  env: string;
  registered: PreviewView | null;
  detected: DetectedEndpoint | null;
  phone: PhoneView | null;
  state: "running" | "starting" | "stopped";
}

type Status = "running" | "starting" | "stopped" | "failed" | "unknown";

// One project group with its current app (saved or suggested), if any.
interface AppView {
  group: PreviewAppGroup;
  cand: PreviewAppCandidate | null;
  server: Server | null;
  status: Status;
}

type Sheet =
  | { kind: "details"; group: string }
  | { kind: "server"; server: string }
  | { kind: "choose"; group: string }
  | { kind: "add" }
  | { kind: "setup"; server: string; title: string };

// The server proves a route for 60 s. Open app needs a proof younger than that.
const PROOF_FRESH_S = 60;

const TOOL_LABEL: Record<string, string> = {
  vite: "Vite dev server",
  next: "Next.js dev server",
  nuxt: "Nuxt dev server",
  astro: "Astro dev server",
  remix: "Remix dev server",
  "svelte-kit": "SvelteKit dev server",
  webpack: "webpack dev server",
  "webpack-dev-server": "webpack dev server",
  "react-scripts": "React dev server",
  storybook: "Storybook",
  parcel: "Parcel dev server",
  hugo: "Hugo server",
  jekyll: "Jekyll server",
  "live-server": "live-server",
  streamlit: "Streamlit app",
  "http.server": "Python http.server",
  uvicorn: "Python uvicorn",
  gunicorn: "Python gunicorn",
  hypercorn: "Python hypercorn",
  flask: "Flask server",
  django: "Django server",
  fastapi: "FastAPI server",
  rails: "Rails server",
  puma: "Puma server",
};

// A project folder relative to the workspace's projects root.
function projectOf(dir: string | null | undefined, root: string): string | null {
  if (!dir) return null;
  if (dir === root) return "projects";
  return dir.startsWith(root + "/") ? dir.slice(root.length + 1) : dir;
}

// The card title: the last folder of the project path.
function leaf(project: string): string {
  return project.split("/").filter(Boolean).pop() ?? project;
}

function agoText(epoch: number | null | undefined, now: number): string {
  if (!epoch) return "";
  const s = Math.max(0, Math.round(now - epoch));
  if (s < 60) return `${s} s ago`;
  const m = Math.round(s / 60);
  return m < 60 ? `${m} min ago` : `${Math.round(m / 60)} h ago`;
}

function nowS(): number {
  return Date.now() / 1000;
}

// A phone link that relay proved in the last minute. Only http(s) URLs open.
function phoneFresh(phone: PhoneView | null, at: number): boolean {
  if (phone?.status !== "verified" || !phone.url || phone.checked_at == null) return false;
  if (at - phone.checked_at >= PROOF_FRESH_S) return false;
  try {
    const u = new URL(phone.url);
    return u.protocol === "https:" || u.protocol === "http:";
  } catch {
    return false;
  }
}

function phoneExpired(phone: PhoneView | null, at: number): boolean {
  return phone?.status === "verified" && !!phone.url && !phoneFresh(phone, at);
}

function candLabel(tool: string | null | undefined, kind: string | null | undefined, name: string | null | undefined): string {
  if (tool) return TOOL_LABEL[tool] ?? tool;
  if (kind === "registered") return `Registered app “${name ?? ""}”`;
  if (kind === "docker") return `Docker app ${name ?? ""}`.trim();
  return name ? `Server (${name})` : "Server";
}

// Likely apps come first in the pickers: a clear web app or a registered preview.
function likely(c: PreviewAppCandidate): boolean {
  return c.frontend || c.kind === "registered";
}

function toServers(list: PreviewsList): Server[] {
  const out: Server[] = list.previews.map((p) => {
    const status = p.server?.status;
    const state: Server["state"] =
      status === "starting" ? "starting" : status === "ready" || p.listening || p.detected ? "running" : "stopped";
    return { key: `reg:${p.id}`, env: p.env, registered: p, detected: p.detected ?? null, phone: p.phone ?? null, state };
  });
  for (const d of list.detected) {
    out.push({ key: d.key, env: d.env, registered: null, detected: d, phone: d.phone ?? null, state: "running" });
  }
  return out;
}

function serverStatus(s: Server): Status {
  return s.state === "stopped" && s.registered?.server?.error ? "failed" : s.state;
}

function pillOf(status: Status): { cls: string; text: string } {
  switch (status) {
    case "running":
      return { cls: "ok", text: "Running" };
    case "starting":
      return { cls: "warn", text: "Starting" };
    case "failed":
      return { cls: "warn", text: "Stopped with an error" };
    case "unknown":
      return { cls: "plain", text: "Status unknown" };
    default:
      return { cls: "plain", text: "Stopped" };
  }
}

function errText(err: unknown): string {
  return err instanceof ApiError ? err.message : "Cannot reach relay.";
}

export function PreviewsPage({ envs, reloadKey }: PreviewsPageProps) {
  const toast = useToast();
  const [list, setList] = useState<PreviewsList | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [scanning, setScanning] = useState(false);
  const [scanNote, setScanNote] = useState<string | null>(null);
  const [filter, setFilter] = useState<string>("all");
  const [sheet, setSheet] = useState<Sheet[]>([]);
  const [logsFor, setLogsFor] = useState<string | null>(null);
  const [logsText, setLogsText] = useState("");
  const [busy, setBusy] = useState<Set<string>>(() => new Set());
  const [register, setRegister] = useState<{ open: boolean; from: Server | null }>({ open: false, from: null });
  const [now, setNow] = useState(nowS);

  const labelOf = (id: string) => envs.find((e) => e.id === id)?.label ?? id;
  const rootOf = (id: string) => envs.find((e) => e.id === id)?.projects_root ?? "/home/node/projects";

  const load = () => {
    getPreviews()
      .then((res) => {
        setList(res);
        setLoadError(null);
      })
      .catch((err) => setLoadError(errText(err)));
  };

  useEffect(load, [reloadKey]);
  useEffect(() => {
    const t = window.setInterval(() => setNow(nowS()), 5000);
    return () => window.clearInterval(t);
  }, []);

  const rescan = async (): Promise<boolean> => {
    setScanning(true);
    try {
      setList(await rescanPreviews());
      setLoadError(null);
      setNow(nowS());
      return true;
    } catch (err) {
      toast.error(errText(err));
      return false;
    } finally {
      setScanning(false);
    }
  };

  const action = async (key: string, fn: () => Promise<unknown>, ok: string) => {
    setBusy((b) => new Set(b).add(key));
    try {
      await fn();
      toast.show(ok);
      load();
    } catch (err) {
      toast.error(errText(err));
    } finally {
      setBusy((b) => {
        const next = new Set(b);
        next.delete(key);
        return next;
      });
    }
  };

  const toggleLogs = async (id: string) => {
    if (logsFor === id) {
      setLogsFor(null);
      return;
    }
    try {
      const res = await getPreviewLogs(id);
      setLogsText(res.text);
      setLogsFor(id);
    } catch (err) {
      toast.error(errText(err));
    }
  };

  const servers = useMemo(() => (list ? toServers(list) : []), [list]);
  const serverByKey = useMemo(() => new Map(servers.map((s) => [s.key, s])), [servers]);

  const serverOf = (c: PreviewAppCandidate | null): Server | null => {
    if (!c) return null;
    if (c.preview_id) return serverByKey.get(`reg:${c.preview_id}`) ?? null;
    return c.endpoint_key ? serverByKey.get(c.endpoint_key) ?? null : null;
  };

  const envScanFailed = (env: string) => {
    const st = list?.scan.envs[env]?.status;
    return st === "error" || st === "unavailable" || st === "partial";
  };

  const apps: AppView[] = useMemo(
    () =>
      (list?.app_choices ?? []).map((group) => {
        const cand = group.app ? group.candidates.find((c) => c.id === group.app) ?? null : null;
        const server = serverOf(cand);
        const status: Status = server ? serverStatus(server) : envScanFailed(group.env) ? "unknown" : "stopped";
        return { group, cand, server, status };
      }),
    // serverOf and envScanFailed read only list and serverByKey.
    [list, serverByKey],
  );
  const appByKey = useMemo(() => new Map(apps.map((a) => [a.group.key, a])), [apps]);

  const inFilter = (env: string) => filter === "all" || env === filter;
  const cards = apps.filter((a) => a.group.state !== "needs_choice");
  // A project whose servers all have an unknown type stays in Add app: it is not a main-page row.
  const pending = apps.filter((a) => a.group.state === "needs_choice" && a.group.candidates.some(likely));
  const others = apps.filter((a) => a.group.state === "needs_choice" && !a.group.candidates.some(likely));
  const visibleCards = cards.filter((a) => inFilter(a.group.env));
  const visiblePending = pending.filter((a) => inFilter(a.group.env));
  const counts = (id: string) => cards.filter((a) => id === "all" || a.group.env === id).length;
  const where = filter === "all" ? envs.map((e) => e.label).join(" or ") || "your workspaces" : labelOf(filter);
  const scanIssues = list
    ? [
        ...Object.entries(list.scan.envs)
          .filter(([, s]) => s.status === "error" || s.status === "partial" || s.status === "unavailable")
          .map(([id, s]) => `${labelOf(id)}: ${s.message ?? s.status}`),
        ...(list.scan.docker.status === "error" ? [`Docker: ${list.scan.docker.message ?? "error"}`] : []),
      ]
    : [];

  const push = (view: Sheet) => {
    if (view.kind === "add") setScanNote(null);
    setSheet((s) => [...s, view]);
  };
  const back = () => setSheet((s) => s.slice(0, -1));
  const closeSheet = () => {
    setSheet([]);
    setLogsFor(null);
  };

  // Open app: the link opens only when the proof is fresh at the moment of the click.
  const guardOpen = (phone: PhoneView | null, status: Status) => (e: MouseEvent<HTMLAnchorElement>) => {
    const at = nowS();
    if (status !== "running" || !phoneFresh(phone, at)) {
      e.preventDefault();
      setNow(at);
      toast.error("The phone link check is older than one minute. Use Check again.");
    }
  };

  const openRegister = (from: Server | null) => {
    closeSheet();
    setRegister({ open: true, from });
  };

  const choose = async (group: PreviewAppGroup, candidate: string) => {
    try {
      setList(await savePreviewApp(group.env, group.project_dir, candidate));
      closeSheet();
      toast.show(`${leaf(group.project)}: relay keeps this choice. It does not create a phone link.`);
    } catch (err) {
      toast.error(errText(err));
      load();
    }
  };

  const forget = async (group: PreviewAppGroup) => {
    try {
      setList(await clearPreviewApp(group.env, group.project_dir));
      closeSheet();
      toast.show(`${leaf(group.project)}: relay forgot the app choice.`);
    } catch (err) {
      toast.error(errText(err));
    }
  };

  const renderCard = (a: AppView) => {
    const id = `ap-why-${a.group.key.replace(/[^A-Za-z0-9_-]/g, "-")}`;
    const phone = a.server?.phone ?? null;
    const ready = a.status === "running" && phoneFresh(phone, now);
    const pill = pillOf(a.status);
    let access: ReactNode;
    if (ready) {
      access = (
        <p className="ap-hint" id={id}>
          Phone link checked {agoText(phone?.checked_at, now)}.
        </p>
      );
    } else if (a.status === "running" && a.server) {
      access = (
        <PhoneBlock
          id={id}
          server={a.server}
          env={labelOf(a.group.env)}
          now={now}
          compact
          scanning={scanning}
          onRecheck={() => void rescan()}
          onSetup={() => push({ kind: "setup", server: a.server!.key, title: leaf(a.group.project) })}
        />
      );
    } else {
      const reason =
        a.status === "starting"
          ? "Starting. relay waits for the health path."
          : a.status === "failed"
            ? "The last start failed. See Logs in Details."
            : a.status === "unknown"
              ? "relay could not check this workspace. Try the scan again."
              : !a.server && a.group.saved?.kind === "registered"
                ? "The registered app is no longer in this project. Use Change app or Forget choice in Details."
                : a.server?.registered
                  ? "Stopped. Start it from Details or a terminal."
                  : "Stopped. Start it from a terminal. relay keeps this choice.";
      access = (
        <p className="ap-hint" id={id}>
          {reason}
        </p>
      );
    }
    return (
      <article className="ap-card" key={a.group.key} data-group={a.group.key} data-status={a.status}>
        <div className="ap-head">
          <div>
            <h3>{leaf(a.group.project)}</h3>
            <div className="ap-meta">
              <span className="ap-ws">{labelOf(a.group.env)}</span>
            </div>
          </div>
          <span className={`pill ${pill.cls}`}>{pill.text}</span>
        </div>
        {access}
        <div className="ap-actions">
          {ready && phone?.url ? (
            <a
              className="btn dark open"
              href={phone.url}
              target="_blank"
              rel="noopener noreferrer"
              onClick={guardOpen(phone, a.status)}
            >
              <ExternalIcon />
              Open app
            </a>
          ) : (
            <button className="btn dark open" disabled aria-describedby={id}>
              <ExternalIcon />
              Open app
            </button>
          )}
          <button className="btn det" aria-haspopup="dialog" onClick={() => push({ kind: "details", group: a.group.key })}>
            Details
            <ChevronRightIcon />
          </button>
        </div>
      </article>
    );
  };

  const top = sheet[sheet.length - 1] ?? null;

  return (
    <section className="view-previews">
      <div className="page-head">
        <div>
          <h1>Previews</h1>
          <p>Your web apps, one per project.</p>
        </div>
        <button className="btn" aria-haspopup="dialog" onClick={() => push({ kind: "add" })}>
          <PlusIcon />
          Add app
        </button>
      </div>

      <div className="seg ap-filter" role="group" aria-label="Workspace filter">
        {[{ id: "all", label: "All" }, ...envs.map((e) => ({ id: e.id, label: e.label }))].map((t) => (
          <button key={t.id} aria-pressed={filter === t.id} onClick={() => setFilter(t.id)}>
            {t.label} <b>{counts(t.id)}</b>
          </button>
        ))}
      </div>

      {loadError ? (
        <div className="banner conflict" role="alert">
          <strong>relay cannot load the previews.</strong>
          <p>{loadError}</p>
          <div className="acts">
            <button className="btn sm" onClick={load}>
              Try again
            </button>
          </div>
        </div>
      ) : null}
      {scanIssues.length ? (
        <div className="banner note" role="status">
          <strong>Part of the scan did not finish.</strong>
          {scanIssues.map((t) => (
            <p key={t}>{t}</p>
          ))}
          <div className="acts">
            <button className="btn sm" onClick={() => void rescan()} disabled={scanning}>
              <RefreshIcon />
              {scanning ? "Scanning…" : "Try again"}
            </button>
          </div>
        </div>
      ) : null}

      {!list && !loadError ? <p className="pv-hint">Looking for web servers…</p> : null}

      {list && visibleCards.length === 0 && visiblePending.length === 0 ? (
        <div className="pv-empty">
          <div className="ico">
            <ServerIcon />
          </div>
          <h3>No apps in {where} yet</h3>
          <p>Start your web app in a terminal. relay finds it and shows it here, one app per project.</p>
          <button className="btn dark" aria-haspopup="dialog" onClick={() => push({ kind: "add" })}>
            Find apps
          </button>
        </div>
      ) : null}

      {list && (visibleCards.length > 0 || visiblePending.length > 0) ? (
        <>
          {visibleCards.length ? (
            <div className="pv-grid">{visibleCards.map(renderCard)}</div>
          ) : (
            <p className="ap-hint">No app chosen in {where} yet.</p>
          )}
          {visiblePending.length ? (
            <>
              <h2 className="group-h">
                Needs a choice <b>{visiblePending.length}</b>
              </h2>
              <div className="ap-pending">
                {visiblePending.map((a) => (
                  <div className="ap-choose" key={a.group.key} data-group={a.group.key}>
                    <div className="what">
                      <strong>{leaf(a.group.project)}</strong>
                      <small>
                        {labelOf(a.group.env)} · {a.group.candidates.length} possible{" "}
                        {a.group.candidates.length === 1 ? "app" : "apps"}. relay will not guess.
                      </small>
                    </div>
                    <button
                      className="btn acc-o"
                      aria-haspopup="dialog"
                      onClick={() => push({ kind: "choose", group: a.group.key })}
                    >
                      Choose app
                    </button>
                  </div>
                ))}
              </div>
            </>
          ) : null}
          <p className="ap-foot">Other servers, ports, and logs are in Details and Add app.</p>
        </>
      ) : null}

      {top ? (
        <Modal open onClose={closeSheet} titleId="ap-sheet-title" dismissLabel="Close">
          <SheetBody key={sheet.length} depth={sheet.length} onBack={back}>
            {top.kind === "add" ? (
              <AddApp
                list={list}
                apps={apps}
                cards={cards}
                pending={pending}
                others={others}
                servers={servers}
                labelOf={labelOf}
                rootOf={rootOf}
                now={now}
                scanning={scanning}
                scanNote={scanNote}
                scanIssues={scanIssues.length > 0}
                onChoose={(g) => push({ kind: "choose", group: g })}
                onServer={(k) => push({ kind: "server", server: k })}
                onRescan={async () => {
                  setScanNote("Scanning…");
                  const ok = await rescan();
                  setScanNote(ok ? "Scan done." : "The scan did not finish. Try again.");
                }}
                onRegister={() => openRegister(null)}
              />
            ) : top.kind === "choose" ? (
              <Chooser
                app={appByKey.get(top.group) ?? null}
                labelOf={labelOf}
                onCancel={back}
                onUse={(g, c) => choose(g, c)}
              />
            ) : top.kind === "setup" ? (
              <Setup
                server={serverByKey.get(top.server) ?? null}
                title={top.title}
                labelOf={labelOf}
                now={now}
                scanning={scanning}
                onRecheck={rescan}
                onDone={back}
              />
            ) : (
              <Details
                app={top.kind === "details" ? appByKey.get(top.group) ?? null : null}
                server={
                  top.kind === "details" ? appByKey.get(top.group)?.server ?? null : serverByKey.get(top.server) ?? null
                }
                missingTitle={top.kind === "details" ? "This app is no longer listed." : "This server is no longer listed."}
                labelOf={labelOf}
                rootOf={rootOf}
                now={now}
                scanAt={list?.scan.at ?? null}
                busy={busy}
                scanning={scanning}
                logsFor={logsFor}
                logsText={logsText}
                guardOpen={guardOpen}
                onRecheck={() => void rescan()}
                onSetup={(s, title) => push({ kind: "setup", server: s.key, title })}
                onChange={(g) => push({ kind: "choose", group: g.key })}
                onForget={(g) => void forget(g)}
                onStart={(p) => action(`${p.id}:start`, () => startPreview(p.id), "Starting the preview.")}
                onStop={(p) => action(`${p.id}:stop`, () => stopPreview(p.id), "Stopping the preview.")}
                onCheck={(p) => action(`${p.id}:check`, () => runPreviewCheck(p.id, ["phone", "desktop"]), "Running a check.")}
                onTest={(p) => action(`${p.id}:test`, () => runPreviewTest(p.id), "Running the tests.")}
                onRemove={(p) => action(`${p.id}:remove`, () => deletePreview(p.id), "Removed the registration.")}
                onLogs={(p) => void toggleLogs(p.id)}
                onRegister={(s) => openRegister(s)}
              />
            )}
          </SheetBody>
        </Modal>
      ) : null}

      {register.open ? (
        <RegisterDialog
          from={register.from}
          envs={envs}
          onClose={() => setRegister({ open: false, from: null })}
          onCreated={() => {
            setRegister({ open: false, from: null });
            load();
          }}
        />
      ) : null}
    </section>
  );
}

// The sheet frame: a Back button for a stacked view, focus on the new title.
// The frame mounts again for each view. React runs this effect before the
// Modal's effect, and the Modal records the focused element as the opener. The
// title focus waits one frame, so the Modal records the real opener first and
// returns focus to it when the sheet closes.
function SheetBody({ depth, onBack, children }: { depth: number; onBack: () => void; children: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const frame = window.requestAnimationFrame(() => ref.current?.querySelector<HTMLElement>("h2")?.focus());
    return () => window.cancelAnimationFrame(frame);
  }, []);
  return (
    <div className="modal-body dlg-body ap-dlg" ref={ref}>
      {depth > 1 ? (
        <button type="button" className="ap-back" aria-label="Back" onClick={onBack}>
          <BackIcon />
        </button>
      ) : null}
      {children}
    </div>
  );
}

function SheetTitle({ title, sub }: { title: string; sub?: ReactNode }) {
  return (
    <div className="ap-top">
      <h2 id="ap-sheet-title" tabIndex={-1}>
        {title}
      </h2>
      {sub ? <p className="ap-sub">{sub}</p> : null}
    </div>
  );
}

// The phone access state of a running server: link required, check expired, or not reachable.
function PhoneBlock({
  id,
  server,
  env,
  now,
  compact,
  scanning,
  onRecheck,
  onSetup,
}: {
  id?: string;
  server: Server;
  env: string;
  now: number;
  compact?: boolean;
  scanning: boolean;
  onRecheck: () => void;
  onSetup: () => void;
}) {
  const phone = server.phone;
  const port = server.registered?.port ?? server.detected?.port ?? 0;
  const hostPort = server.registered ? server.registered.host_port : server.detected?.host_port ?? null;
  if (phoneExpired(phone, now)) {
    return (
      <div className="ap-access none" id={id}>
        <PhoneIcon />
        <div className="what">
          <strong>Phone link check expired</strong>
          <small>relay opens an app only through a phone link that it checked in the last minute.</small>
        </div>
        <button className="btn sm" onClick={onRecheck} disabled={scanning}>
          <RefreshIcon />
          {scanning ? "Checking…" : "Check again"}
        </button>
      </div>
    );
  }
  if (phone?.status === "blocked" || !hostPort) {
    return (
      <div className="ap-access none" id={id}>
        <PhoneIcon />
        <div className="what">
          <strong>Phone link required</strong>
          <small>
            {env} does not publish port {port} to the laptop.
          </small>
        </div>
      </div>
    );
  }
  const text = compact
    ? phone?.status === "failed"
      ? "relay could not verify the phone link."
      : "This app has no approved phone link yet."
    : "relay opens an app on your phone only through a phone link that it checked in the last minute.";
  return (
    <div className="ap-access none" id={id}>
      <PhoneIcon />
      <div className="what">
        <strong>Phone link required</strong>
        <small>{text}</small>
        {!compact && phone?.status === "failed" && phone.message ? <small>{phone.message}</small> : null}
      </div>
      <button className="btn sm" aria-haspopup="dialog" onClick={onSetup}>
        Set up phone access
      </button>
    </div>
  );
}

interface DetailsProps {
  app: AppView | null;
  server: Server | null;
  missingTitle: string;
  labelOf: (id: string) => string;
  rootOf: (id: string) => string;
  now: number;
  scanAt: number | null;
  busy: Set<string>;
  scanning: boolean;
  logsFor: string | null;
  logsText: string;
  guardOpen: (phone: PhoneView | null, status: Status) => (e: MouseEvent<HTMLAnchorElement>) => void;
  onRecheck: () => void;
  onSetup: (s: Server, title: string) => void;
  onChange: (g: PreviewAppGroup) => void;
  onForget: (g: PreviewAppGroup) => void;
  onStart: (p: PreviewView) => void;
  onStop: (p: PreviewView) => void;
  onCheck: (p: PreviewView) => void;
  onTest: (p: PreviewView) => void;
  onRemove: (p: PreviewView) => void;
  onLogs: (p: PreviewView) => void;
  onRegister: (s: Server) => void;
}

// Details of a project app (with its app choice) or of one server from Add app.
function Details({ app, server, missingTitle, labelOf, rootOf, now, scanAt, busy, scanning, logsFor, logsText, guardOpen, ...on }: DetailsProps) {
  if (!app && !server) {
    return <SheetTitle title={missingTitle} sub="Rescan in Add app to see the current servers." />;
  }
  const env = app?.group.env ?? server!.env;
  const envLabel = labelOf(env);
  const root = rootOf(env);
  const p = server?.registered ?? null;
  const d = server?.detected ?? null;
  const project = app
    ? app.group.project
    : p
      ? projectOf(p.project_dir, root) ?? "Project unknown"
      : d?.project
        ? d.project
        : "Project unknown";
  const title = app || p || d?.project ? leaf(project) : project;
  const status: Status = app ? app.status : serverStatus(server!);
  const pill = pillOf(status);
  const managed = p?.source === "managed";
  const phone = server?.phone ?? null;
  const ready = status === "running" && phoneFresh(phone, now);

  // Phone
  let phoneSection: ReactNode;
  if (ready && phone?.url) {
    phoneSection = (
      <>
        <div className="ap-access ok">
          <CheckIcon />
          <div className="what">
            <strong>Phone link verified</strong>
            <small>Checked {agoText(phone.checked_at, now)} through the relay preview gateway.</small>
          </div>
        </div>
        <div className="ap-btn-row">
          <a className="btn dark" href={phone.url} target="_blank" rel="noopener noreferrer" onClick={guardOpen(phone, status)}>
            <ExternalIcon />
            Open app
          </a>
        </div>
      </>
    );
  } else if (status === "running" && server) {
    phoneSection = (
      <PhoneBlock
        server={server}
        env={envLabel}
        now={now}
        scanning={scanning}
        onRecheck={on.onRecheck}
        onSetup={() => on.onSetup(server, title)}
      />
    );
  } else {
    phoneSection = <p>The app is not running, so relay has no phone link to check.</p>;
  }

  // App choice (project details only)
  let appSection: ReactNode = null;
  if (app) {
    const g = app.group;
    const snap = g.saved?.snapshot ?? {};
    const label = app.cand
      ? candLabel(app.cand.tool, app.cand.kind, app.cand.name)
      : candLabel(snap.tool, g.saved?.kind, snap.name);
    const port = app.cand?.port ?? snap.port;
    const why =
      g.state === "suggested"
        ? `relay suggests this because it is the only clear web app in this project. Nothing is saved until you choose.`
        : g.saved?.available
          ? "Chosen by you. relay keeps this choice after a restart and never switches to another server on its own."
          : "Chosen by you. relay does not see this server now, so the app shows as stopped. relay does not switch to another server on its own.";
    appSection = (
      <div className="ap-dsec">
        <h3>App</h3>
        <p>
          <strong>{label}</strong>
          {port ? `, port ${port}` : ""}
        </p>
        <p>{why}</p>
        <div className="ap-btn-row">
          <button className="btn sm" aria-haspopup="dialog" onClick={() => on.onChange(g)} disabled={g.candidates.length === 0}>
            Change app
          </button>
          {g.state === "saved" ? (
            <button className="btn sm" onClick={() => on.onForget(g)}>
              Forget choice
            </button>
          ) : null}
        </div>
        {g.candidates.length === 0 ? <p className="ap-hint">relay sees no server in this project now.</p> : null}
      </div>
    );
  }

  // Controls: the original rules of the server cards.
  const actions: ReactNode[] = [];
  let hint = "";
  if (p) {
    const isBusy = (k: string) => busy.has(`${p.id}:${k}`);
    const st = server!.state;
    if (managed) {
      actions.push(
        <button key="stop" className="btn sm" disabled={isBusy("stop")} onClick={() => on.onStop(p)}>
          Stop
        </button>,
      );
    } else if (st === "stopped") {
      actions.push(
        <button key="start" className="btn sm" disabled={isBusy("start")} onClick={() => on.onStart(p)}>
          Start
        </button>,
      );
    }
    actions.push(
      <button
        key="check"
        className="btn sm"
        disabled={isBusy("check") || st === "starting" || p.last_check?.status === "running"}
        onClick={() => on.onCheck(p)}
      >
        {isBusy("check") || (p.last_check?.status === "running" && p.last_check.kind === "smoke") ? "Checking…" : "Run check"}
      </button>,
    );
    if (p.test_command)
      actions.push(
        <button key="test" className="btn sm" disabled={isBusy("test")} onClick={() => on.onTest(p)}>
          {isBusy("test") ? "Testing…" : "Run tests"}
        </button>,
      );
    actions.push(
      <button key="logs" className="btn sm" aria-expanded={logsFor === p.id} onClick={() => on.onLogs(p)}>
        {logsFor === p.id ? "Hide logs" : "Logs"}
      </button>,
    );
    actions.push(
      <button
        key="remove"
        className="btn sm danger"
        disabled={managed || isBusy("remove")}
        title={managed ? "Stop the server before you remove the preview." : undefined}
        onClick={() => on.onRemove(p)}
      >
        Remove
      </button>,
    );
    hint = managed
      ? "Stop the server before you remove the preview."
      : st === "running"
        ? "Started outside relay, so relay cannot stop it. Remove deletes the registration only."
        : "";
  } else if (d?.reachable) {
    actions.push(
      <button key="register" className="btn sm acc-o" onClick={() => on.onRegister(server!)}>
        Register app
      </button>,
    );
    hint = "Register it to get Run check, Run tests, Logs, and Remove. relay does not stop servers that it did not start.";
  } else if (d) {
    hint = `${envLabel} does not publish port ${d.port}. Only a published port can be registered.`;
  } else if (app?.group.saved && !app.server) {
    hint =
      app.group.saved.kind === "registered"
        ? "The registered app is not in this project now. Use Change app or Forget choice."
        : "Start the app from a terminal. relay finds it again when it runs with the same tool, port, and folder.";
  }

  // Technical facts
  const port = p ? p.port : d?.port ?? app?.group.saved?.snapshot.port ?? null;
  const hostPort = p ? p.host_port : d ? d.host_port : app?.group.saved?.snapshot.host_port ?? null;
  const process = d?.docker
    ? `${d.docker.name} (${d.docker.image.split(":")[0].split("/").pop()})`
    : d?.process
      ? `${d.process.name ?? "process"}${d.process.tool && d.process.tool !== d.process.name ? ` (${d.process.tool})` : ""}${
          d.process.started_at ? `, since ${clockTime(d.process.started_at)}` : ""
        }`
      : p && server!.state !== "stopped"
        ? managed
          ? "Started by relay"
          : "Owner not detected"
        : "None";
  const found = d?.docker
    ? `Docker, linked by its ${d.docker.link === "compose" ? "Compose project folder" : d.docker.link === "mount" ? "bind-mounted project folder" : "registered port"}`
    : d
      ? p
        ? "Registered, and the process scan"
        : "Process scan"
      : p
        ? "Registered"
        : "Saved choice only";
  const folder = p ? p.project_dir : d?.folder ?? (app ? app.group.project_dir : null);
  const loopback = location.hostname === "127.0.0.1" ? "localhost" : "127.0.0.1";
  const local = hostPort && server && status === "running" ? `http://${loopback}:${hostPort}` : null;

  return (
    <>
      <SheetTitle
        title={title}
        sub={
          <>
            {envLabel} · <span className={`pill ${pill.cls}`}>{pill.text}</span>
          </>
        }
      />
      <div className="ap-dsec first">
        <h3>Phone</h3>
        {phoneSection}
      </div>
      {appSection}
      <div className="ap-dsec">
        <h3>Controls</h3>
        {actions.length ? <div className="ap-btn-row">{actions}</div> : null}
        {hint ? <p className="ap-hint">{hint}</p> : null}
        {!actions.length && !hint ? <p className="ap-hint">No controls for this server.</p> : null}
        {p?.last_check ? <LastCheck check={p.last_check} /> : null}
        {p && logsFor === p.id ? (
          <pre className="logs">{logsText || "No logs yet. relay did not start the running server, so it has no output to show."}</pre>
        ) : null}
      </div>
      <details className="ap-tech ap-dsec">
        <summary>Technical details</summary>
        <dl className="ap-facts">
          <dt>Port</dt>
          <dd>{port ?? "Unknown"}</dd>
          <dt>Laptop port</dt>
          <dd>{hostPort ?? `Not published (${envLabel} does not publish this port)`}</dd>
          <dt>{d?.docker ? "Container" : "Process"}</dt>
          <dd>{process}</dd>
          <dt>Found by</dt>
          <dd>{found}</dd>
          <dt>Registered</dt>
          <dd>{p ? (managed ? `Yes, “${p.name}”, relay started it` : `Yes, “${p.name}”`) : "No"}</dd>
          <dt>Folder</dt>
          <dd>{folder ?? "Unknown"}</dd>
          {app ? (
            <>
              <dt>Project</dt>
              <dd>{app.group.project_dir}</dd>
            </>
          ) : null}
          <dt>Last scan</dt>
          <dd>{scanAt ? agoText(scanAt, now) : "Not yet"}</dd>
        </dl>
        {local ? (
          <div className="ap-btn-row">
            <a className="btn sm ap-laptop" href={local} target="_blank" rel="noopener noreferrer">
              <ExternalIcon />
              Open on laptop
            </a>
          </div>
        ) : null}
        <p className="ap-hint">The laptop link stays laptop only. It does not work on a phone.</p>
      </details>
    </>
  );
}

function candRow(c: PreviewAppCandidate, project: string, env: string, current: string | null, selected: string | null, onPick: (id: string) => void) {
  const note =
    c.kind === "registered" && !c.frontend ? "registered preview" : c.frontend ? "likely web app" : "type unknown, may have no UI";
  return (
    <label className="ap-cand" key={c.id}>
      <input type="radio" name="ap-cand" value={c.id} checked={selected === c.id} onChange={() => onPick(c.id)} />
      <span className="t">
        <strong>
          {candLabel(c.tool, c.kind, c.name)} · port {c.port}
        </strong>
        <small>
          {project} · {env} · {note}
          {c.status !== "running" ? ` · ${c.status}` : ""}
        </small>
        {current === c.id ? <span className="now">Current app</span> : null}
      </span>
    </label>
  );
}

function Chooser({
  app,
  labelOf,
  onCancel,
  onUse,
}: {
  app: AppView | null;
  labelOf: (id: string) => string;
  onCancel: () => void;
  onUse: (g: PreviewAppGroup, candidate: string) => Promise<void>;
}) {
  const [selected, setSelected] = useState<string | null>(app?.group.app ?? null);
  const [saving, setSaving] = useState(false);
  if (!app) return <SheetTitle title="This project is no longer listed." sub="Rescan in Add app to see the current servers." />;
  const g = app.group;
  const env = labelOf(g.env);
  const likelyList = g.candidates.filter(likely);
  const otherList = g.candidates.filter((c) => !likely(c));
  const n = g.candidates.length;
  return (
    <>
      <SheetTitle
        title="Choose an app"
        sub={`${g.project} · ${env} · relay found ${n} ${n === 1 ? "server" : "servers"} in this project.`}
      />
      <form
        className="ap-choose-form"
        onSubmit={async (e) => {
          e.preventDefault();
          if (!selected || saving) return;
          setSaving(true);
          await onUse(g, selected);
          setSaving(false);
        }}
      >
        {likelyList.length ? (
          <fieldset className="ap-cand-list">
            <legend>Likely apps</legend>
            {likelyList.map((c) => candRow(c, leaf(g.project), env, g.app, selected, setSelected))}
          </fieldset>
        ) : null}
        {otherList.length ? (
          <details className="ap-more" open={likelyList.length === 0}>
            <summary>
              Other servers ({otherList.length}), may have no UI
            </summary>
            <div className="ap-cand-list">
              {otherList.map((c) => candRow(c, leaf(g.project), env, g.app, selected, setSelected))}
            </div>
          </details>
        ) : null}
        {n === 0 ? <p className="ap-note">relay sees no server in this project now. Start the app, then rescan.</p> : null}
        <p className="ap-note">
          relay keeps your choice. After a restart it never switches to another server. Choosing an app does not create a
          phone link.
        </p>
        <div className="dlg-actions">
          <button type="button" className="btn" onClick={onCancel}>
            Cancel
          </button>
          <button type="submit" className="btn dark" disabled={!selected || saving}>
            {saving ? "Saving…" : "Use this app"}
          </button>
        </div>
      </form>
    </>
  );
}

function AddApp({
  list,
  apps,
  cards,
  pending,
  others,
  servers,
  labelOf,
  rootOf,
  now,
  scanning,
  scanNote,
  scanIssues,
  onChoose,
  onServer,
  onRescan,
  onRegister,
}: {
  list: PreviewsList | null;
  apps: AppView[];
  cards: AppView[];
  pending: AppView[];
  others: AppView[];
  servers: Server[];
  labelOf: (id: string) => string;
  rootOf: (id: string) => string;
  now: number;
  scanning: boolean;
  scanNote: string | null;
  scanIssues: boolean;
  onChoose: (groupKey: string) => void;
  onServer: (serverKey: string) => void;
  onRescan: () => Promise<void>;
  onRegister: () => void;
}) {
  const sub = scanning
    ? "Scanning…"
    : list
      ? `Scanned ${agoText(list.scan.at, now)}.${scanIssues ? " Part of the scan did not finish." : ""}`
      : "Looking for web servers…";
  const pickRow = (a: AppView, label: string, button: string) => (
    <div className="ap-pick" key={a.group.key}>
      <div className="t">
        <strong>{leaf(a.group.project)}</strong>
        <small>
          {labelOf(a.group.env)} · {label}
        </small>
      </div>
      <button className={`btn sm${button === "Change" ? "" : " acc-o"}`} onClick={() => onChoose(a.group.key)}>
        {button}
      </button>
    </div>
  );
  const appLabel = (a: AppView) => {
    if (a.cand) return `${candLabel(a.cand.tool, a.cand.kind, a.cand.name)}, port ${a.cand.port}`;
    const snap = a.group.saved?.snapshot ?? {};
    return `${candLabel(snap.tool, a.group.saved?.kind, snap.name)}${snap.port ? `, port ${snap.port}` : ""}, stopped`;
  };
  const serverRow = (s: Server) => {
    const p = s.registered;
    const d = s.detected;
    const project = p ? projectOf(p.project_dir, rootOf(s.env)) : d?.project ? d.project : null;
    const tool = d?.process?.tool ?? null;
    const what = d?.docker ? `Docker ${d.docker.name}` : tool ? TOOL_LABEL[tool] ?? tool : d?.process?.name ?? (p ? `“${p.name}”` : "Server");
    const port = p ? p.port : d?.port;
    return (
      <div className="ap-pick" key={s.key}>
        <div className="t">
          <strong className={project ? undefined : "unknown"}>{project ?? "Project unknown"}</strong>
          <small>
            {labelOf(s.env)} · {what} · port {port} · {pillOf(serverStatus(s)).text}
          </small>
        </div>
        <button className="btn sm" aria-haspopup="dialog" onClick={() => onServer(s.key)}>
          Details
        </button>
      </div>
    );
  };
  const empty = list != null && apps.length === 0 && servers.length === 0;
  return (
    <>
      <SheetTitle title="Add app" sub={sub} />
      {empty ? (
        <p className="ap-note">
          relay found no web servers that a process inside a work container owns. Start your app in a terminal, then
          rescan.
        </p>
      ) : null}
      {pending.length ? (
        <div className="ap-dsec first">
          <h3>Needs a choice</h3>
          {pending.map((a) =>
            pickRow(a, `${a.group.candidates.length} possible ${a.group.candidates.length === 1 ? "app" : "apps"}`, "Choose"),
          )}
        </div>
      ) : null}
      {cards.length ? (
        <div className={`ap-dsec${pending.length ? "" : " first"}`}>
          <h3>Projects with an app</h3>
          {cards.map((a) => pickRow(a, appLabel(a), "Change"))}
        </div>
      ) : null}
      {others.length ? (
        <details className="ap-more ap-dsec">
          <summary>Other projects ({others.length}), servers may have no UI</summary>
          {others.map((a) =>
            pickRow(a, `${a.group.candidates.length} ${a.group.candidates.length === 1 ? "server" : "servers"} of unknown type`, "Choose"),
          )}
        </details>
      ) : null}
      <div className="ap-dsec">
        <h3>Not listed?</h3>
        <div className="ap-btn-row">
          <button className="btn sm" onClick={() => void onRescan()} disabled={scanning}>
            <RefreshIcon />
            Rescan
          </button>
          <button className="btn sm" onClick={onRegister}>
            Register by command
          </button>
        </div>
        <p className="ap-hint" aria-live="polite">
          {scanNote ?? "Register by command opens the Register preview form."}
        </p>
      </div>
      {servers.length ? (
        <details className="ap-tech ap-dsec">
          <summary>All servers (technical, {servers.length})</summary>
          {servers.map(serverRow)}
          <p className="ap-hint">
            Not listed: sockets that no process inside a work container owns, Docker containers without a verified
            workspace project folder, and ports that do not answer HTTP or HTTPS, such as databases. Work 2 shares the
            laptop network, so relay itself and other laptop services also listen there.
          </p>
        </details>
      ) : null}
    </>
  );
}

function Setup({
  server,
  title,
  labelOf,
  now,
  scanning,
  onRecheck,
  onDone,
}: {
  server: Server | null;
  title: string;
  labelOf: (id: string) => string;
  now: number;
  scanning: boolean;
  onRecheck: () => Promise<boolean>;
  onDone: () => void;
}) {
  const [checked, setChecked] = useState(false);
  const phone = server?.phone ?? null;
  const env = server ? labelOf(server.env) : "";
  let status: string;
  if (scanning) status = "Checking…";
  else if (!server) status = "relay does not see this server now.";
  else if (phone?.status === "blocked") status = `${env} does not publish this port to the laptop, so no phone link can reach it.`;
  else if (phoneFresh(phone, now)) status = `Phone link verified ${agoText(phone?.checked_at, now)}.`;
  else if (phoneExpired(phone, now)) status = "The last phone link check is older than one minute.";
  else if (phone?.status === "failed")
    status = `relay checked the phone link and it did not work${phone.message ? `: ${phone.message}` : "."}`;
  else status = "No phone link for this app yet.";
  if (checked && !scanning) status += " relay checked again just now.";
  return (
    <>
      <SheetTitle title="Set up phone access" sub={`${title}${env ? ` · ${env}` : ""}`} />
      <p>
        This app needs an approved phone link before Open app works on your phone. Phone access is separate from the app
        choice.
      </p>
      <ol className="ap-steps">
        <li>On the laptop, add a trusted preview gateway for this app and restart relay. This step opens no remote access.</li>
        <li>After your approval, serve the gateway port, not the app port, on your private network.</li>
        <li>relay checks the route. Open app turns on only after a check in the last minute.</li>
      </ol>
      <p className="ap-hint">The steps are in relay's operations guide, under phone access to a preview.</p>
      <div className="ap-status" aria-live="polite">
        {status}
      </div>
      <div className="dlg-actions">
        <button
          className="btn"
          disabled={scanning}
          onClick={async () => {
            setChecked(false);
            setChecked(await onRecheck());
          }}
        >
          <RefreshIcon />
          Check again
        </button>
        <button className="btn dark" onClick={onDone}>
          Done
        </button>
      </div>
    </>
  );
}

function LastCheck({ check }: { check: CheckRunView }) {
  const phone = check.artifacts.find((a) => a.name.includes("phone"));
  const desktop = check.artifacts.find((a) => a.name.includes("desktop"));
  const label = check.kind === "test_command" ? "Tests" : "Check";
  return (
    <div className="pv-check">
      <span className={check.status === "running" ? "run" : check.status === "passed" ? "ok" : "bad"}>
        {label}: {check.status}
      </span>
      <span>
        {clockTime(check.started_at)}
        {check.summary ? ` · ${check.summary}` : ""}
      </span>
      {check.target_url ? <span className="target">Target: {check.target_url}</span> : null}
      {phone || desktop ? (
        <span className="thumbs" aria-label="Phone and desktop screenshots">
          {desktop ? <img className="d" src={desktop.url} alt="Desktop screenshot" /> : null}
          {phone ? <img className="p" src={phone.url} alt="Phone screenshot" /> : null}
        </span>
      ) : null}
    </div>
  );
}

function suggestCommand(d: DetectedEndpoint | null): string {
  const tool = d?.process?.tool;
  if (!d || !tool) return "";
  if (["vite", "next", "nuxt", "astro", "remix", "svelte-kit", "webpack", "webpack-dev-server", "parcel", "storybook"].includes(tool))
    return "npm run dev";
  if (tool === "http.server") return `python3 -m http.server ${d.port}`;
  return "";
}

// Mounted only while open, so the first render already shows the prefilled values.
function RegisterDialog({
  from,
  envs,
  onClose,
  onCreated,
}: {
  from: Server | null;
  envs: EnvView[];
  onClose: () => void;
  onCreated: () => void;
}) {
  const toast = useToast();
  const [form, setForm] = useState<PreviewInput>(() => {
    const d = from?.detected ?? null;
    const project = d?.project ?? "";
    return {
      env: from?.env ?? envs.find((e) => e.available)?.id ?? envs[0]?.id ?? "",
      name: (project.split("/").pop() || "").replace(/[^A-Za-z0-9 _-]/g, "-").slice(0, 40),
      project,
      command: suggestCommand(d),
      port: d?.port ?? 3000,
      health_path: "/",
      test_command: "",
    };
  });
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);

  const set = <K extends keyof PreviewInput>(key: K, value: PreviewInput[K]) => {
    setForm((f) => ({ ...f, [key]: value }));
    setErrors((e) => {
      const { [key as string]: _drop, ...rest } = e;
      void _drop;
      return rest;
    });
  };

  const submit = async () => {
    setBusy(true);
    setErrors({});
    try {
      await createPreview({
        ...form,
        name: form.name.trim(),
        project: form.project.trim(),
        command: form.command.trim(),
        test_command: form.test_command?.trim() || undefined,
      });
      toast.show("Preview registered.");
      onCreated();
    } catch (err) {
      if (err instanceof ApiError) {
        const details = (err.body?.details as Array<{ field?: string; message?: string }>) ?? [];
        const map: Record<string, string> = {};
        for (const d of details) if (d.field) map[d.field] = d.message ?? "Invalid value.";
        const field = { invalid_name: "name", duplicate_name: "name", invalid_project: "project", invalid_command: "command", invalid_port: "port", port_not_published: "port", invalid_health_path: "health_path", invalid_test_command: "test_command" }[err.code];
        if (field) map[field] = err.message;
        if (Object.keys(map).length) setErrors(map);
        else toast.error(err.message);
      } else {
        toast.error("Cannot reach relay.");
      }
    } finally {
      setBusy(false);
    }
  };

  const field = (key: keyof PreviewInput, label: string, props: React.InputHTMLAttributes<HTMLInputElement> = {}, optional = false) => (
    <div className="form-field">
      <label htmlFor={`pv-${key}`}>
        {label} {optional ? <span>· optional</span> : null}
      </label>
      <input
        id={`pv-${key}`}
        value={String(form[key] ?? "")}
        onChange={(e) => set(key, (props.type === "number" ? Number(e.target.value) : e.target.value) as never)}
        aria-invalid={errors[key] ? true : undefined}
        autoComplete="off"
        {...props}
      />
      {errors[key] ? <div className="field-error">{errors[key]}</div> : null}
    </div>
  );

  return (
    <Modal open onClose={onClose} titleId="pv-register-title" dismissLabel="Close the register dialog">
      <form
        className="modal-body dlg-body ap-dlg"
        noValidate
        onSubmit={(e) => {
          e.preventDefault();
          if (!busy) void submit();
        }}
      >
        <h2 id="pv-register-title">Register a preview</h2>
        <p>
          {from?.detected
            ? `Prefilled from the detected server on port ${from.detected.port}. A registered preview gets Run check, Run tests, Logs, and Remove.`
            : "A registered preview gets Start, Stop, Run check, Run tests, Logs, and Remove."}
        </p>
        <div className="form-row">
          <div className="form-field">
            <label htmlFor="pv-env">Workspace</label>
            <select id="pv-env" value={form.env} onChange={(e) => set("env", e.target.value)}>
              {envs.map((e) => (
                <option key={e.id} value={e.id} disabled={!e.available}>
                  {e.label}
                </option>
              ))}
            </select>
          </div>
          {field("name", "Name")}
        </div>
        {field("project", "Project folder", { placeholder: "garden-planner" })}
        {field("command", "Command", { placeholder: "npm run dev" })}
        <div className="form-row">
          {field("port", "Port", { type: "number", inputMode: "numeric" })}
          {field("health_path", "Health path")}
        </div>
        {field("test_command", "Test command", { placeholder: "npm test" }, true)}
        <div className="dlg-actions">
          <button type="button" className="btn" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className="btn dark" disabled={busy}>
            {busy ? "Registering…" : "Register preview"}
          </button>
        </div>
      </form>
    </Modal>
  );
}
