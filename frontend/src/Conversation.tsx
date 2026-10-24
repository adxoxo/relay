// Latest exchange, collapsed earlier turns, and existing permission controls.

import { useEffect, useRef, useState } from "react";
import { ApiError, getConversation, sendApproval } from "./api";
import { useToast } from "./toast";
import { CheckIcon, KindGlyph, ShieldIcon, TerminalIcon } from "./icons";
import { clockTime, firstLine, kindLabel, stateLabel } from "./util";
import type { ConvItem, ConversationView, SessionView } from "./types";

interface ConversationProps {
  session: SessionView;
  reloadKey: number;
  visible: boolean;
  onOpenTerminal: () => void;
}

export function Conversation({ session, reloadKey, visible, onOpenTerminal }: ConversationProps) {
  const s = session;
  const toast = useToast();
  const sourceKey = `${s.env}:${s.id}:${s.agent_session_id ?? ""}`;
  const [loaded, setLoaded] = useState<{ key: string; view: ConversationView } | null>(null);
  const conv = loaded?.key === sourceKey ? loaded.view : null;
  const [now, setNow] = useState(Date.now() / 1000);
  const [approvalBusy, setApprovalBusy] = useState(false);
  const feedRef = useRef<HTMLDivElement>(null);
  const followLatest = useRef(true);
  const active = s.status === "live" && (s.state === "working" || s.state === "needs_input");

  useEffect(() => {
    followLatest.current = true;
    if (feedRef.current) feedRef.current.scrollTop = 0;
  }, [sourceKey]);

  useEffect(() => {
    if (!s.capabilities.conversation) {
      setLoaded(null);
      return;
    }
    if (!visible) return;
    const ctrl = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let busy = false;
    const refresh = async () => {
      if (ctrl.signal.aborted || busy || document.hidden) return;
      busy = true;
      clearTimeout(timer);
      try {
        const view = await getConversation(s.id, 200, ctrl.signal);
        if (!ctrl.signal.aborted) {
          setLoaded((prev) => prev?.key === sourceKey && sameConversation(prev.view, view)
            ? prev : { key: sourceKey, view });
        }
      } catch (err) {
        if (!ctrl.signal.aborted) {
          setLoaded({ key: sourceKey, view: { supported: false,
            reason: err instanceof ApiError ? err.message : "Cannot reach Relay. Try again shortly.", items: [] } });
        }
      } finally {
        busy = false;
        if (!ctrl.signal.aborted && s.status === "live" && !document.hidden) {
          timer = setTimeout(() => void refresh(), active ? 2000 : 5000);
        }
      }
    };
    const visibility = () => {
      clearTimeout(timer);
      if (!document.hidden) void refresh();
    };
    document.addEventListener("visibilitychange", visibility);
    void refresh();
    return () => {
      ctrl.abort();
      clearTimeout(timer);
      document.removeEventListener("visibilitychange", visibility);
    };
  }, [s.id, sourceKey, s.capabilities.conversation, s.status, active, visible, reloadKey]);

  useEffect(() => {
    if (!active || !visible) return;
    const timer = setInterval(() => { if (!document.hidden) setNow(Date.now() / 1000); }, 1000);
    return () => clearInterval(timer);
  }, [active, visible]);

  const turns = conversationTurns(conv?.items ?? []);
  const candidate = turns.at(-1);
  const currentEvidence = conv?.state_at === s.state_at;
  const latest = !active || (currentEvidence && conv?.active_prompt_id === candidate?.prompt.id) ? candidate : undefined;
  const earlier = latest ? turns.slice(0, -1) : turns;
  const complete = !active;
  const finished = s.state === "idle" || s.state === "exited" || s.status !== "live";
  const tool = active ? s.pending?.summary ?? latest?.tool?.tool?.summary : null;
  const start = active ? currentEvidence ? conv?.active_started_at : null : latest?.prompt.ts;
  const elapsed = start && active ? duration(Math.max(0, now - start)) : null;

  useEffect(() => {
    if (feedRef.current && followLatest.current) feedRef.current.scrollTop = 0;
  }, [latest?.prompt.id]);

  const onFeedScroll = () => {
    const feed = feedRef.current;
    if (!feed) return;
    followLatest.current = feed.scrollTop < 80;
  };

  const decide = async (decision: "allow" | "deny") => {
    if (!s.pending) return;
    setApprovalBusy(true);
    try {
      await sendApproval(s.id, s.pending.id, decision);
      toast.show(decision === "allow" ? "Allowed once." : "Denied.");
    } catch (err) {
      if (err instanceof ApiError) toast.error(err.message);
      else toast.error("Cannot reach relay.");
    } finally {
      setApprovalBusy(false);
    }
  };

  const copyAnswer = async (text: string) => {
    try {
      if (!navigator.clipboard) throw new Error("Clipboard unavailable");
      await navigator.clipboard.writeText(text);
      toast.show("Answer copied.");
    } catch {
      toast.error("Cannot copy the answer. Select its text instead.");
    }
  };

  const notSupported = s.capabilities.conversation && conv && !conv.supported;

  return (
    <div className="feed" id="feed" aria-live="polite" ref={feedRef} onScroll={onFeedScroll}>
      {!s.capabilities.conversation ? (
        <UnsupportedBlock
          reason={s.kind === "codex" ? "Relay needs a verified link to this Codex thread. Open Terminal to continue."
            : s.kind === "shell" ? "Shell sessions have no conversation."
            : "Relay has no saved conversation for this session yet."}
          onOpenTerminal={onOpenTerminal}
        />
      ) : notSupported ? (
        <UnsupportedBlock
          reason={conv?.reason ?? "Conversation is not available."}
          onOpenTerminal={onOpenTerminal}
        />
      ) : (
        <>
          <div className="turn-status" role="status">
            <span>{finished && latest?.reply ? "Done" : stateLabel(s.state)}{elapsed ? ` · ${elapsed}` : ""}</span>
            {tool ? <code title={`Latest tool: ${tool}`}>{tool}</code> : null}
          </div>
          {latest ? (
            <>
              {latest.promptMissing ? <p className="turn-placeholder">The prompt is outside the recent transcript window. Use Terminal for older output.</p>
                : <TurnMessage item={latest.prompt} session={s} />}
              {complete && latest.reply ? (
                <>
                  {!finished ? <p className="turn-history-note">Last saved answer. Open Terminal to check the current state.</p> : null}
                  <TurnMessage item={latest.reply} session={s} />
                  <div className="turn-actions">
                    <button className="button" onClick={() => void copyAnswer(latest.reply!.text)}>Copy answer</button>
                    <button className="button" onClick={onOpenTerminal}><TerminalIcon />Open terminal</button>
                  </div>
                </>
              ) : (
                <p className="turn-placeholder">{active ? "The answer appears when the turn ends."
                  : complete ? "This turn has no saved answer yet." : "Open Terminal to check the current turn."}</p>
              )}
            </>
          ) : (
            <div className="empty-state">
              {!conv ? "Load the latest turn…" : active ? "The agent has not saved the current prompt yet."
                : "No saved prompt in the recent transcript. Open Terminal for older output."}
            </div>
          )}
          {earlier.length ? (
            <details className="earlier-turns" key={sourceKey}>
              <summary>Earlier turns ({earlier.length})</summary>
              {[...earlier].reverse().map((turn) => (
                <details className="earlier-turn" key={turn.prompt.id}>
                  <summary><time>{clockTime(turn.prompt.ts ?? turn.reply?.ts ?? null)}</time><span>{firstLine(turn.prompt.text) || "Earlier saved answer"}</span></summary>
                  {turn.promptMissing ? <p className="turn-placeholder">The prompt is outside the recent transcript window.</p>
                    : <TurnMessage item={turn.prompt} session={s} />}
                  {turn.reply ? <TurnMessage item={turn.reply} session={s} /> : <p className="turn-placeholder">No saved answer.</p>}
                </details>
              ))}
              <p className="turn-history-note">Recent saved turns only. Use Terminal for older output.</p>
            </details>
          ) : null}
        </>
      )}

      {s.pending ? (
        <div className="approval">
          <div className="approval-heading">
            <ShieldIcon />
            Permission requested
          </div>
          {s.pending.detail ? <p>{s.pending.detail}</p> : null}
          <code>{s.pending.summary}</code>
          {s.pending.answerable ? (
            <>
              <div className="approval-buttons">
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
              <div className="approval-note">
                This permission applies to this command only.
              </div>
            </>
          ) : (
            <>
              <p>Answer this request in the terminal.</p>
              <div className="approval-buttons">
                <button className="button" onClick={onOpenTerminal}>
                  <TerminalIcon />
                  Open terminal
                </button>
              </div>
            </>
          )}
        </div>
      ) : null}
    </div>
  );
}

