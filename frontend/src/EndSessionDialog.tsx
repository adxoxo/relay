// End session dialog. Warns when the tmux session has more than one pane.
// Handles 409 scope_changed by refreshing state and explaining.

import { useState } from "react";
import { ApiError, endSession } from "./api";
import { Modal } from "./Modal";
import type { SessionView } from "./types";

interface EndSessionDialogProps {
  open: boolean;
  session: SessionView | null;
  onClose: () => void;
  onEnded: () => void;
  onScopeChanged: () => void;
}

export function EndSessionDialog({
  open,
  session,
  onClose,
  onEnded,
  onScopeChanged,
}: EndSessionDialogProps) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!session) return null;
  const s = session;

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      await endSession(s.id, {
        tmux_session_id: s.tmux.session_id,
        tmux_created: s.tmux.created ?? 0,
        pane_count: s.tmux.pane_count,
      });
      onEnded();
    } catch (err) {
      if (err instanceof ApiError && err.status === 409 && err.code === "scope_changed") {
        setError(
          "This session changed since it loaded. Relay refreshed it. Review it and try again.",
        );
        onScopeChanged();
      } else if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError("Cannot reach relay.");
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal open={open} onClose={onClose} titleId="end-session-title" dismissLabel="Dismiss end session">
      <div className="modal-body">
        <div className="modal-head">
          <div>
            <h2 id="end-session-title">End {s.name}?</h2>
            <p>
              This stops the agent and closes its terminal in {s.env_label}. Project files
              stay unchanged.
            </p>
          </div>
        </div>
        <div className="close-summary">
          <strong>
            {s.env_label} / {s.name}
          </strong>
          <span>~/projects/{s.project}</span>
        </div>
        {s.tmux.pane_count > 1 ? (
          <div className="modal-warning" role="status">
            This tmux session has {s.tmux.pane_count} panes. Ending it closes all of them.
          </div>
        ) : null}
        {error ? (
          <div className="field-error" role="alert">
            {error}
          </div>
        ) : null}
        <div className="modal-actions">
          <button type="button" className="button" onClick={onClose}>
            Keep session
          </button>
          <button
            type="button"
            className="button destructive"
            onClick={() => void submit()}
            disabled={busy}
          >
            {busy ? "Ending…" : "End session"}
          </button>
        </div>
      </div>
    </Modal>
  );
}
