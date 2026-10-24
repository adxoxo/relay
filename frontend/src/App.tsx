// The shell: left rail, header, stats, workspace tabs, session list + panel, files,
// previews, activity, and mobile bottom nav. Wires live data and URL-hash session
// selection. Leaving Files asks first when the open file has unsaved changes.
// Activity is a popup on desktop and its own page (with a history entry) on phones.

import { lazy, Suspense, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { resumeSession } from "./api";
import { useToast } from "./toast";
import { useAppState, type ConnStatus } from "./useAppState";
import { SessionList } from "./SessionList";
import { AgentDirectory, DirectoryButton, SessionSidebar, WorkspacePicker } from "./AgentNavigation";
import { rememberSession } from "./sessionNavigation";
import { SessionPanel } from "./SessionPanel";
import { NewSessionDialog } from "./NewSessionDialog";
import { EndSessionDialog } from "./EndSessionDialog";
import { ActivityPanel } from "./ActivityPanel";
import { PreviewsPage } from "./PreviewsPage";
import { FilesPage } from "./FilesPage";
import {
  ActivityIcon,
  BellIcon,
  ChatIcon,
  CheckIcon,
  ClockIcon,
  CloseIcon,
  FolderIcon,
  GridIcon,
  RefreshIcon,
  ShieldIcon,
} from "./icons";
import type { SessionView, StreamTopic } from "./types";
import { isPhoneScreen } from "./util";
import type { OfficeMemory } from "./officeState";

const OfficeView = lazy(() => import("./OfficeView"));

type Page = "agents" | "files" | "previews";
type MobileTab = "agents" | "session" | "activity" | "files" | "previews";

const WORKSPACE_KEY = "relay.workspace";
// The phone layout (bottom nav). Matches the 760 px breakpoint in styles.css.
const PHONE = "(max-width: 760px)";
// Wide enough for the session list plus two terminal panes (.dashboard.split in
// workspace.css). Width only: an on-screen keyboard changes the height, and a
// keyboard must never close a pane. A phone (isPhoneScreen) shows one pane.
const SPLIT = "(min-width: 1180px)";
const splitFits = () => matchMedia(SPLIT).matches && !isPhoneScreen();

// One phone visit to the Activity page. Its history entries carry `key`: the list
// entry ({relayActivity}) and the entry of a session opened from the list
// ({relayActivityItem}). Both keep the URL of the entry under the visit, so the page
// or session under the visit never changes while the visit lasts.
interface ActivityVisit {
  key: string;
  // The visit whose item session is under this one (Activity, session, Activity).
  from: ActivityVisit | null;
  // The view under a first visit (from === null): Back restores it.
  chatOpen: boolean;
  scrollY: number;
  // The session opened from this visit's list, and the list position.
  sessionId: string | null;
  listScrollY: number;
}

// What the current visit shows: its list (page) or its item session (session).
type ActivityNav = { key: string; role: "page" | "session" } | null;
type ActivityEntry = { relayActivity?: string; relayActivityItem?: string };

function rootVisit(v: ActivityVisit): ActivityVisit {
  while (v.from) v = v.from;
  return v;
}

// Read the session id from the hash (#/s/<id>).
function hashSessionId(): string | null {
  const m = location.hash.match(/^#\/s\/(.+)$/);
  return m ? decodeURIComponent(m[1]) : null;
}

export function App() {
  const toast = useToast();
  const { state, status, refresh, onTopics } = useAppState(true);

  const [page, setPage] = useState<Page>("agents");
  const [agentView, setAgentView] = useState<"list" | "office">(() => {
    try { return localStorage.getItem("relay.agentView") === "office" ? "office" : "list"; }
    catch { return "list"; }
  });
  const officeMemory = useRef<OfficeMemory>({ slots: {}, pages: {}, workspace: null, paused: false, focusId: null, lastKnown: {} });
  useEffect(() => {
    try { localStorage.setItem("relay.agentView", agentView); } catch { /* navigation still works without storage */ }
  }, [agentView]);
  const [workspace, setWorkspace] = useState<string>(
    () => localStorage.getItem(WORKSPACE_KEY) ?? "all",
  );
  const [selectedId, setSelectedId] = useState<string | null>(() => hashSessionId());
  const [newOpen, setNewOpen] = useState(false);
  const [directoryOpen, setDirectoryOpen] = useState(false);
  const [directoryMotion, setDirectoryMotion] = useState(false);
  const openDirectory = useCallback((animate: boolean) => { setDirectoryMotion(animate); setDirectoryOpen(true); }, []);
  const closeDirectory = useCallback(() => setDirectoryOpen(false), []);
  const [endTarget, setEndTarget] = useState<SessionView | null>(null);
  const [showActivity, setShowActivity] = useState(false);
  const [convReload, setConvReload] = useState(0);
  const [activityReload, setActivityReload] = useState(0);
  const [previewReload, setPreviewReload] = useState(0);
  const [nowSec, setNowSec] = useState(() => Date.now() / 1000);
  const [activityNav, setActivityNavState] = useState<ActivityNav>(null);
  const [narrow, setNarrow] = useState(() => matchMedia(PHONE).matches);
  // Second terminal pane (wide screens). A list pick fills the active pane.
  const [wide, setWide] = useState(splitFits);
  const [split, setSplit] = useState(false);
  const [secondId, setSecondId] = useState<string | null>(null);
  const [activePane, setActivePane] = useState<0 | 1>(0);

  const bellRef = useRef<HTMLButtonElement>(null);
  // Files registers its unsaved-changes guard here.
  const filesGuard = useRef<(() => Promise<boolean>) | null>(null);
  // History and resize handlers read the Activity visit through this ref.
  const activityNavRef = useRef<ActivityNav>(null);
  const setActivityNav = useCallback((nav: ActivityNav) => {
    activityNavRef.current = nav;
    if (nav == null) listLoaded.current = false; // the list unmounts
    itemGoneKey.current = null;
    setActivityNavState(nav);
  }, []);
  // Visits whose history entries can still show Activity (Back, then Forward).
  const visitsRef = useRef(new Map<string, ActivityVisit>());
  // A newer Activity tap supersedes one that still waits on the Files guard.
  const activityOpenGen = useRef(0);
  // A newer history move supersedes a Forward that still waits on the Files guard.
  const reenterGen = useRef(0);
  // The step back after "Keep editing" on a Forward into Activity.
  const skipPop = useRef(false);
  // A scroll position to apply once the next view is laid out.
  const scrollAfter = useRef<number | null>(null);
  // The mounted Activity list has loaded once. Until then its position is not read
  // or applied: the list is still empty.
  const listLoaded = useRef(false);
  // The visit whose item session left the state; its list is on the way back.
  const itemGoneKey = useRef<string | null>(null);
  // The visit whose item session opened the End dialog.
  const endFromVisit = useRef<string | null>(null);
  // The End dialog came from the second pane.
  const endFromSecond = useRef(false);
  const selectedRef = useRef<SessionView | null>(null);
  const pageRef = useRef(page);
  pageRef.current = page;

  // Refresh relative times every 30 s.
  useEffect(() => {
    const t = window.setInterval(() => setNowSec(Date.now() / 1000), 30000);
    return () => window.clearInterval(t);
  }, []);

  // Keep selection, and the phone panel visibility, in sync with the hash
  // (reload of a #/s/<id> link, and ordinary Back/Forward). Activity's own
  // visits never change the hash (they push history state instead), so this
  // never runs during one and never fights showVisit/restoreUnder/exitActivity.
  useEffect(() => {
    const id = hashSessionId();
    if (id) document.body.classList.add("chat-open");
    const onHash = () => {
      const next = hashSessionId();
      setSelectedId(next);
      document.body.classList.toggle("chat-open", next != null);
    };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  // Persist the workspace tab.
  useEffect(() => {
    localStorage.setItem(WORKSPACE_KEY, workspace);
  }, [workspace]);

  // Refetch the open conversation and the activity list on relevant topic changes (throttled 1 s).
  const convThrottle = useRef<number | null>(null);
  useEffect(() => {
    return onTopics((topics: StreamTopic[]) => {
      const touchSessions = topics.includes("sessions");
      const touchActivity = topics.includes("activity");
      if (topics.includes("previews") || topics.includes("checks")) {
        setPreviewReload((v) => v + 1);
      }
      if (!touchSessions && !touchActivity) return;
      if (convThrottle.current != null) return;
      convThrottle.current = window.setTimeout(() => {
        convThrottle.current = null;
        setConvReload((v) => v + 1);
        setActivityReload((v) => v + 1);
      }, 1000);
    });
  }, [onTopics]);

  const selectSession = useCallback((id: string) => {
    location.hash = `#/s/${encodeURIComponent(id)}`;
    setSelectedId(id);
    document.body.classList.add("chat-open");
    setShowActivity(false);
  }, []);

  const clearSelection = useCallback(() => {
    if (location.hash) history.replaceState(null, "", location.pathname + location.search);
    setSelectedId(null);
    document.body.classList.remove("chat-open");
  }, []);

  const sessions = state?.sessions ?? [];
  const envs = state?.envs ?? [];
  const selected = useMemo(
    () => sessions.find((s) => s.id === selectedId) ?? null,
    [sessions, selectedId],
  );
  selectedRef.current = selected;
  useEffect(() => {
    if (!selected) return;
    rememberSession(selected);
    setWorkspace(selected.env);
  }, [selected?.id, selected?.env]);
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k" && !newOpen && !endTarget) {
        event.preventDefault();
        event.stopPropagation();
        openDirectory(false);
      }
    };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, [openDirectory, newOpen, endTarget]);
  const splitOn = split && wide && !narrow;
  // Never the session of pane 1, whatever path selected it (list, hash/Back,
  // Activity): one session never mounts two terminals.
  const second = useMemo(
    () =>
      splitOn && secondId && secondId !== selectedId ? sessions.find((s) => s.id === secondId) ?? null : null,
    [sessions, secondId, selectedId, splitOn],
  );
  // The switch strip: every live session, plus the open one when it ended.
  const switchSessions = useMemo(
    () => sessions.filter((s) => s.status === "live" || s.id === selectedId),
    [sessions, selectedId],
  );

  // An open terminal workspace (tablet, desktop): compact counts, no hero, and
  // panes that fit the visible viewport (workspace.css). Phone CSS ignores it.
  const workspaceOpen = page === "agents" && !activityNav && (selected != null || splitOn);
  useEffect(() => {
    document.body.classList.toggle("workspace-open", workspaceOpen);
  }, [workspaceOpen]);
  useEffect(() => () => document.body.classList.remove("workspace-open"), []);

  useEffect(() => {
    const mq = matchMedia(SPLIT);
    const onChange = () => setWide(splitFits());
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);

  // A list pick fills the active pane. One session never shows in both panes
  // (two tmux clients of one session would fight over its size): a pick of the
  // session in the other pane makes that pane active instead.
  const pickSession = (id: string) => {
    if (splitOn && id === secondId) {
      setActivePane(1);
      return;
    }
    if (splitOn && activePane === 1) {
      if (id === selectedId) setActivePane(0);
      else setSecondId(id);
      return;
    }
    selectSession(id);
  };
  const openSplit = () => {
    setSplit(true);
    setSecondId(null);
    setActivePane(1);
  };
  const closeSplit = () => {
    setSplit(false);
    setSecondId(null);
    setActivePane(0);
  };

  // If the selected session vanished, drop the chat-open class.
  useEffect(() => {
    if (selectedId && !selected && sessions.length > 0) {
      // The session may be ended and out of the 24 h window; keep the id but leave overview.
      if (!sessions.some((s) => s.id === selectedId)) {
        document.body.classList.remove("chat-open");
      }
    }
  }, [selectedId, selected, sessions]);

  // Close the activity panel on outside click / Esc.
  useEffect(() => {
    if (!showActivity) return;
    const onClick = (e: MouseEvent) => {
      const target = e.target as HTMLElement;
      if (target.closest(".notification-panel,.bell,.mobile-nav,[data-activity-toggle]")) return;
      setShowActivity(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setShowActivity(false);
    };
    document.addEventListener("click", onClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("click", onClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [showActivity]);

  // Resolves false when the user keeps editing an unsaved file.
  const leaveFiles = useCallback(async (): Promise<boolean> => {
    if (pageRef.current !== "files" || !filesGuard.current) return true;
    return filesGuard.current();
  }, []);

  // Show the list or the item session of a visit. A list that has not loaded yet gets
  // its position when it loads (onActivityLoad).
  const showVisit = useCallback(
    (visit: ActivityVisit, role: "page" | "session") => {
      document.body.classList.toggle("chat-open", role === "session");
      setShowActivity(false);
      setActivityNav({ key: visit.key, role });
      scrollAfter.current = role === "page" && listLoaded.current ? visit.listScrollY : 0;
    },
    [setActivityNav],
  );

  // The first load of a mounted list: the list shows its saved position now.
  const onActivityLoad = useCallback(() => {
    if (listLoaded.current) return;
    listLoaded.current = true;
    const nav = activityNavRef.current;
    const visit = nav?.role === "page" ? visitsRef.current.get(nav.key) : undefined;
    if (visit) window.scrollTo(0, visit.listScrollY);
  }, []);

  // Show the view under a visit again, with its session layout and scroll position.
  // That view stays mounted and unchanged during the visit.
  const restoreUnder = useCallback((visit: ActivityVisit) => {
    const root = rootVisit(visit);
    document.body.classList.toggle("chat-open", root.chatOpen && selectedRef.current != null);
    scrollAfter.current = root.scrollY;
  }, []);

  // A bottom tab leaves the visit: the view under it comes back for the tab action,
  // and the entries of the visit become inert.
  const exitActivity = useCallback(() => {
    const nav = activityNavRef.current;
    let v = nav ? visitsRef.current.get(nav.key) : undefined;
    if (!v) return;
    document.body.classList.toggle("chat-open", rootVisit(v).chatOpen && selectedRef.current != null);
    history.scrollRestoration = "auto"; // the entry now holds an ordinary view
    while (v) {
      visitsRef.current.delete(v.key);
      v = v.from ?? undefined;
    }
    setActivityNav(null);
  }, [setActivityNav]);

  // Apply a scroll position saved by an Activity transition after the view is laid out.
  // Activity entries use manual scroll restoration, so the browser does not move them
  // afterwards to a position saved from an incomplete list.
  useLayoutEffect(() => {
    if (scrollAfter.current == null) return;
    window.scrollTo(0, scrollAfter.current);
    scrollAfter.current = null;
  });

  // Phone Back and Forward through Activity entries. The listener runs in the capture
  // phase, before the Files listener, and stops the moves it handles, so Files does not
  // read them as its own Back. Entries of a visit that a tab, a widen, or a reload
  // ended are inert here.
  useEffect(() => {
    const onPop = (e: PopStateEvent) => {
      reenterGen.current += 1;
      if (skipPop.current) {
        skipPop.current = false;
        e.stopImmediatePropagation();
        return;
      }
      if (!matchMedia(PHONE).matches) return;
      const st = history.state as ActivityEntry | null;
      const key = st?.relayActivity ?? st?.relayActivityItem;
      const target = key ? visitsRef.current.get(key) : undefined;
      const nav = activityNavRef.current;
      const current = nav ? visitsRef.current.get(nav.key) : undefined;
      if (current && nav?.role === "page" && listLoaded.current) current.listScrollY = window.scrollY;
      if (target) {
        e.stopImmediatePropagation();
        const role = st?.relayActivity ? "page" : "session";
        if (current) {
          showVisit(target, role);
          return;
        }
        // Forward from the view under the visit. Files asks first about unsaved changes.
        const root = rootVisit(target);
        const chatOpen = document.body.classList.contains("chat-open");
        const scrollY = window.scrollY;
        const gen = reenterGen.current;
        void leaveFiles().then((ok) => {
          // A later move or a widen (it clears the visits) wins over this Forward.
          if (gen !== reenterGen.current || !visitsRef.current.has(target.key)) return;
          if (!ok) {
            // Keep editing: step back to the Files entry.
            skipPop.current = true;
            history.back();
            return;
          }
          root.chatOpen = chatOpen;
          root.scrollY = scrollY;
          showVisit(target, role);
        });
        return;
      }
      if (!current) return;
      // Back from the visit to the view under it. The visit stays for Forward.
      e.stopImmediatePropagation();
      restoreUnder(current);
      setActivityNav(null);
    };
    window.addEventListener("popstate", onPop, true);
    return () => window.removeEventListener("popstate", onPop, true);
  }, [leaveFiles, restoreUnder, setActivityNav, showVisit]);

  // Wider than the phone layout: the visit ends, the view under it comes back, and every
  // Activity entry becomes inert. The desktop bell opens the popup again.
  useEffect(() => {
    const mq = matchMedia(PHONE);
    const onChange = () => {
      setNarrow(mq.matches);
      if (mq.matches) return;
      const nav = activityNavRef.current;
      const current = nav ? visitsRef.current.get(nav.key) : undefined;
      visitsRef.current.clear();
      if (!current) return;
      history.scrollRestoration = "auto"; // the entry now holds an ordinary view
      setActivityNav(null);
      restoreUnder(current);
    };
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, [restoreUnder, setActivityNav]);

  const onResume = async (s: SessionView) => {
    try {
      const created = await resumeSession(s.id);
      toast.show(`Resumed ${created.name}.`);
      refresh();
      selectSession(created.id);
    } catch {
      toast.error("Could not resume this session.");
    }
  };

  const counts = state?.counts;
  const unread = state?.activity_unread ?? 0;
  const hostName = state?.host.name ?? "workspace";
  const secure = location.protocol === "https:";

  const activityVisit = activityNav ? visitsRef.current.get(activityNav.key) : undefined;
  const activityPage = activityNav?.role === "page";
  const activitySession = activityNav?.role === "session";
  // The item session must be in the current state; else the list shows.
  const itemSession =
    activitySession && activityVisit?.sessionId ? sessions.find((x) => x.id === activityVisit.sessionId) : undefined;
  const listShown = activityPage || (activitySession && !itemSession);

  // The item session left the current state (or Forward reached its stale entry): the
  // list shows at once, and one step back returns to the list entry.
  useLayoutEffect(() => {
    if (!state || !activitySession || itemSession || !activityVisit) return;
    if (itemGoneKey.current === activityVisit.key) return;
    itemGoneKey.current = activityVisit.key;
    document.body.classList.remove("chat-open");
    if ((history.state as ActivityEntry | null)?.relayActivityItem === activityVisit.key) history.back();
    else showVisit(activityVisit, "page");
  });
  // The current visit and the visits under it. Each keeps its item session mounted.
  const visitChain: ActivityVisit[] = [];
  for (let v = activityVisit; v; v = v.from ?? undefined) visitChain.push(v);

  const mobileTab: MobileTab = listShown || showActivity
    ? "activity"
    : activitySession
      ? "session"
      : page === "previews"
        ? "previews"
        : page === "files"
          ? "files"
          : selectedId && document.body.classList.contains("chat-open")
            ? "session"
            : "agents";

  const goAgents = async () => {
    if (!(await leaveFiles())) return;
    exitActivity();
    setPage("agents");
    clearSelection();
    setShowActivity(false);
  };

  const goPage = async (next: "files" | "previews") => {
    if (next === page) {
      exitActivity();
      setShowActivity(false);
      return;
    }
    if (!(await leaveFiles())) return;
    exitActivity();
    setPage(next);
    clearSelection();
    setShowActivity(false);
    window.scrollTo(0, 0);
  };

  // Phone bell and Activity tab: open the Activity page through the Files guard, with
  // one history entry. A re-tap keeps the page. Desktop keeps the popup toggle.
  const openActivityPage = async () => {
    const pageOpen = () => activityNavRef.current?.role === "page";
    if (pageOpen()) return;
    const gen = ++activityOpenGen.current;
    if (!(await leaveFiles())) return;
    if (gen !== activityOpenGen.current || pageOpen() || !matchMedia(PHONE).matches) return;
    const nav = activityNavRef.current;
    const visit: ActivityVisit = {
      key: `${Date.now()}.${gen}`,
      from: (nav && visitsRef.current.get(nav.key)) ?? null,
      chatOpen: document.body.classList.contains("chat-open"),
      scrollY: window.scrollY,
      sessionId: null,
      listScrollY: 0,
    };
    visitsRef.current.set(visit.key, visit);
    history.pushState({ relayActivity: visit.key }, "");
    history.scrollRestoration = "manual"; // per entry; the item entry pushed from here copies it
    showVisit(visit, "page");
  };

  const onActivityTap = () => {
    if (matchMedia(PHONE).matches) void openActivityPage();
    else setShowActivity((v) => !v);
  };

  // An item on the Activity page opens its session in its own panel and history entry,
  // so the Back arrow and phone Back return to the list at the same position. A session
  // that is not in the current state (ended more than 24 h ago) does not open.
  const openFromActivity = (id: string) => {
    const nav = activityNavRef.current;
    const visit = nav?.role === "page" ? visitsRef.current.get(nav.key) : undefined;
    if (!visit) return;
    if (!sessions.some((x) => x.id === id)) {
      toast.show("This session is no longer available.");
      return;
    }
    if (listLoaded.current) visit.listScrollY = window.scrollY;
    visit.sessionId = id;
    history.pushState({ relayActivityItem: visit.key }, "");
    showVisit(visit, "session");
  };

  const onActivityChanged = () => {
    refresh();
    setActivityReload((v) => v + 1);
  };

  const officeOverview = agentView === "office" && !selected && !splitOn;
  const viewControl = <div className="agent-view-control" role="group" aria-label="Agent view">
    <button aria-pressed={agentView === "list"} onClick={() => setAgentView("list")}><GridIcon />List</button>
    <button aria-pressed={agentView === "office"} onClick={() => setAgentView("office")}><svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="1.6"><path d="M3 21V7l9-4 9 4v14M8 9h2M14 9h2M8 13h2M14 13h2M9 21v-4h6v4" /></svg>Office</button>
  </div>;

  return (
    <div className="app-shell">
      <aside className="rail" aria-label="Main navigation">
        <div className="rail-brand"><div className="logo" aria-label="relay"><ActivityIcon /></div><span>relay</span></div>
        <div className="rail-nav">
          <button
            className={`rail-button${page === "agents" ? " active" : ""}`}
            title="Agents"
            aria-label="Agents"
            aria-current={page === "agents" ? "page" : undefined}
            onClick={() => void goAgents()}
          >
            <GridIcon /><span className="rail-label">Agents</span>
          </button>
          <button
            className={`rail-button${page === "files" ? " active" : ""}`}
            title="Files"
            aria-label="Files"
            aria-current={page === "files" ? "page" : undefined}
            onClick={() => void goPage("files")}
          >
            <FolderIcon /><span className="rail-label">Files</span>
          </button>
          <button
            className={`rail-button${page === "previews" ? " active" : ""}`}
            title="Previews"
            aria-label="Previews"
            aria-current={page === "previews" ? "page" : undefined}
            onClick={() => void goPage("previews")}
          >
            <RefreshIcon /><span className="rail-label">Previews</span>
          </button>
        </div>
        {page === "agents" ? <SessionSidebar envs={envs} sessions={sessions} selectedId={selectedId}
          secondId={splitOn ? secondId : null} onSelect={pickSession} /> : null}
        <div className="rail-bottom">
          <div className="avatar" title="Your workspace">
            AY
          </div>
        </div>
      </aside>

      <main className={activityNav ? "main activity-open" : "main"}>
        <header className="topbar">
          <div className="wordmark">
            {!narrow && page === "agents" && selected ? <><span className={`workspace-dot ${selected.env}`} />{selected.env_label}<span className="breadcrumb-slash">/</span>{selected.name}</> : <>relay<span>Your agent workspace</span></>}
          </div>
          <div className="top-right">
            <DirectoryButton sessions={sessions} onClick={openDirectory} />
            <div className="host">
              <span className="dot" />
              {hostName}
            </div>
            <ConnBadge status={status} />
            <button
              className="bell"
              ref={bellRef}
              data-activity-toggle
              aria-label={`Notifications${unread > 0 ? `, ${unread} unread` : ""}`}
              aria-expanded={narrow ? undefined : showActivity}
              onClick={onActivityTap}
            >
              <BellIcon />
              {unread > 0 ? <span className="bell-badge">{unread}</span> : null}
            </button>
          </div>
        </header>

        {/* Phone Activity page and the sessions opened from it. The list stays mounted,
            hidden, while an item session shows, so Back returns to the same position. The
            view under the visit (below) stays mounted and unchanged, hidden by .activity-open. */}
        {activityNav ? (
          <section className="view-activity" aria-labelledby="activity-title" hidden={!listShown}>
            <div className="page-head">
              <div>
                <h1 id="activity-title">Activity</h1>
              </div>
            </div>
            <ActivityPanel
              layout="page"
              reloadKey={activityReload}
              nowSec={nowSec}
              onSelectSession={openFromActivity}
              onChanged={onActivityChanged}
              onLoad={onActivityLoad}
            />
          </section>
        ) : null}
        {visitChain.map((v) => {
          const s = v.sessionId ? sessions.find((x) => x.id === v.sessionId) : undefined;
          return s ? (
            <div key={`${v.key}:${s.id}`} className="activity-session" hidden={!(activitySession && v === activityVisit)}>
              <SessionPanel
                session={s}
                reloadKey={convReload}
                visible={!!activitySession && v === activityVisit}
                onBack={() => history.back()}
                backLabel="Back to Activity"
                onResume={(x) => void onResume(x)}
                envs={envs}
                onDirectory={openDirectory}
                onEmptyWorkspace={(id) => { exitActivity(); setPage("agents"); clearSelection(); setWorkspace(id); }}
                switchSessions={sessions.filter((x) => x.status === "live" || x.id === s.id)}
                onSwitch={(id) => {
                  // A switch leaves the Activity visit for the Agents workspace.
                  exitActivity();
                  setPage("agents");
                  selectSession(id);
                }}
                onNew={() => setNewOpen(true)}
                onEnd={(x) => {
                  endFromVisit.current = v.key;
                  setEndTarget(x);
                }}
                onRefresh={() => {
                  refresh();
                  setConvReload((n) => n + 1);
                }}
              />
            </div>
          ) : null;
        })}

        {page === "previews" ? (
          <PreviewsPage envs={envs} reloadKey={previewReload} />
        ) : page === "files" ? (
          envs.length ? (
            <FilesPage envs={envs} guardRef={filesGuard} />
          ) : (
            <div className="empty-state">Loading workspaces…</div>
          )
        ) : (
          <>
            {agentView === "office" ? <div className="office-holder" hidden={!officeOverview}>
              <Suspense fallback={<div className="empty-state">Loading office…</div>}>
                <OfficeView envs={envs} sessions={sessions} connected={status === "live"} generatedAt={state?.generated_at ?? null}
                  visible={officeOverview && !activityNav} obscured={newOpen || directoryOpen || endTarget != null || showActivity} memory={officeMemory.current} viewControl={viewControl}
                  onSelect={selectSession} onNew={(env) => { setWorkspace(env); setNewOpen(true); }} onRefresh={refresh} />
              </Suspense>
            </div> : null}
            {!officeOverview ? <>
            {/* Phone only (styles.css): a short heading and a compact counts row
                replace the hero and the four-card metrics grid below. Unread and
                completion counts stay reachable through Activity. */}
            <section className="phone-head">
              <h1>Your agents</h1>
              <div className="phone-counts">
                <span>
                  <b>{counts?.sessions ?? 0}</b> sessions
                </span>
                <span>
                  <b>{counts?.working ?? 0}</b> active
                </span>
                <span>
                  <b>{counts?.needs_input ?? 0}</b> need input
                </span>
              </div>
            </section>

            <section className="intro">
              <div>
                
                <h1>Your agents</h1>
                <p>Every session, in its own workspace.</p>
              </div>
              <div className="connection">
                <ShieldIcon />
                {secure ? "Private connection" : "Local connection"}
              </div>
            </section>

            <section className="metrics" aria-label="Agent overview">
              <Metric icon={<GridIcon />} value={counts?.sessions ?? 0} label="Agent sessions" />
              <Metric
                icon={<ActivityIcon />}
                tone="green"
                value={counts?.working ?? 0}
                label="Working now"
              />
              <Metric
                icon={<ClockIcon />}
                tone="orange"
                value={counts?.needs_input ?? 0}
                label="Needs your input"
                badge={counts && counts.needs_input > 0 ? "Your turn" : undefined}
              />
              <Metric
                icon={<CheckIcon />}
                value={counts?.finished_unread ?? 0}
                label="Turns finished"
              />
            </section>

            {!selected && !splitOn ? <div className="list-view-control">{viewControl}</div> : null}
            <div className="workspace-line">
              <WorkspacePicker envs={envs} sessions={sessions} value={workspace} onChange={setWorkspace} all />
              <span className="sync">
                <RefreshIcon />
                {status === "live" ? "Live" : "Reconnecting"}
              </span>
            </div>

            <div className={`${splitOn ? "dashboard split" : "dashboard"}${selected ? " has-session" : ""}`}>
              <SessionList
                workspace={workspace}
                envs={envs}
                sessions={sessions}
                selectedId={selectedId}
                secondId={splitOn ? secondId : null}
                onSelect={pickSession}
                onNew={() => setNewOpen(true)}
                onResume={(s) => void onResume(s)}
                nowSec={nowSec}
              />
              {selected ? (
                <SessionPanel
                  session={selected}
                  reloadKey={convReload}
                  visible={!activityNav}
                  onBack={() => {
                    if (agentView === "office") {
                      officeMemory.current.focusId = selected.id;
                      closeSplit();
                    }
                    clearSelection();
                  }}
                  backLabel={agentView === "office" ? "Back to Office" : undefined}
                  onEnd={(s) => {
                    endFromSecond.current = false;
                    setEndTarget(s);
                  }}
                  onRefresh={() => {
                    refresh();
                    setConvReload((v) => v + 1);
                  }}
                  onResume={(s) => void onResume(s)}
                  envs={envs}
                  onDirectory={openDirectory}
                  onEmptyWorkspace={(id) => { exitActivity(); setPage("agents"); clearSelection(); setWorkspace(id); }}
                  switchSessions={switchSessions}
                  onSwitch={selectSession}
                  onNew={() => setNewOpen(true)}
                  onSplit={wide && !narrow && !splitOn ? openSplit : undefined}
                  active={splitOn && activePane === 0}
                  onActivate={() => setActivePane(0)}
                />
              ) : (
                <section
                  className={`conversation${splitOn && activePane === 0 ? " active-pane" : ""}`}
                  aria-label="Selected agent"
                  onPointerDownCapture={() => setActivePane(0)}
                >
                  <div className="empty-state">
                    A workspace for your next idea.
                    <button className="button primary" onClick={() => setNewOpen(true)}>
                      New session
                    </button>
                  </div>
                </section>
              )}
              {splitOn ? (
                second ? (
                  <SessionPanel
                    session={second}
                    reloadKey={convReload}
                    visible={!activityNav}
                    onBack={closeSplit}
                    onEnd={(s) => {
                      endFromSecond.current = true;
                      setEndTarget(s);
                    }}
                    onRefresh={() => {
                      refresh();
                      setConvReload((v) => v + 1);
                    }}
                    onResume={(s) => void onResume(s)}
                    onClosePane={closeSplit}
                    active={activePane === 1}
                    onActivate={() => setActivePane(1)}
                  />
                ) : (
                  <section
                    className={`conversation pane-empty${activePane === 1 ? " active-pane" : ""}`}
                    aria-label="Second pane"
                    onPointerDownCapture={() => setActivePane(1)}
                  >
                    <div className="empty-state">
                      Pick a session in the list. It opens in this pane.
                      <button className="button" onClick={closeSplit}>
                        <CloseIcon />
                        Close pane
                      </button>
                    </div>
                  </section>
                )
              ) : null}
            </div>

            <footer className="bottom-info">
              <span>
                <span className="dot" style={{ width: 5, height: 5 }} />
                Designed for your Ubuntu and Podman workspace
              </span>
              <span>
                <BellIcon />
                Session activity
              </span>
            </footer>
            </> : null}
          </>
        )}
      </main>

      {directoryOpen ? <AgentDirectory envs={envs} sessions={sessions} currentId={activitySession ? itemSession?.id ?? null : listShown ? null : splitOn && activePane === 1 ? secondId : selectedId}
        animate={directoryMotion} onClose={closeDirectory}
        onSelect={(id) => {
          closeDirectory();
          void leaveFiles().then((ok) => { if (!ok) return; exitActivity(); setPage("agents"); setShowActivity(false); pickSession(id); });
        }}
        onNew={() => {
          closeDirectory();
          void leaveFiles().then((ok) => {
            if (!ok) return;
            exitActivity(); setPage("agents"); setShowActivity(false); setNewOpen(true);
          });
        }} /> : null}

      {showActivity ? (
        <ActivityPanel
          reloadKey={activityReload}
          nowSec={nowSec}
          onSelectSession={(id) => {
            void leaveFiles().then((ok) => {
              if (!ok) return;
              setPage("agents");
              selectSession(id);
            });
          }}
          onChanged={onActivityChanged}
        />
      ) : null}

      <nav className="mobile-nav" aria-label="Mobile navigation">
        <button
          className={mobileTab === "agents" ? "active" : ""}
          aria-current={mobileTab === "agents" ? "page" : undefined}
          onClick={() => void goAgents()}
        >
          <GridIcon />
          Agents
        </button>
        <button
          className={mobileTab === "session" ? "active" : ""}
          aria-current={mobileTab === "session" ? "page" : undefined}
          onClick={() => {
            // A session opened from Activity is the open session already.
            if (activityNavRef.current?.role === "session") return;
            void leaveFiles().then((ok) => {
              if (!ok) return;
              exitActivity();
              if (selectedId) {
                setPage("agents");
                document.body.classList.add("chat-open");
                setShowActivity(false);
              } else {
                setNewOpen(true);
              }
            });
          }}
        >
          <ChatIcon />
          Session
        </button>
        <button
          className={mobileTab === "activity" ? "active" : ""}
          aria-current={listShown ? "page" : undefined}
          data-activity-toggle
          onClick={onActivityTap}
        >
          <BellIcon />
          {unread > 0 ? <span className="nav-badge">{unread}</span> : null}
          Activity
        </button>
        <button
          className={mobileTab === "files" ? "active" : ""}
          aria-current={mobileTab === "files" ? "page" : undefined}
          onClick={() => void goPage("files")}
        >
          <FolderIcon />
          Files
        </button>
        <button
          className={mobileTab === "previews" ? "active" : ""}
          aria-current={mobileTab === "previews" ? "page" : undefined}
          onClick={() => void goPage("previews")}
        >
          <RefreshIcon />
          Previews
        </button>
      </nav>

      <NewSessionDialog
        open={newOpen}
        onClose={() => setNewOpen(false)}
        envs={envs}
        defaultEnv={workspace === "all" ? envs[0]?.id ?? "work1" : workspace}
        onCreated={(s) => {
          setNewOpen(false);
          exitActivity();
          setPage("agents");
          refresh();
          selectSession(s.id);
        }}
      />

      <EndSessionDialog
        open={endTarget != null}
        session={endTarget}
        onClose={() => {
          endFromVisit.current = null;
          endFromSecond.current = false;
          setEndTarget(null);
        }}
        onEnded={() => {
          setEndTarget(null);
          refresh();
          const fromVisit = endFromVisit.current;
          endFromVisit.current = null;
          const fromSecond = endFromSecond.current;
          endFromSecond.current = false;
          if (fromSecond) {
            // The second pane ended its session: that pane empties, the first keeps its session.
            setSecondId(null);
          } else if (fromVisit == null) {
            clearSelection();
          } else if (activityNavRef.current?.role === "session" && activityNavRef.current.key === fromVisit) {
            // The ended item session still shows: return to its list. Nothing else changes,
            // so the view under the visit keeps its selection.
            history.back();
          }
        }}
        onScopeChanged={() => {
          refresh();
        }}
      />
    </div>
  );
}

function ConnBadge({ status }: { status: ConnStatus }) {
  return (
    <div
      className={`conn-indicator${status === "reconnecting" ? " reconnecting" : ""}`}
      role="status"
    >
      <span className="dot" />
      {status === "live" ? "Live" : "Reconnecting"}
    </div>
  );
}

interface MetricProps {
  icon: React.ReactNode;
  value: number;
  label: string;
  tone?: "green" | "orange";
  badge?: string;
}

function Metric({ icon, value, label, tone, badge }: MetricProps) {
  return (
    <div className="metric">
      <div className={`metric-icon${tone ? ` ${tone}` : ""}`}>{icon}</div>
      <div>
        <strong>{value}</strong>
        <span>{label}</span>
      </div>
      {badge ? <small>{badge}</small> : null}
    </div>
  );
}
