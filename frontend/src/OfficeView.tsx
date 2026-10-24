import { useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { BackIcon, ChevronRightIcon, KindGlyph, PlusIcon } from "./icons";
import { projectName, workspaceGroups } from "./sessionNavigation";
import type { EnvView, SessionView } from "./types";
import { DESKS_PER_PAGE, OFFICE_STALE_SECONDS, OFFICE_STATE_LABELS, officeSessions, officeState, reconcileDesks, type OfficeMemory, type OfficeState } from "./officeState";
import { DESK_POSITIONS, paintOffice, type OfficeSprite } from "./officePainter";
import artNotice from "./officeArt.LICENSE.txt?url&no-inline";

interface Props {
  envs: EnvView[];
  sessions: SessionView[];
  connected: boolean;
  generatedAt: number | null;
  visible: boolean;
  obscured: boolean;
  memory: OfficeMemory;
  viewControl: ReactNode;
  onSelect: (id: string) => void;
  onNew: (env: string) => void;
  onRefresh: () => void;
}

function MotionIcon({ paused }: { paused: boolean }) {
  return <svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="1.6">
    {paused ? <path d="m8 4 12 8-12 8z" /> : <path d="M8 5v14M16 5v14" />}
  </svg>;
}

function useMedia(query: string) {
  const [matches, setMatches] = useState(() => matchMedia(query).matches);
  useEffect(() => {
    const mq = matchMedia(query);
    const update = () => setMatches(mq.matches);
    mq.addEventListener("change", update);
    return () => mq.removeEventListener("change", update);
  }, [query]);
  return matches;
}

function OfficeCanvas({ env, agents, active, animate }: { env: string; agents: OfficeSprite[]; active: boolean; animate: boolean }) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const [onScreen, setOnScreen] = useState(false);
  useEffect(() => {
    const node = canvas.current;
    if (!node) return;
    const observer = new IntersectionObserver(([entry]) => setOnScreen(entry.isIntersecting));
    observer.observe(node);
    return () => observer.disconnect();
  }, []);
  useEffect(() => {
    const node = canvas.current;
    if (!node || !active) return;
    let frame = 0, last = -Infinity;
    paintOffice(node, env, agents, 0);
    const tick = (time: number) => {
      if (time - last >= 1000 / 12) { paintOffice(node, env, agents, time); last = time; }
      frame = requestAnimationFrame(tick);
    };
    if (animate && onScreen && agents.some((agent) => agent.state === "working")) frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [env, agents, active, animate, onScreen]);
  return <canvas ref={canvas} width="640" height="408" aria-hidden="true" />;
}

export default function OfficeView({ envs, sessions, connected, generatedAt, visible, obscured, memory, viewControl, onSelect, onNew, onRefresh }: Props) {
  const displayed = useMemo(() => officeSessions(sessions, envs, memory.lastKnown), [sessions, envs, memory]);
  const groups = workspaceGroups(envs, displayed);
  const [activeEnv, setActiveEnv] = useState(() => memory.workspace ?? groups[0]?.id ?? "work1");
  const [pages, setPages] = useState(memory.pages);
  const [slots, setSlots] = useState(() => reconcileDesks(memory.slots, displayed));
  const [paused, setPaused] = useState(memory.paused);
  const [hidden, setHidden] = useState(document.hidden);
  const [now, setNow] = useState(() => Date.now() / 1000);
  const phone = useMedia("(max-width: 760px)");
  const reduced = useMedia("(prefers-reduced-motion: reduce)");
  const root = useRef<HTMLElement>(null);
  const identity = displayed.map((s) => `${s.env}:${s.id}`).sort().join("|");

  useEffect(() => { setSlots((current) => reconcileDesks(current, displayed)); }, [identity]); // state-only updates keep every desk
  useEffect(() => {
    for (const env of envs) if (env.available) memory.lastKnown[env.id] = sessions.filter((s) => s.env === env.id && s.status === "live");
  }, [envs, sessions, memory]);
  useEffect(() => { memory.slots = slots; }, [slots, memory]);
  useEffect(() => { memory.pages = pages; }, [pages, memory]);
  useEffect(() => { memory.workspace = activeEnv; }, [activeEnv, memory]);
  useEffect(() => { memory.paused = paused; }, [paused, memory]);
  useEffect(() => {
    const update = () => setHidden(document.hidden);
    document.addEventListener("visibilitychange", update);
    return () => document.removeEventListener("visibilitychange", update);
  }, []);
  useEffect(() => {
    if (!visible || hidden || obscured) return;
    setNow(Date.now() / 1000);
    // A quiet stream can stay healthy without a changed event. Refresh the displayed snapshot too.
    onRefresh();
    const clock = window.setInterval(() => setNow(Date.now() / 1000), 5000);
    const refresh = window.setInterval(onRefresh, 15000);
    return () => { clearInterval(clock); clearInterval(refresh); };
  }, [visible, hidden, obscured, onRefresh]);
  useLayoutEffect(() => {
    if (!visible || !memory.focusId) return;
    const session = sessions.find((s) => s.id === memory.focusId);
    if (!session) return;
    const index = (slots[session.env] ?? []).indexOf(session.id);
    if (index < 0) return;
    setActiveEnv(session.env);
    setPages((current) => ({ ...current, [session.env]: Math.floor(index / DESKS_PER_PAGE) }));
    const id = session.id;
    memory.focusId = null;
    const frame = requestAnimationFrame(() => {
      root.current?.querySelectorAll<HTMLButtonElement>(".office-agent-row").forEach((button) => {
        if (button.dataset.sessionId === id && !button.disabled) button.focus({ preventScroll: true });
      });
    });
    return () => cancelAnimationFrame(frame);
  }, [visible]);
  const chosenEnv = groups.some((g) => g.id === activeEnv) ? activeEnv : groups[0]?.id;
  const fresh = connected && generatedAt != null && now - generatedAt <= OFFICE_STALE_SECONDS && generatedAt - now < 60;
  const byId = useMemo(() => new Map(displayed.map((s) => [s.id, s])), [displayed]);
  const available = (env: string) => envs.some((e) => e.id === env && e.available);
  const stateOf = (s: SessionView) => officeState(s, available(s.env), fresh);
  const live = [...byId.values()];
  const count = (state: OfficeState) => live.filter((s) => stateOf(s) === state).length;
  const choose = (id: string) => {
    const s = byId.get(id);
    if (!s || !available(s.env)) return;
    memory.focusId = id;
    memory.workspace = s.env;
    setActiveEnv(s.env);
    onSelect(id);
  };
  const status = (s: SessionView) => <span className={`office-status ${stateOf(s)}`}><span className={`office-dot ${stateOf(s)}`} />{OFFICE_STATE_LABELS[stateOf(s)]}</span>;

  return <section className="office-view" ref={root} aria-label="Agent office" hidden={!visible}>
    <div className="office-heading">
      <div><p className="office-eyebrow">A place for every agent</p><h1>Your office<span>.</span></h1><p>See who’s at work, who needs you, and where they belong.</p></div>
      <div className="office-actions">
        <button className="button office-motion" aria-label={paused ? "Resume motion" : "Pause motion"} aria-pressed={paused} onClick={() => setPaused(!paused)}>
          <MotionIcon paused={paused} /><span>{paused ? "Resume" : "Pause"} motion</span>
        </button>
        <button className="button primary office-new" onClick={() => onNew(chosenEnv ?? "work1")}><PlusIcon />New session</button>
      </div>
    </div>
    <div className="office-toolbar">{viewControl}<div className="office-summary" aria-label="Office summary">
      {fresh ? <><span><i className="office-dot working" />{count("working")} working</span><span><i className="office-dot needs_input" />{count("needs_input")} need you</span><span><i className="office-dot idle" />{count("idle")} idle</span></> : <span role="status">Updates paused. Status may be out of date.</span>}
    </div></div>
    <div className="office-workspaces" role="group" aria-label="Office workspace">
      {groups.map((env) => <button key={env.id} aria-pressed={chosenEnv === env.id} onClick={() => setActiveEnv(env.id)}>
        <span className={`workspace-dot ${env.id}`} />{env.label}<span className="office-count">{live.filter((s) => s.env === env.id).length}</span>
        {!available(env.id) ? <span className="office-count">Offline</span> : fresh && live.some((s) => s.env === env.id && stateOf(s) === "needs_input") ? <span className="attention-count">{live.filter((s) => s.env === env.id && stateOf(s) === "needs_input").length}</span> : null}
      </button>)}
    </div>
    <div className="office-floors">
      {groups.map((env, number) => {
        const envSlots = slots[env.id] ?? [];
        const page = Math.min(pages[env.id] ?? 0, Math.max(0, Math.ceil(envSlots.length / DESKS_PER_PAGE) - 1));
        const rows = envSlots.slice(page * DESKS_PER_PAGE, (page + 1) * DESKS_PER_PAGE).map((id, slot) => ({ session: id ? byId.get(id) : undefined, slot })).filter((item): item is { session: SessionView; slot: number } => item.session != null);
        const total = live.filter((s) => s.env === env.id).length;
        const online = available(env.id);
        const needs = live.filter((s) => s.env === env.id && stateOf(s) === "needs_input").length;
        const sprites = rows.map(({ session, slot }) => ({ id: session.id, slot, state: stateOf(session) }));
        return <section key={env.id} className={`office-floor${chosenEnv === env.id ? " chosen" : ""}`} aria-label={`${env.label} office`} data-env={env.id}>
          <div className="office-floor-head"><h2><span className={`workspace-dot ${env.id}`} />{env.label}<span className="office-floor-number">F{String(number + 1).padStart(2, "0")}</span></h2><span className="office-floor-presence"><i className={`office-dot ${online && fresh ? "working" : "unknown"}`} />{online ? `${total} sessions` : "Workspace offline"}</span></div>
          <div className={`office-scene${online ? "" : " offline"}${fresh ? "" : " stale"}`}>
            <OfficeCanvas env={env.id} agents={sprites} active={visible && !hidden && (!phone || chosenEnv === env.id)} animate={!paused && !reduced && !obscured && fresh && online} />
            <div className="office-scene-name">{env.label}</div><span className="office-scene-brand">RELAY / F{String(number + 1).padStart(2, "0")}</span>
            {online ? rows.map(({ session: s, slot }) => <button key={s.id} className="office-desk" data-session-id={s.id} data-desk={slot + page * DESKS_PER_PAGE} data-state={stateOf(s)}
              style={{ left: `${DESK_POSITIONS[slot].x / 640 * 100}%`, top: `${(DESK_POSITIONS[slot].y - 9) / 408 * 100}%` }}
              aria-label={`Open ${env.label}, ${s.name}, ${s.kind}, ${projectName(s)}, ${OFFICE_STATE_LABELS[stateOf(s)]}`} onClick={() => choose(s.id)}>
              {stateOf(s) === "needs_input" ? <span className="office-desk-marker">!</span> : stateOf(s) === "idle" ? <span className="office-desk-marker idle">z z</span> : stateOf(s) === "unknown" ? <span className="office-desk-marker unknown">?</span> : null}
              <span className="office-desk-tag"><span>{s.kind === "claude" ? "cc" : s.kind === "codex" ? "cx" : s.kind === "shell" ? "sh" : "•"}</span>{s.name}</span>
            </button>) : null}
            <span className="office-scene-note">{online ? fresh ? "Your agents, in one place" : "Updates paused" : "Last known office"}</span>
            {!online || !total ? <div className="office-scene-overlay"><strong>{online ? "Room for your next idea" : `${env.label} is offline`}</strong><p>{online ? `No live sessions in ${env.label}. A new session will get a desk here.` : "The workspace needs to reconnect before you can open its agents."}</p>{online ? <button className="button" onClick={() => onNew(env.id)}><PlusIcon />New session</button> : null}</div> : null}
          </div>
          <div className="office-floor-tools"><span>{online ? "On this floor" : "Last known sessions"}</span>
            {envSlots.length > DESKS_PER_PAGE ? <div className="office-pagination"><span>{page + 1} / {Math.ceil(envSlots.length / DESKS_PER_PAGE)}</span>
              <button aria-label={`Previous ${env.label} desks`} disabled={!page} onClick={() => setPages({ ...pages, [env.id]: page - 1 })}><BackIcon /></button>
              <button aria-label={`Next ${env.label} desks`} disabled={(page + 1) * DESKS_PER_PAGE >= envSlots.length} onClick={() => setPages({ ...pages, [env.id]: page + 1 })}><ChevronRightIcon /></button>
            </div> : <span className={needs ? "office-attention" : ""}>{needs ? `${needs} ${needs === 1 ? "agent needs" : "agents need"} you` : !online ? "Offline" : !fresh ? "Updates paused" : "All set"}</span>}
          </div>
          <div className="office-agent-list">{rows.map(({ session: s }) => <button className="office-agent-row" key={s.id} data-session-id={s.id} disabled={!online}
            aria-label={`Open ${env.label}, ${s.name}, ${s.kind}, ${projectName(s)}, ${OFFICE_STATE_LABELS[stateOf(s)]}`} onClick={() => choose(s.id)}>
            <span className={`agent-glyph ${s.kind}`}><KindGlyph kind={s.kind} /></span><span className="office-agent-copy"><span className="office-agent-name">{s.name}<small>{s.kind === "claude" ? "Claude Code" : s.kind === "codex" ? "Codex" : s.kind === "shell" ? "Shell" : "Agent"}</small></span><span className="office-agent-project">{projectName(s)} <span>/</span> {env.label}</span></span>
            {status(s)}<ChevronRightIcon />
          </button>)}{!total ? <p className="office-empty">{online ? "Start a session to bring this floor to life." : "No live sessions are available."}</p> : null}</div>
        </section>;
      })}
    </div>
    {!groups.length ? <p className="office-empty">Loading workspaces…</p> : null}
    <footer className="office-footer"><span>Choose a desk or an agent to open its terminal.</span><span>Pixel art from <a href="https://github.com/pixtuoid/pixtuoid" target="_blank" rel="noreferrer">pixtuoid</a> · <a href={artNotice} target="_blank" rel="noreferrer">License</a></span></footer>
  </section>;
}
