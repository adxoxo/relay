// The per-workspace session list: heading, New session button, status filter,
// live cards, and a collapsible Recently ended group.

import { useEffect, useMemo, useState } from "react";
import type { EnvView, SessionView } from "./types";
import {
  cardTitle,
  displayProjectPath,
  kindLabel,
  relativeTime,
  stateClass,
  stateDescription,
  stateLabel,
} from "./util";
import { byAgentName, workspaceGroups } from "./sessionNavigation";
import { ArrowIcon, ChevronRightIcon, KindGlyph, LinkIcon, PlusIcon } from "./icons";

type StatusFilter = "all" | "waiting" | "working" | "idle" | "terminal" | "ended";

interface SessionListProps {
  workspace: "all" | string;
  envs: EnvView[];
  sessions: SessionView[];
  selectedId: string | null;
  // The session in the second terminal pane, when two panes show.
  secondId?: string | null;
  onSelect: (id: string) => void;
  onNew: () => void;
  onResume: (s: SessionView) => void;
  nowSec: number;
}

const FILTER_LABELS: Record<StatusFilter, string> = {
  all: "All statuses",
  waiting: "Needs input",
  working: "Working",
  idle: "Idle",
  terminal: "Terminal only",
  ended: "Ended",
};

function matchesFilter(s: SessionView, filter: StatusFilter): boolean {
  switch (filter) {
    case "all":
      return true;
    case "waiting":
      return s.status === "live" && s.state === "needs_input";
    case "working":
      return s.status === "live" && s.state === "working";
    case "idle":
      return s.status === "live" && s.state === "idle";
    case "terminal":
      return s.status === "live" && s.state_source === "terminal";
    case "ended":
      return s.status !== "live";
    default:
      return true;
  }
}

function envLabelFor(workspace: string, envs: EnvView[]): string {
  if (workspace === "all") return "All sessions";
  const env = envs.find((e) => e.id === workspace);
  return env ? `${env.label} sessions` : "Sessions";
}

export function SessionList({
  workspace,
  envs,
  sessions,
  selectedId,
  secondId = null,
  onSelect,
  onNew,
  onResume,
  nowSec,
}: SessionListProps) {
  const paneOf = (id: string): 1 | 2 | null =>
    secondId == null ? null : id === selectedId ? 1 : id === secondId ? 2 : null;
  const [filter, setFilter] = useState<StatusFilter>("all");
  const [endedOpen, setEndedOpen] = useState(false);

  // Reset the filter when the workspace changes.
  useEffect(() => {
    setFilter("all");
  }, [workspace]);

  const inScope = useMemo(
    () => sessions.filter((s) => workspace === "all" || s.env === workspace),
    [sessions, workspace],
  );

  const currentEnv = workspace === "all" ? null : envs.find((e) => e.id === workspace) ?? null;
  const envUnavailable = currentEnv && !currentEnv.available;

  const live = inScope.filter((s) => s.status === "live");
  const ended = inScope.filter((s) => s.status !== "live");

  const visibleLive = live.filter((s) => matchesFilter(s, filter));
  const showEndedGroup =
    ended.length > 0 && (filter === "all" || filter === "ended");

  return (
    <section className="agent-list" aria-label="Agent sessions">
      <div className="list-head">
        <h2>{envLabelFor(workspace, envs)}</h2>
        <button className="button new-session" onClick={onNew}>
          <PlusIcon />
          New session
        </button>
      </div>
      <div className="list-filter">
        <label htmlFor="status-filter">Session status</label>
        <select
          id="status-filter"
          value={filter}
          onChange={(e) => setFilter(e.target.value as StatusFilter)}
          aria-label="Filter sessions"
        >
          {(Object.keys(FILTER_LABELS) as StatusFilter[]).map((key) => (
            <option key={key} value={key}>
              {FILTER_LABELS[key]}
            </option>
          ))}
        </select>
      </div>

      {envUnavailable ? (
        <div className="env-unavailable" role="status">
          {currentEnv?.label} is unavailable:{" "}
          {currentEnv?.error ?? unavailableReason(currentEnv)}
        </div>
      ) : null}

      {!envUnavailable && visibleLive.length === 0 && !showEndedGroup ? (
        <EmptyState hasAny={inScope.length > 0} onNew={onNew} />
      ) : null}

      <div className="workspace-session-groups">
        {workspaceGroups(envs, inScope).filter((env) => workspace === "all" || env.id === workspace).map((env) => {
          const rows = visibleLive.filter((s) => s.env === env.id).sort(byAgentName);
          if (!rows.length) return null;
          return <section className="workspace-session-group" key={env.id} aria-label={`${env.label} agents`}>
            <h3 className="workspace-group-heading"><span className={`workspace-dot ${env.id}`} />{env.label}<span>{rows.length} sessions</span></h3>
            <div className="workspace-session-cards">{rows.map((s) => <SessionCard key={s.id} session={s} workspace={workspace}
              selected={s.id === selectedId || s.id === secondId} pane={paneOf(s.id)} onSelect={onSelect} nowSec={nowSec} />)}</div>
          </section>;
        })}
      </div>

      {showEndedGroup ? (
        <div className="ended-group">
          <button
            className={`ended-toggle${endedOpen ? " open" : ""}`}
            onClick={() => setEndedOpen((v) => !v)}
            aria-expanded={endedOpen}
          >
            <ChevronRightIcon />
            Recently ended ({ended.length})
          </button>
          {endedOpen
            ? ended.map((s) => (
                <SessionCard
                  key={s.id}
                  session={s}
                  workspace={workspace}
                  selected={s.id === selectedId || s.id === secondId}
                  pane={paneOf(s.id)}
                  onSelect={onSelect}
                  onResume={onResume}
                  nowSec={nowSec}
                />
              ))
            : null}
        </div>
      ) : null}

      <div className="tip">
        <LinkIcon />
        <span>
          The same sessions as your laptop.
          <br />
          Close this tab. Your agents keep working.
        </span>
      </div>
    </section>
  );
}

