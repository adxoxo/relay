// Small pure helpers shared across components.

import type { SessionKind, SessionState, SessionView } from "./types";

export function kindLabel(kind: SessionKind): string {
  switch (kind) {
    case "claude":
      return "Claude";
    case "codex":
      return "Codex";
    case "shell":
      return "Shell";
    default:
      return "Session";
  }
}

// The status badge class and label derive from the session state.
const STATE_LABELS: Record<string, string> = {
  working: "Working",
  needs_input: "Needs input",
  idle: "Idle",
  exited: "Ended",
  unknown: "Unknown",
};

export function stateLabel(state: SessionState): string {
  return STATE_LABELS[state] ?? "Unknown";
}

// Map a state to a CSS badge class from styles.css.
export function stateClass(state: SessionState): string {
  if (state === "working") return "working";
  if (state === "needs_input") return "waiting";
  if (state === "idle") return "idle";
  if (state === "exited") return "exited";
  return "unknown";
}

// One-line description of the current session state.
export function stateDescription(s: SessionView): string {
  if (s.status !== "live") {
    return endReasonText(s.end_reason);
  }
  switch (s.state) {
    case "working":
      return "Working on your task now.";
    case "needs_input":
      return "Waiting for your input.";
    case "idle":
      return s.kind === "shell"
        ? "An open terminal. Start an agent when you are ready."
        : "Idle. Send a prompt to continue.";
    case "exited":
      return "The agent has exited.";
    default:
      return s.origin === "discovered"
        ? "Status is not reported for sessions started outside relay. Open the terminal to check."
        : "Status appears after the agent's first event.";
  }
}

export function endReasonText(reason: string | null): string {
  switch (reason) {
    case "ended_by_user":
      return "You ended this session.";
    case "closed":
      return "The tmux session closed.";
    case "container_stopped":
      return "The container stopped.";
    case "container_restarted":
      return "The container restarted.";
    case "server_restarted":
      return "The tmux server restarted.";
    default:
      return "This session has ended.";
  }
}

// Title line for a card: the first line of the last agent message, else a
// title that only claims what relay actually knows about the session.
export function cardTitle(s: SessionView): string {
  const line = firstLine(s.last_message);
  if (line) return line;
  if (s.status !== "live") return "Session ended";
  switch (s.state) {
    case "working":
      return "Working on a task";
    case "needs_input":
      return "Waiting for your answer";
    case "idle":
      return "Ready for your next task";
    case "exited":
      return "The agent has exited";
    default:
      if (s.kind === "shell") return "Open terminal";
      return s.origin === "discovered" ? "Started on the laptop" : "Starting";
  }
}

export function firstLine(text: string | null | undefined): string {
  if (!text) return "";
  const line = text.split("\n").find((l) => l.trim().length > 0);
  return line ? line.trim() : "";
}

// Show a project path as ~/projects/... regardless of the container home.
export function displayProjectPath(s: SessionView): string {
  const project = s.project?.trim();
  if (project) return `~/projects/${project}`;
  const dir = s.project_dir ?? "";
  const idx = dir.indexOf("/projects");
  if (idx >= 0) return `~${dir.slice(idx)}`;
  return dir || "~/projects";
}

// Relative time that a caller refreshes on a timer.
export function relativeTime(epoch: number | null, now: number = Date.now() / 1000): string {
  if (!epoch) return "";
  const diff = Math.max(0, now - epoch);
  if (diff < 45) return "Just now";
  if (diff < 90) return "1 min ago";
  const mins = Math.round(diff / 60);
  if (mins < 60) return `${mins} min ago`;
  const hours = Math.round(diff / 3600);
  if (hours < 24) return `${hours} h ago`;
  const days = Math.round(diff / 86400);
  return `${days} d ago`;
}

// Clock time for a conversation message.
export function clockTime(epoch: number | null): string {
  if (!epoch) return "";
  const d = new Date(epoch * 1000);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

// A phone, from the physical screen: a touch screen whose short side is at most
// 560 CSS px. The screen size does not change when the on-screen keyboard
// opens, so a tablet with its keyboard up never reads as a phone in landscape.
export function isPhoneScreen(): boolean {
  const touch = matchMedia("(pointer: coarse)").matches || "ontouchstart" in window;
  return touch && Math.min(screen.width, screen.height) <= 560;
}

// The name pattern from ARCHITECTURE.md section 7.
export const NAME_PATTERN = /^[A-Za-z0-9][A-Za-z0-9 _-]{0,39}$/;

export function isValidName(name: string): boolean {
  return NAME_PATTERN.test(name);
}
