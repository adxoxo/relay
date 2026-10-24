// Activity panel: attention items first, unread styling, click selects a session and marks read,
// and a Mark all read button. Opened from the bell and the mobile Activity tab: a popup on
// desktop, the same list on the phone Activity page (layout="page").

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { getActivity, markActivityRead } from "./api";
import { relativeTime } from "./util";
import type { ActivityItem } from "./types";

interface ActivityPanelProps {
  reloadKey: number;
  onSelectSession: (sessionId: string) => void;
  onChanged: () => void;
  nowSec: number;
  layout?: "popup" | "page";
  // Runs after a loaded list is on screen (the phone page restores its position).
  onLoad?: () => void;
}

// Defensive: some notify sources deliver a JSON object as the title string.
// Unwrap a `{ "title": "..." }` shape so the panel never shows raw JSON.
function displayTitle(raw: string): string {
  const text = raw?.trim() ?? "";
  if (!text.startsWith("{") || !text.includes("title")) return raw;
  try {
    const parsed = JSON.parse(text);
    if (parsed && typeof parsed === "object" && typeof parsed.title === "string") {
      return parsed.title;
    }
  } catch {
    /* not JSON; show the original text */
  }
  return raw;
}

export function ActivityPanel({
  reloadKey,
  onSelectSession,
  onChanged,
  nowSec,
  layout = "popup",
  onLoad,
}: ActivityPanelProps) {
  const [items, setItems] = useState<ActivityItem[]>([]);
  const [loads, setLoads] = useState(0);
  const ref = useRef<HTMLDivElement>(null);

  const load = () => {
    getActivity({ limit: 50 })
      .then((res) => {
        const sorted = [...res.items].sort((a, b) => {
          const attn = Number(b.attention) - Number(a.attention);
          if (attn !== 0) return attn;
          return b.created_at - a.created_at;
        });
        setItems(sorted);
        setLoads((n) => n + 1);
      })
      .catch(() => setItems([]));
  };

  useEffect(load, [reloadKey]);

  useLayoutEffect(() => {
    if (loads > 0) onLoad?.();
  }, [loads, onLoad]);

  const openItem = async (item: ActivityItem) => {
    if (item.session_id) onSelectSession(item.session_id);
    if (!item.read) {
      try {
        await markActivityRead({ ids: [item.id] });
        onChanged();
      } catch {
        /* keep the panel usable if the mark fails */
      }
    }
  };

  const markAll = async () => {
    try {
      await markActivityRead({ all: true });
      onChanged();
      load();
    } catch {
      /* ignore */
    }
  };

  const unreadCount = items.filter((i) => !i.read).length;
  const asPage = layout === "page";
  // The page sits under the "Activity" h1, so its list title is an h2.
  const Title = asPage ? "h2" : "h3";
  const content = (
    <>
      <div className="notification-head">
        <Title>While you were away</Title>
        {unreadCount > 0 ? (
          <button className="mark-all" onClick={() => void markAll()}>
            Mark all read
          </button>
        ) : null}
      </div>
      {items.length === 0 ? (
        <div className="notice-empty">No activity yet.</div>
      ) : (
        items.map((item) => (
          <button
            key={item.id}
            className={`notice${item.read ? "" : " unread"}`}
            onClick={() => void openItem(item)}
          >
            {asPage && !item.read ? <span className="sr-only">Unread. </span> : null}
            <strong>{displayTitle(item.title)}</strong>
            {item.body ? <span className="notice-body">{item.body}</span> : null}
            <small>{relativeTime(item.created_at, nowSec)}</small>
          </button>
        ))
      )}
    </>
  );
  if (asPage) {
    return (
      <section className="notification-panel as-page" aria-label="Recent activity">
        {content}
      </section>
    );
  }
  return (
    <aside className="notification-panel" aria-label="Recent activity" ref={ref}>
      {content}
    </aside>
  );
}
