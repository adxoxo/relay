import { useEffect, useRef, useState } from "react";
import type { EnvView, SessionView } from "./types";
import { CheckIcon, CloseIcon, GridIcon, KindGlyph, PlusIcon } from "./icons";
import { cardTitle, displayProjectPath, kindLabel, stateClass, stateLabel } from "./util";
import { byAgentName, needsAttention, projectName, workspaceGroups } from "./sessionNavigation";

export function WorkspacePicker({ envs, sessions, value, onChange, all = false }: {
  envs: EnvView[]; sessions: SessionView[]; value: string;
  onChange: (id: string) => void; all?: boolean;
}) {
  const groups = workspaceGroups(envs, sessions);
  const live = sessions.filter((s) => s.status === "live");
  return (
    <div className="workspace-picker" role="group" aria-label="Work environment">
      {all ? <button className="workspace-pill" aria-pressed={value === "all"} onClick={() => onChange("all")}>
        All agents <span className="workspace-count">{live.length}</span>
      </button> : null}
      {groups.map((env) => {
        const rows = live.filter((s) => s.env === env.id);
        const requests = rows.filter(needsAttention).length;
        const unavailable = envs.find((e) => e.id === env.id)?.available === false;
        return <button key={env.id} className="workspace-pill" aria-pressed={value === env.id}
          aria-label={`${env.label}, ${rows.length} sessions${requests ? `, ${requests} need input` : ""}${unavailable ? ", unavailable" : ""}`}
          onClick={() => onChange(env.id)}>
          <span className={`workspace-dot ${env.id}`} aria-hidden="true" />
          <span className="workspace-label">{env.label}</span>
          <span className="workspace-count">{rows.length}</span>
          {requests ? <span className="attention-count" aria-hidden="true">{requests}</span> : null}
          {unavailable ? <span className="workspace-unavailable" aria-hidden="true">Offline</span> : null}
        </button>;
      })}
    </div>
  );
}

export function DirectoryButton({ sessions, onClick }: { sessions: SessionView[]; onClick: (animate: boolean) => void }) {
  const requests = sessions.filter(needsAttention).length;
  return <button className="directory-button icon-button" aria-label={`All agents, grouped by workspace${requests ? `, ${requests} need input` : ""}`}
    title="All agents (Ctrl or Command K)" onClick={(e) => { e.currentTarget.focus({ preventScroll: true }); onClick(e.detail !== 0); }}>
    <GridIcon />{requests ? <span className="attention-count" aria-hidden="true">{requests}</span> : null}
  </button>;
}

export function SessionSidebar({ envs, sessions, selectedId, secondId, onSelect }: {
  envs: EnvView[]; sessions: SessionView[]; selectedId: string | null; secondId: string | null;
  onSelect: (id: string) => void;
}) {
  const rows = sessions.filter((s) => s.status === "live" || s.id === selectedId || s.id === secondId);
  return <nav className="sidebar-sessions" aria-label="Sessions by workspace">
    {workspaceGroups(envs, rows).map((env) => {
      const group = rows.filter((s) => s.env === env.id).sort(byAgentName);
      const unavailable = envs.find((e) => e.id === env.id)?.available === false;
      return <section className="sidebar-group" key={env.id}>
        <h2 className="workspace-group-heading"><span className={`workspace-dot ${env.id}`} />{env.label}
          <span>{group.filter((s) => s.status === "live").length}</span></h2>
        {unavailable ? <p className="sidebar-empty">Workspace unavailable</p> : null}
        {!unavailable && !group.length ? <p className="sidebar-empty">No live sessions</p> : null}
        {group.map((s) => <button key={s.id} className={`sidebar-session${s.id === selectedId || s.id === secondId ? " selected" : ""}`}
          aria-label={`${s.env_label}, ${s.name}, ${projectName(s)}, ${s.status === "live" ? stateLabel(s.state) : "Ended"}`}
          aria-current={s.id === selectedId ? "true" : undefined} onClick={() => onSelect(s.id)}>
          <span className={`state-dot ${s.status === "live" ? stateClass(s.state) : "exited"}`} />
          <span className="sidebar-session-copy"><strong>{s.name}</strong><span title={displayProjectPath(s)}>{projectName(s)}</span></span>
          {s.id === selectedId && secondId ? <span className="pane-tag">1</span> : s.id === secondId ? <span className="pane-tag">2</span> : null}
          {needsAttention(s) ? <span className="attention-count" aria-hidden="true">!</span> : null}
        </button>)}
      </section>;
    })}
  </nav>;
}