function unavailableReason(env: EnvView | null): string {
  if (!env) return "unavailable.";
  switch (env.status) {
    case "stopped":
      return "container stopped.";
    case "missing":
      return "container missing.";
    default:
      return "container error.";
  }
}

interface CardProps {
  session: SessionView;
  workspace: "all" | string;
  selected: boolean;
  pane?: 1 | 2 | null;
  onSelect: (id: string) => void;
  onResume?: (s: SessionView) => void;
  nowSec: number;
}

function SessionCard({ session, workspace, selected, pane, onSelect, onResume, nowSec }: CardProps) {
  const s = session;
  const kindText = kindLabel(s.kind) + (workspace === "all" ? ` · ${s.env_label}` : "");
  const badgeClass = s.status === "live" ? stateClass(s.state) : "exited";
  const badgeText = s.status === "live" ? stateLabel(s.state) : "Ended";
  return (
    <button
      className={`agent-card${selected ? " selected" : ""}`}
      onClick={() => onSelect(s.id)}
      aria-pressed={selected}
      aria-label={`${s.env_label}, ${s.name}, ${badgeText}`}
    >
      <div className="agent-top">
        <div className="agent-identity">
          <div className={`agent-glyph ${s.kind}`}>
            <KindGlyph kind={s.kind} />
          </div>
          <div>
            <div className="agent-name">{s.name}</div>
            <div className="agent-kind">{kindText}</div>
          </div>
        </div>
        <span className="agent-badges">
          {pane ? <span className="pane-tag">Pane {pane}</span> : null}
          <span className={`state ${badgeClass}`}>
            <span className="dot" />
            {badgeText}
          </span>
        </span>
      </div>
      {s.pending ? <p className="agent-pending">Permission requested: {s.pending.summary}</p> : null}
      <h3>{cardTitle(s)}</h3>
      <p className="agent-summary">{stateDescription(s)}</p>
      <div className="agent-foot">
        <span className="path">{displayProjectPath(s)}</span>
        {s.status === "live" ? (
          <span className="time">
            {relativeTime(s.activity_at ?? s.created_at, nowSec)} <ArrowIcon />
          </span>
        ) : onResume && s.capabilities.resume ? (
          <span
            className="resume-button"
            role="button"
            tabIndex={0}
            onClick={(e) => {
              e.stopPropagation();
              onResume(s);
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                e.stopPropagation();
                onResume(s);
              }
            }}
          >
            Resume
          </span>
        ) : (
          <span className="time">{relativeTime(s.ended_at, nowSec)}</span>
        )}
      </div>
    </button>
  );
}

function EmptyState({ hasAny, onNew }: { hasAny: boolean; onNew: () => void }) {
  if (hasAny) {
    return <div className="empty-state">No sessions with this status.</div>;
  }
  return (
    <div className="empty-state">
      No sessions here yet.
      <br />
      Start a new session to get going.
      <button className="button primary" onClick={onNew}>
        <PlusIcon />
        New session
      </button>
    </div>
  );
}