interface Turn {
  prompt: ConvItem;
  reply: ConvItem | null;
  tool: ConvItem | null;
  promptMissing?: boolean;
}

function conversationTurns(items: ConvItem[]): Turn[] {
  const turns: Turn[] = [];
  for (const item of items) {
    if (item.role === "user") {
      const current = turns.at(-1);
      if (current && sameMessage(current.prompt, item)) {
        current.prompt = { ...current.prompt, text: `${current.prompt.text}\n\n${item.text}` };
      } else turns.push({ prompt: item, reply: null, tool: null });
    }
    else {
      let current = turns.at(-1);
      if (!current && item.role === "assistant" && item.phase !== "commentary") {
        // A long turn can put its prompt outside the bounded read window.
        current = { prompt: { id: `missing-${item.id}`, role: "user", text: "", ts: null, tool: null },
          reply: null, tool: null, promptMissing: true };
        turns.push(current);
      }
      if (!current) continue;
      if (item.role === "tool") current.tool = item;
      else if (item.phase !== "commentary") {
        current.reply = current.reply && sameMessage(current.reply, item)
          ? { ...current.reply, text: `${current.reply.text}\n\n${item.text}` } : item;
      }
    }
  }
  return turns;
}

function sameMessage(a: ConvItem, b: ConvItem): boolean {
  return a.ts === b.ts && a.id.replace(/-\d+$/, "") === b.id.replace(/-\d+$/, "");
}

