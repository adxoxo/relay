import type { EnvView, SessionView } from "./types";

const MEMORY_KEY = "relay.lastSessionByWorkspace";

// Session IDs, rather than agent names: both workspaces can have an agent1.
export function rememberedSessions(): Record<string, string> {
  try {
    const saved: unknown = JSON.parse(localStorage.getItem(MEMORY_KEY) ?? "{}");
    if (!saved || typeof saved !== "object" || Array.isArray(saved)) return {};
    return Object.fromEntries(Object.entries(saved).filter(([, value]) => typeof value === "string"));
  } catch {
    return {};
  }
}

export function rememberSession(session: SessionView) {
  try {
    localStorage.setItem(MEMORY_KEY, JSON.stringify({ ...rememberedSessions(), [session.env]: session.id }));
  } catch {
    // Storage restrictions must not prevent session navigation.
  }
}

export function projectName(session: SessionView): string {
  return (session.project || session.project_dir || "").split("/").filter(Boolean).at(-1) || "projects";
}

export function needsAttention(session: SessionView): boolean {
  return session.status === "live" && (session.state === "needs_input" || session.pending != null);
}

export function byAgentName(a: SessionView, b: SessionView): number {
  return a.name.localeCompare(b.name, undefined, { numeric: true }) || a.id.localeCompare(b.id);
}

export function workspaceGroups(envs: EnvView[], sessions: SessionView[]): { id: string; label: string }[] {
  // Keep sessions visible even if an environment temporarily drops out of a snapshot.
  const groups = new Map(envs.map((env) => [env.id, { id: env.id, label: env.label }]));
  for (const session of sessions) {
    if (!groups.has(session.env)) groups.set(session.env, { id: session.env, label: session.env_label });
  }
  return [...groups.values()];
}
