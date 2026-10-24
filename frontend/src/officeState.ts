import type { EnvView, SessionView } from "./types";
import { byAgentName, needsAttention } from "./sessionNavigation";

export const DESKS_PER_PAGE = 6;
export const OFFICE_STALE_SECONDS = 40;
export type OfficeState = "working" | "needs_input" | "idle" | "unknown" | "stale" | "offline";
export interface OfficeMemory {
  slots: Record<string, (string | null)[]>;
  pages: Record<string, number>;
  workspace: string | null;
  paused: boolean;
  focusId: string | null;
  lastKnown: Record<string, SessionView[]>;
}

export function officeSessions(sessions: SessionView[], envs: EnvView[], lastKnown: OfficeMemory["lastKnown"]): SessionView[] {
  const result: SessionView[] = [];
  for (const env of new Set([...envs.map((e) => e.id), ...sessions.map((s) => s.env)])) {
    const rows = sessions.filter((s) => s.env === env);
    const live = rows.filter((s) => s.status === "live");
    if (envs.some((e) => e.id === env && e.available) || live.length) { result.push(...live); continue; }
    if (lastKnown[env]?.length) {
      result.push(...lastKnown[env].map((s) => rows.find((row) => row.id === s.id) ?? s).filter((s) => s.end_reason !== "ended_by_user"));
      continue;
    }
    // Reload while stopped: only the most recent container-stop cohort, never all ended agents.
    const stopped = rows.filter((s) => s.status === "interrupted" && s.end_reason === "container_stopped" && s.ended_at != null);
    const latest = Math.max(0, ...stopped.map((s) => s.ended_at!));
    result.push(...stopped.filter((s) => latest - s.ended_at! < 5));
  }
  return result;
}

// Names and projects can change or repeat; a desk belongs to one exact Relay row.
export function reconcileDesks(previous: OfficeMemory["slots"], sessions: SessionView[]): OfficeMemory["slots"] {
  const live = sessions;
  const result: OfficeMemory["slots"] = {};
  for (const env of new Set(live.map((s) => s.env))) {
    const rows = live.filter((s) => s.env === env).sort(byAgentName);
    const ids = new Set(rows.map((s) => s.id));
    const assigned = new Set<string>();
    const slots = (previous[env] ?? []).map((id) => {
      if (!id || !ids.has(id) || assigned.has(id)) return null;
      assigned.add(id);
      return id;
    });
    for (const row of rows) {
      if (assigned.has(row.id)) continue;
      const vacancy = slots.indexOf(null);
      if (vacancy < 0) slots.push(row.id);
      else slots[vacancy] = row.id;
      assigned.add(row.id);
    }
    while (slots.length && slots.at(-1) == null) slots.pop();
    result[env] = slots;
  }
  return result;
}

export function officeState(session: SessionView, available: boolean, fresh: boolean): OfficeState {
  if (!available) return "offline";
  if (!fresh) return "stale";
  if (needsAttention(session)) return "needs_input";
  return session.state === "working" || session.state === "idle" ? session.state : "unknown";
}

export const OFFICE_STATE_LABELS: Record<OfficeState, string> = {
  working: "Working", needs_input: "Needs input", idle: "Idle", unknown: "Unknown",
  stale: "Updates paused", offline: "Unavailable",
};