export function AgentDirectory({ envs, sessions, currentId, animate, onSelect, onClose, onNew }: {
  envs: EnvView[]; sessions: SessionView[]; currentId: string | null; animate: boolean;
  onSelect: (id: string) => void; onClose: () => void; onNew: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const close = useRef<HTMLButtonElement>(null);
  const [query, setQuery] = useState("");
  const [attention, setAttention] = useState(false);
  const availableRows = sessions.filter((s) => s.status === "live" || s.id === currentId);
  const requests = availableRows.filter(needsAttention).length;
  const normalize = (s: string) => s.toLocaleLowerCase().replace(/\s+/g, "");
  const term = normalize(query);
  const matches = availableRows.filter((s) => (!attention || needsAttention(s)) &&
    normalize(`${s.env} ${s.env_label} ${s.name} ${s.tmux_name} ${s.project} ${s.project_dir} ${kindLabel(s.kind)} ${cardTitle(s)}`).includes(term));

  useEffect(() => {
    const node = dialog.current;
    const opener = document.activeElement as HTMLElement | null;
    node?.showModal();
    // Do not summon the phone keyboard merely by opening the directory.
    close.current?.focus({ preventScroll: true });
    return () => {
      node?.close();
      if (opener?.isConnected) opener.focus({ preventScroll: true });
    };
  }, []);

  return <dialog className={`agent-directory${animate ? " pointer-open" : ""}`} ref={dialog}
    aria-labelledby="agent-directory-title" aria-describedby="agent-directory-summary"
    onCancel={(e) => { e.preventDefault(); onClose(); }}
    onKeyDown={(e) => { if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); onClose(); } }}
    onClick={(e) => {
      if (e.target !== e.currentTarget) return;
      const rect = e.currentTarget.getBoundingClientRect();
      if (e.clientX < rect.left || e.clientX > rect.right || e.clientY < rect.top || e.clientY > rect.bottom) onClose();
    }}>
    <span className="directory-handle" aria-hidden="true" />
    <header className="directory-head"><h2 id="agent-directory-title">All agents</h2>
      <button className="icon-button" ref={close} aria-label="Close agent directory" onClick={onClose}><CloseIcon /></button></header>
    <p id="agent-directory-summary">{availableRows.length} sessions across {workspaceGroups(envs, availableRows).length} workspaces</p>
    <div className="directory-tools">
      <input type="search" value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Find an agent or project…"
        aria-label="Search agents, projects or workspaces" autoCapitalize="none" autoComplete="off" spellCheck={false} />
      <div className="directory-filters" role="group" aria-label="Directory filter">
        <button aria-pressed={!attention} onClick={() => setAttention(false)}>All agents <span className="workspace-count">{availableRows.length}</span></button>
        <button aria-pressed={attention} onClick={() => setAttention(true)}>Needs you <span className="attention-count">{requests}</span></button>
      </div>
    </div>
    <div className="directory-list">
      {workspaceGroups(envs, availableRows).map((env) => {
        const group = matches.filter((s) => s.env === env.id).sort(byAgentName);
        if (!group.length && (term || attention)) return null;
        const unavailable = envs.find((e) => e.id === env.id)?.available === false;
        return <section className="directory-group" key={env.id}>
          <h3 className="workspace-group-heading"><span className={`workspace-dot ${env.id}`} />{env.label}<span>{group.length} sessions</span></h3>
          {unavailable ? <p className="sidebar-empty">Workspace unavailable</p> : null}
          {!group.length && !unavailable ? <p className="sidebar-empty">No live sessions</p> : null}
          {group.map((s) => <button className={`directory-session${s.id === currentId ? " selected" : ""}`} key={s.id}
            aria-label={`Open ${s.env_label}, ${s.name}, ${projectName(s)}, ${s.status === "live" ? stateLabel(s.state) : "Ended"}`}
            onClick={() => onSelect(s.id)}>
            <span className={`agent-glyph ${s.kind}`}><KindGlyph kind={s.kind} /></span>
            <span className="directory-session-copy"><strong>{s.name}</strong><span className="directory-project" title={displayProjectPath(s)}>{kindLabel(s.kind)} · {projectName(s)}</span>
              <span className="directory-task">{cardTitle(s)}</span></span>
            {s.id === currentId ? <CheckIcon className="directory-selected" /> : <span className={`state ${s.status === "live" ? stateClass(s.state) : "exited"}`}><span className="dot" />{s.status === "live" ? stateLabel(s.state) : "Ended"}</span>}
          </button>)}
        </section>;
      })}
      {!matches.length && (term || attention) ? <p className="directory-empty" role="status">{attention && !term ? "No agents need your input." : "No agents match your search."}</p> : null}
    </div>
    <footer className="directory-footer"><button className="button" onClick={onNew}><PlusIcon />New session</button></footer>
  </dialog>;
}
