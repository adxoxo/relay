// Session panel: one terminal pane of the Agents workspace. Header (back,
// icon, name / env, kind · project, status, pane controls, an actions menu
// with Interrupt and End), the pending permission request, a compact session
// switch strip (phone and phone landscape, where the list is hidden), and the
// live terminal. An ended session shows its saved transcript, read only.

import { useEffect, useRef, useState } from "react";
import { ApiError, getConversation, interruptSession, sendApproval } from "./api";
import { useToast } from "./toast";
import { TerminalPane } from "./TerminalPane";
import { BackIcon, CheckIcon, CloseIcon, KindGlyph, MoreIcon, PlusIcon, ShieldIcon, SplitIcon, StopIcon } from "./icons";
import { kindLabel, stateClass, stateLabel } from "./util";
import type { ConvItem, EnvView, SessionView } from "./types";
import { DirectoryButton, WorkspacePicker } from "./AgentNavigation";
import { byAgentName, projectName, rememberedSessions } from "./sessionNavigation";

interface SessionPanelProps {
  session: SessionView;
  reloadKey: number;
  visible?: boolean;
  onBack: () => void;
  // "Back to Activity" when the phone Activity page opened this session.
  backLabel?: string;
  onEnd: (s: SessionView) => void;
  onRefresh: () => void;
  onResume?: (s: SessionView) => void;
  // The switch strip: the sessions to offer and the switch itself.
  switchSessions?: SessionView[];
  envs?: EnvView[];
  onDirectory?: (animate: boolean) => void;
  onEmptyWorkspace?: (id: string) => void;
  onSwitch?: (id: string) => void;
  onNew?: () => void;
  // Two-pane workspace: open a second pane, close this pane, the pane a list pick fills.
  onSplit?: () => void;
  onClosePane?: () => void;
  active?: boolean;
  onActivate?: () => void;
}

