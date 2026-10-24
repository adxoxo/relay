// Unsent terminal text, one draft per exact session id. The draft survives a
// session switch, a closed pane, a reconnect and a reload of this tab
// (sessionStorage). Only a successful send or the user's own edit clears it.

const PREFIX = "relay.draft.";
const memory = new Map<string, string>();

export function getDraft(sessionId: string): string {
  const cached = memory.get(sessionId);
  if (cached != null) return cached;
  try {
    return sessionStorage.getItem(PREFIX + sessionId) ?? "";
  } catch {
    return "";
  }
}

export function setDraft(sessionId: string, text: string): void {
  if (text) memory.set(sessionId, text);
  else memory.delete(sessionId);
  try {
    if (text) sessionStorage.setItem(PREFIX + sessionId, text);
    else sessionStorage.removeItem(PREFIX + sessionId);
  } catch {
    /* storage full or blocked: the in-memory draft still holds */
  }
}