function duration(seconds: number): string {
  const whole = Math.floor(seconds);
  return whole < 60 ? `${whole} s` : `${Math.floor(whole / 60)} min ${whole % 60} s`;
}

function TurnMessage({ item, session }: { item: ConvItem; session: SessionView }) {
  return <div className={`message${item.role === "user" ? " user" : ""}`}>
    <div className="message-avatar">{item.role === "user" ? "AY" : <KindGlyph kind={session.kind} />}</div>
    <div className="message-body">
      <div className="message-meta">{item.role === "user" ? "You" : kindLabel(session.kind)}
        {item.ts ? <time>{clockTime(item.ts)}</time> : null}</div>
      <p>{item.text}</p>
    </div>
  </div>;
}

// Same items (by id and text) means no re-render and no scroll change.
function sameConversation(a: ConversationView | null, b: ConversationView): boolean {
  if (!a || a.supported !== b.supported || a.reason !== b.reason || a.items.length !== b.items.length
    || a.active_started_at !== b.active_started_at || a.active_prompt_id !== b.active_prompt_id || a.state_at !== b.state_at) return false;
  for (let i = 0; i < a.items.length; i += 1) {
    const x = a.items[i];
    const y = b.items[i];
    if (x.id !== y.id || x.role !== y.role || x.text !== y.text || x.ts !== y.ts || x.phase !== y.phase
      || x.tool?.name !== y.tool?.name || x.tool?.summary !== y.tool?.summary) return false;
  }
  return true;
}

function UnsupportedBlock({
  reason,
  onOpenTerminal,
}: {
  reason: string;
  onOpenTerminal: () => void;
}) {
  return (
    <div className="empty-state">
      {reason}
      <button className="button" onClick={onOpenTerminal}>
        <TerminalIcon />
        Open terminal
      </button>
    </div>
  );
}