export function SessionPanel({
  session,
  reloadKey,
  visible = true,
  onBack,
  backLabel = "Back to agents",
  onEnd,
  onRefresh,
  onResume,
  switchSessions,
  envs = [],
  onDirectory,
  onEmptyWorkspace,
  onSwitch,
  onNew,
  onSplit,
  onClosePane,
  active = false,
  onActivate,
}: SessionPanelProps) {
  const s = session;
  const toast = useToast();
  const [interrupting, setInterrupting] = useState(false);
  const [approvalBusy, setApprovalBusy] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);

  const badgeClass = s.status === "live" ? stateClass(s.state) : "exited";
  const badgeText = s.status === "live" ? stateLabel(s.state) : "Ended";

  // The actions menu closes on an outside tap, on Escape, and on a session change.
  useEffect(() => {
    if (!menuOpen) return;
    const onDown = (e: PointerEvent) => {
      if (!menuRef.current?.contains(e.target as Node)) setMenuOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setMenuOpen(false);
    };
    document.addEventListener("pointerdown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [menuOpen]);
  useEffect(() => setMenuOpen(false), [s.id]);

  const doInterrupt = async () => {
    setMenuOpen(false);
    setInterrupting(true);
    try {
      await interruptSession(s.id);
      toast.show(`Interrupted ${s.name}.`);
      onRefresh();
    } catch (err) {
      if (err instanceof ApiError) toast.error(err.message);
      else toast.error("Cannot reach relay.");
    } finally {
      setInterrupting(false);
    }
  };

  const decide = async (decision: "allow" | "deny") => {
    if (!s.pending) return;
    setApprovalBusy(true);
    try {
      await sendApproval(s.id, s.pending.id, decision);
      toast.show(decision === "allow" ? "Allowed once." : "Denied.");
      onRefresh();
    } catch (err) {
      if (err instanceof ApiError) toast.error(err.message);
      else toast.error("Cannot reach relay.");
    } finally {
      setApprovalBusy(false);
    }
  };

  return (
    <section
      className={`conversation session-pane${active ? " active-pane" : ""}`}
      aria-label={`Session ${s.name}`}
      onPointerDownCapture={onActivate}
      onFocusCapture={onActivate}
    >
      {switchSessions && onSwitch ? (
        <SessionSwitcher sessions={switchSessions} envs={envs} currentId={s.id} onSwitch={onSwitch} onNew={onNew}
          onDirectory={onDirectory} onEmptyWorkspace={onEmptyWorkspace} />
      ) : null}
      <header className="chat-head">
        <div className="chat-name">
          <button className={`mobile-back${backLabel === "Back to Office" ? " office-back" : ""}`} aria-label={backLabel} onClick={onBack}>
            <BackIcon />
            {backLabel === "Back to Office" ? <span className="office-back-label">Office</span> : null}
          </button>
          <div className={`agent-glyph ${s.kind}`}>
            <KindGlyph kind={s.kind} />
          </div>
          <div className="chat-title">
            <h2>
              {s.name} <span className="env-label">/ {s.env_label}</span>
            </h2>
            <p title={s.project_dir}>
              {kindLabel(s.kind)} · {projectName(s)}
            </p>
          </div>
        </div>
        <div className="pane-tools">
          <span className={`state chat-state ${badgeClass}`}>
            <span className="dot" />
            {badgeText}
          </span>
          {onSplit ? (
            <button className="icon-button pane-split" aria-label="Open a second terminal pane" title="Second pane" onClick={onSplit}>
              <SplitIcon />
            </button>
          ) : null}
          <div className="pane-menu-wrap" ref={menuRef}>
            <button
              className="icon-button pane-menu-button"
              aria-label="Session actions"
              aria-haspopup="menu"
              aria-expanded={menuOpen}
              onClick={() => setMenuOpen((v) => !v)}
            >
              <MoreIcon />
            </button>
            {menuOpen ? (
              <div className="pane-menu" role="menu" aria-label={`Actions for ${s.name}`}>
                <button
                  role="menuitem"
                  onClick={() => void doInterrupt()}
                  disabled={!s.capabilities.interrupt || interrupting}
                  title="Stop the current turn"
                >
                  <StopIcon />
                  Interrupt
                </button>
                <button
                  role="menuitem"
                  className="danger-button"
                  onClick={() => {
                    setMenuOpen(false);
                    onEnd(s);
                  }}
                  disabled={!s.capabilities.end}
                >
                  <CloseIcon />
                  End session…
                </button>
              </div>
            ) : null}
          </div>
          {onClosePane ? (
            <button className="icon-button pane-close" aria-label="Close this pane" title="Close pane" onClick={onClosePane}>
              <CloseIcon />
            </button>
          ) : null}
        </div>
      </header>


      {s.pending ? (
        <div className="pending-bar" role="status">
          <ShieldIcon />
          <div className="pending-text">
            <b>Permission requested</b>
            <code title={s.pending.detail ?? undefined}>{s.pending.summary}</code>
          </div>
          {s.pending.answerable ? (
            <div className="pending-buttons">
              <button
                className="button primary"
                onClick={() => void decide("allow")}
                disabled={approvalBusy || !s.capabilities.approve}
              >
                <CheckIcon />
                Allow once
              </button>
              <button
                className="button"
                onClick={() => void decide("deny")}
                disabled={approvalBusy || !s.capabilities.approve}
              >
                Deny
              </button>
            </div>
          ) : (
            <span className="pending-hint">Answer it in the terminal.</span>
          )}
        </div>
      ) : null}

      {s.capabilities.terminal ? (
        visible ? <TerminalPane key={s.id} session={s} /> : <div className="terminal-wrap" />
      ) : (
        <SavedTranscript session={s} reloadKey={reloadKey} visible={visible} onResume={onResume} />
      )}
    </section>
  );
}

// Live sessions in one compact row. Shown where the session list is hidden
// (phone, phone landscape), so a switch never needs the overview page.
function SessionSwitcher({ sessions, envs, currentId, onSwitch, onNew, onDirectory, onEmptyWorkspace }: {
  sessions: SessionView[]; envs: EnvView[]; currentId: string; onSwitch: (id: string) => void;
  onNew?: () => void; onDirectory?: (animate: boolean) => void; onEmptyWorkspace?: (id: string) => void;
}) {
  const rowRef = useRef<HTMLDivElement>(null);
  const current = sessions.find((s) => s.id === currentId);
  const scoped = sessions.filter((s) => s.env === current?.env).sort(byAgentName);
  useEffect(() => {
    const row = rowRef.current;
    const chip = row?.querySelector<HTMLElement>("[aria-current='true']");
    if (!row || !chip) return;
    if (chip.offsetLeft < row.scrollLeft || chip.offsetLeft + chip.offsetWidth > row.scrollLeft + row.clientWidth) {
      row.scrollLeft = Math.max(0, chip.offsetLeft - 8);
    }
  }, [currentId, scoped.length]);
  const switchWorkspace = (id: string) => {
    if (id === current?.env) return;
    const live = sessions.filter((s) => s.env === id && s.status === "live").sort(byAgentName);
    const previous = rememberedSessions()[id];
    const next = live.find((s) => s.id === previous) ?? live[0];
    if (next && envs.find((e) => e.id === id)?.available !== false) onSwitch(next.id);
    else onEmptyWorkspace?.(id);
  };
  return (
    <nav className="session-switch" aria-label="Switch session">
      <div className="session-workspaces">
        <WorkspacePicker envs={envs} sessions={sessions} value={current?.env ?? ""} onChange={switchWorkspace} />
        {onDirectory ? <DirectoryButton sessions={sessions} onClick={onDirectory} /> : null}
      </div>
      <div className="session-switch-row" ref={rowRef}>
        {scoped.map((x) => {
          const selected = x.id === currentId;
          const label = x.status === "live" ? stateLabel(x.state) : "Ended";
          return <button key={x.id} className={`switch-chip${selected ? " current" : ""}`}
            aria-current={selected ? "true" : undefined}
            aria-label={`${x.name}, ${x.env_label}, ${projectName(x)}, ${label}${x.pending ? ", permission requested" : ""}`}
            title={`${x.name} / ${x.env_label} · ${x.project || x.project_dir} · ${label}`}
            onClick={() => { if (!selected) onSwitch(x.id); }}>
            <span className="switch-identity"><span className={`state-dot ${x.status === "live" ? stateClass(x.state) : "exited"}`} />
              <span className="switch-name">{x.name}</span>
              {x.pending ? <span className="switch-flag" aria-hidden="true">!</span> : null}</span>
            <span className="switch-project">{projectName(x)}</span>
          </button>;
        })}
        {onNew ? <button className="switch-chip switch-new" aria-label="New session" title="New session" onClick={onNew}><PlusIcon /></button> : null}
      </div>
    </nav>
  );
}

// An ended session (or one without a terminal): its saved transcript, read
// only and labeled, through the existing conversation API.
function SavedTranscript({
  session,
  reloadKey,
  visible,
  onResume,
}: {
  session: SessionView;
  reloadKey: number;
  visible: boolean;
  onResume?: (s: SessionView) => void;
}) {
  const s = session;
  const [items, setItems] = useState<ConvItem[] | null>(null);
  const [reason, setReason] = useState<string | null>(null);
  const reasonText =
    s.status === "live"
      ? s.notes.find((n) => n) ?? "The terminal is not available for this session."
      : "This session has ended.";

  useEffect(() => {
    setItems(null);
    setReason(null);
    if (!visible || !s.capabilities.conversation) return;
    const ctrl = new AbortController();
    getConversation(s.id, 200, ctrl.signal)
      .then((view) => {
        if (ctrl.signal.aborted) return;
        if (view.supported) setItems(view.items);
        else setReason(view.reason ?? "No saved transcript.");
      })
      .catch((err) => {
        if (!ctrl.signal.aborted) setReason(err instanceof ApiError ? err.message : "Cannot reach Relay. Try again shortly.");
      });
    return () => ctrl.abort();
  }, [s.id, s.capabilities.conversation, visible, reloadKey]);

  return (
    <div className="saved-transcript" role="log" aria-label="Saved transcript">
      <div className="saved-transcript-head">
        <span>{reasonText}</span>
        {onResume && s.capabilities.resume ? (
          <button className="button" onClick={() => onResume(s)}>
            Resume
          </button>
        ) : null}
      </div>
      {s.capabilities.conversation ? (
        <>
          <p className="term-history-label">Saved transcript. This is not a live screen.</p>
          {reason ? <p className="term-history-note">{reason}</p> : null}
          {items == null && !reason ? <p className="term-history-note">Loading the saved transcript…</p> : null}
          {items?.length === 0 ? <p className="term-history-note">No saved messages.</p> : null}
          {items?.map((it) => (
            <div key={it.id} className={`term-history-item ${it.role === "user" ? "user" : it.role === "tool" ? "tool" : "assistant"}`}>
              <span className="term-history-role">
                {it.role === "user" ? "You" : it.role === "tool" ? "Tool" : kindLabel(s.kind)}
              </span>
              {it.role === "tool" ? it.tool?.summary ?? it.text : it.text}
            </div>
          ))}
        </>
      ) : null}
    </div>
  );
}
