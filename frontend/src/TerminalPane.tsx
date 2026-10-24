// Terminal tab: xterm.js bound to the relay terminal WebSocket.
// Binary frames -> term.write; term.onData -> {type:'input'}; ResizeObserver -> debounced resize.
// Heartbeat: {type:'ping'} every 10 s; a missing pong (8 s) or a wake from the
// background with no answer (4 s) replaces the socket at once.
// History: a swipe (touch) or the wheel sends {type:'scroll', lines, epoch}.
// - The pane has older tmux output (a shell): the server scrolls tmux copy mode
//   and answers {type:'scroll', active, position, history}.
// - It has none (an agent TUI on the alternate screen): the server leaves copy
//   mode and sends pages of the session transcript as {type:'history', epoch, ...}.
//   They show as plain text in a scroll view over the live terminal, which stays
//   mounted and keeps its output. Older asks for the page before `before`.
//   The view keeps at most HISTORY_TEXT_CAP of text: the first page drops its
//   oldest items, an Older page drops the newest items. A note marks each cut.
// Back to live, a key, a reconnect, or a new session ends the view and bumps the
// epoch, so a late history frame (older epoch) is dropped.
// Reconnect backoff 1s/2s/5s; the overlay shows only after a short grace period.
// Input: nothing is queued. While the socket is not open, the key bar, the
// text field's Send and Insert, and direct typing are off, so a reconnect
// never replays input. The text field (touch) keeps an unsent draft per
// session (drafts.ts). Send pastes it as one paste (bracketed when the program
// asked for it), then presses Enter after a short pause, as the prompt API does.

import { Fragment, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties, KeyboardEvent as ReactKeyboardEvent, SyntheticEvent } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";
import "./TerminalPane.css";
import { getDraft, setDraft } from "./drafts";
import { BackIcon } from "./icons";
import type { SessionView } from "./types";
import { isPhoneScreen } from "./util";

interface TerminalPaneProps {
  session: SessionView;
}

const RECONNECT_DELAYS = [1000, 2000, 5000];
// Pause between a bracketed paste and its Enter, as PASTE_SCRIPT in tmux.py:
// an agent TUI that gets Enter inside the paste burst can take it as a newline.
const PASTE_ENTER_DELAY_MS = 300;
// The bridge cuts one input frame at 64 KiB; refuse text that would be cut.
const MAX_SEND_BYTES = 60 * 1024;
const HEARTBEAT_MS = 10000;
const PONG_TIMEOUT_MS = 8000;
const WAKE_PROBE_MS = 4000;
const OVERLAY_GRACE_MS = 1200;
const RESIZE_DEBOUNCE_MS = 200;
const SWIPE_SLOP_PX = 10;
const HISTORY_TEXT_CAP = 128 * 1024; // UTF-8 bytes of transcript text kept in the history view
const NOTE_MS = 6000;
const TRIMMED_NOTE = "Newer messages were removed from this view to keep it small. Back to live shows the current screen.";
const CUT_NOTE = "Earlier messages were removed here to keep this view small.";

type Phase = "connecting" | "open" | "reconnecting" | "exited";
type HistoryRole = "user" | "assistant" | "tool";

interface HistoryItem {
  key: string;
  role: HistoryRole;
  text: string;
  bytes: number;
}

interface HistoryView {
  status: "loading" | "ready";
  items: HistoryItem[]; // oldest first
  before: number | null; // cursor for the Older request
  more: boolean;
  note: string | null; // shown at the top: start of the conversation, gaps, failures
  trimmed: boolean; // newer items were dropped to stay under HISTORY_TEXT_CAP
  cutBefore: string | null; // key of the item after the oldest first-page items that were dropped
  olderLoading: boolean;
}

interface HistoryFrame {
  type: "history";
  epoch?: number;
  status?: "loading" | "page" | "none";
  first?: boolean;
  items?: { role?: string; text?: string }[];
  before?: number;
  more?: boolean;
  note?: string | null;
}

interface Box {
  top: number;
  left: number;
  width: number;
  height: number;
}

const ROLE_LABEL: Record<HistoryRole, string> = { user: "You", assistant: "Agent", tool: "Tool" };
const encoder = new TextEncoder();

// Plain typing on the focused history view goes to the terminal. Navigation keys
// scroll the view; Tab moves focus; modifier shortcuts (copy) stay with the browser.
function typedInput(e: ReactKeyboardEvent): string | null {
  if (e.ctrlKey || e.metaKey || e.altKey) return null;
  switch (e.key) {
    case "Enter":
      return "\r";
    case "Backspace":
      return "\x7f";
    case "ArrowLeft":
      return "\x1b[D";
    case "ArrowRight":
      return "\x1b[C";
  }
  return e.key.length === 1 ? e.key : null;
}

// Key-bar and text-field buttons keep the focus where it is: a tap neither
// opens the on-screen keyboard nor closes it.
const keepFocus = (e: SyntheticEvent) => e.preventDefault();

type SendResult = "sent" | "offline" | "too-long";

export function TerminalPane({ session }: TerminalPaneProps) {
  const s = session;
  const hostRef = useRef<HTMLDivElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const keyBarRef = useRef<HTMLDivElement>(null);
  const [phase, setPhase] = useState<Phase>("connecting");
  const [showOverlay, setShowOverlay] = useState(false);
  const [exitMessage, setExitMessage] = useState<string | null>(null);
  const [scrolled, setScrolled] = useState(false);
  const [history, setHistory] = useState<HistoryView | null>(null);
  const [liveNote, setLiveNote] = useState<string | null>(null);
  const [box, setBox] = useState<Box | null>(null);
  const [draft, setDraftState] = useState(() => getDraft(s.id));
  const [sending, setSending] = useState(false);
  // After a wake from the background: the socket looks open but is unproven
  // until any frame answers the probe. Input waits, as while disconnected.
  const [verifying, setVerifying] = useState(false);
  const [verifySlow, setVerifySlow] = useState(false);
  const [composeNote, setComposeNote] = useState<string | null>(null);
  const termRef = useRef<Terminal | null>(null);
  const sendRef = useRef<(obj: unknown) => void>(() => {});
  const sendTextRef = useRef<(text: string, submit: boolean, done: (enterSent: boolean) => void) => SendResult>(
    () => "offline",
  );
  const leaveRef = useRef<() => void>(() => {});
  const openHistoryRef = useRef<() => void>(() => {});
  const historyElRef = useRef<HTMLDivElement>(null);
  const epochRef = useRef(1);
  // Where to put the history scroll position after the next render: the bottom
  // (first page), or the same screen position for an item (after Older).
  const anchorRef = useRef<"bottom" | { key: string; offset: number } | null>(null);
  const isTouch = useMemo(
    () => matchMedia("(pointer: coarse)").matches || "ontouchstart" in window,
    [],
  );
  // Text size from the physical device, fixed for the pane's life: the keyboard
  // or a rotation never changes it. The history view uses the same size.
  const isPhone = useMemo(() => isPhoneScreen(), []);
  const fontSize = isPhone ? 10 : isTouch ? 11 : 12;
  const wrapStyle = useMemo(() => ({ "--term-font": `${fontSize}px` }) as CSSProperties, [fontSize]);

  // Show the reconnect overlay only when the gap lasts; a quick reconnect keeps the old screen.
  useEffect(() => {
    if (phase === "open" || phase === "exited") {
      setShowOverlay(false);
      return;
    }
    const t = window.setTimeout(() => setShowOverlay(true), OVERLAY_GRACE_MS);
    return () => window.clearTimeout(t);
  }, [phase]);

  // A wake probe that a live socket answers at once shows no note; a slow one does.
  useEffect(() => {
    if (!verifying) {
      setVerifySlow(false);
      return;
    }
    const t = window.setTimeout(() => setVerifySlow(true), OVERLAY_GRACE_MS);
    return () => window.clearTimeout(t);
  }, [verifying]);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;

    const term = new Terminal({
      convertEol: false,
      cursorBlink: true,
      fontSize,
      fontFamily: "ui-monospace, SFMono-Regular, monospace",
      theme: { background: "#20211f", foreground: "#d7d8d1", cursor: "#e8ac73" },
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(host);
    termRef.current = term;
    try {
      fit.fit();
    } catch {
      /* container may not be measured yet */
    }

    let ws: WebSocket | null = null;
    let attempt = 0;
    let closedByUs = false;
    let reconnectTimer: number | null = null;
    let resizeTimer: number | null = null;
    let probeTimer: number | null = null;
    let noteTimer: number | null = null;
    let awaitingSince: number | null = null; // oldest unanswered ping
    let lastCols = term.cols;
    let lastRows = term.rows;
    // History state shared by the handlers below.
    let scrollPending = 0;
    let scrollTimer: number | null = null;
    let wheelRest = 0;
    let touchY: number | null = null;
    let touchRest = 0;
    let swiping = false;
    let historyOpen = false; // a history view is loading or shown
    let scrollSent = false; // a scroll request went out in this epoch
    let noteShown = false;
    let pageSeq = 0;
    // The Enter of a Send that waits after its paste, and the socket it belongs to.
    let enterTimer: number | null = null;
    let enterSocket: WebSocket | null = null;
    let enterDone: ((enterSent: boolean) => void) | null = null;

    const sendJson = (obj: unknown) => {
      if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
    };
    sendRef.current = sendJson;
    // Input (direct typing, keys, Insert, Send) is on only when the socket is
    // open, the bridge sent its 'ready' frame, and no wake probe is waiting.
    // Nothing is queued, so nothing is ever replayed.
    let bridgeReady = false;
    let probeNonce: string | null = null; // the wake probe that must be answered
    let probeSeq = 0;
    const inputOpen = () => !!ws && ws.readyState === WebSocket.OPEN && bridgeReady && probeNonce == null;
    const syncInput = () => {
      term.options.disableStdin = !inputOpen();
    };
    syncInput();
    const setProbe = (nonce: string | null) => {
      probeNonce = nonce;
      syncInput();
      setVerifying(nonce != null);
    };
    let enterHeld = false; // the Enter came due during a probe on its own socket

    // The text field's Insert (submit false) and Send (submit true). The text
    // goes out once, on the socket that is open now, or not at all. A delayed
    // Enter belongs to the socket of its paste and never moves to another one.
    const finishEnter = () => {
      enterTimer = null;
      const socket = enterSocket;
      if (socket != null && socket === ws && probeNonce != null) {
        enterHeld = true; // the probe decides: answered -> sent, replaced -> dropped
        return;
      }
      const done = enterDone;
      enterSocket = null;
      enterDone = null;
      enterHeld = false;
      const ok = socket != null && socket.readyState === WebSocket.OPEN;
      if (ok) socket.send(JSON.stringify({ type: "input", data: "\r" }));
      done?.(ok);
    };
    const dropEnter = () => {
      if (enterSocket == null) return;
      if (enterTimer != null) window.clearTimeout(enterTimer);
      const done = enterDone;
      enterTimer = null;
      enterSocket = null;
      enterDone = null;
      enterHeld = false;
      done?.(false);
    };
    sendTextRef.current = (text, submit, done) => {
      const socket = ws;
      if (!socket || !inputOpen() || enterSocket != null) return "offline";
      // A real terminal paste: line ends become CR; paste markers inside the text are removed.
      const body = text.replace(/\x1b\[20[01]~/g, "").replace(/\r?\n/g, "\r");
      if (encoder.encode(body).length > MAX_SEND_BYTES) return "too-long";
      if (historyOpen || scrollSent || noteShown) leave();
      const bracketed = term.modes.bracketedPasteMode;
      const data = bracketed ? `\x1b[200~${body}\x1b[201~` : body;
      if (!submit || !bracketed) {
        socket.send(JSON.stringify({ type: "input", data: submit ? `${data}\r` : data }));
        done(submit);
        return "sent";
      }
      socket.send(JSON.stringify({ type: "input", data }));
      enterSocket = socket;
      enterDone = done;
      enterTimer = window.setTimeout(finishEnter, PASTE_ENTER_DELAY_MS);
      return "sent";
    };

    const measure = () => {
      // The Back to live pill sits above the key bar, which wraps to two rows on narrow phones.
      if (keyBarRef.current) wrapRef.current?.style.setProperty("--keybar-h", `${keyBarRef.current.offsetHeight}px`);
      const next = { top: host.offsetTop, left: host.offsetLeft, width: host.offsetWidth, height: host.offsetHeight };
      setBox((prev) =>
        prev &&
        prev.top === next.top &&
        prev.left === next.left &&
        prev.width === next.width &&
        prev.height === next.height
          ? prev
          : next,
      );
    };
    measure();

    const hideNote = () => {
      if (noteTimer != null) window.clearTimeout(noteTimer);
      noteTimer = null;
      if (noteShown) setLiveNote(null);
      noteShown = false;
    };
    const showNote = (text: string) => {
      hideNote();
      noteShown = true;
      setLiveNote(text);
      noteTimer = window.setTimeout(hideNote, NOTE_MS);
    };

    // End the history view and drop pending gestures. The new epoch makes any
    // history frame still on the way (older epoch) be ignored.
    const resetHistory = () => {
      epochRef.current += 1;
      historyOpen = false;
      scrollSent = false;
      anchorRef.current = null;
      scrollPending = 0;
      if (scrollTimer != null) window.clearTimeout(scrollTimer);
      scrollTimer = null;
      wheelRest = 0;
      touchY = null;
      touchRest = 0;
      swiping = false;
      setHistory(null);
    };
    // Back to live or a key: also hide the note and the tmux banner.
    const leave = () => {
      resetHistory();
      hideNote();
      setScrolled(false);
    };
    leaveRef.current = leave;

    const toItems = (frame: HistoryFrame): HistoryItem[] => {
      pageSeq += 1;
      return (frame.items ?? []).map((it, i) => {
        const role: HistoryRole = it.role === "user" || it.role === "tool" ? it.role : "assistant";
        const text = typeof it.text === "string" ? it.text : "";
        return { key: `${pageSeq}-${i}`, role, text, bytes: encoder.encode(text).length };
      });
    };

    const onHistoryFrame = (frame: HistoryFrame) => {
      if (frame.epoch !== epochRef.current) return; // a view that already ended
      if (frame.status === "loading") {
        historyOpen = true;
        scrollPending = 0; // the gesture now scrolls the history view, not tmux
        hideNote();
        setScrolled(false);
        setHistory({
          status: "loading",
          items: [],
          before: null,
          more: false,
          note: null,
          trimmed: false,
          cutBefore: null,
          olderLoading: false,
        });
      } else if (frame.status === "none") {
        historyOpen = false;
        scrollSent = false;
        epochRef.current += 1;
        setHistory(null);
        showNote(frame.note || "No older output is available.");
      } else if (frame.status === "page") {
        const before = typeof frame.before === "number" ? frame.before : null;
        const more = Boolean(frame.more) && before != null && before > 0;
        const note = frame.note ?? null;
        const items = toItems(frame);
        if (frame.first) {
          historyOpen = true;
          anchorRef.current = "bottom";
          // Over the cap: drop the oldest text, at the top; the view opens at the newest.
          let total = items.reduce((sum, it) => sum + it.bytes, 0);
          let start = 0;
          while (total > HISTORY_TEXT_CAP && start < items.length - 1) total -= items[start++].bytes;
          const kept = items.slice(start);
          const cutBefore = start > 0 ? kept[0].key : null;
          setHistory({ status: "ready", items: kept, before, more, note, trimmed: false, cutBefore, olderLoading: false });
          return;
        }
        if (!historyOpen) return;
        // Keep the oldest shown item at the same screen position after the prepend.
        const scroller = historyElRef.current;
        const oldest = scroller?.querySelector<HTMLElement>("[data-hkey]");
        if (scroller && oldest?.dataset.hkey) {
          anchorRef.current = { key: oldest.dataset.hkey, offset: oldest.offsetTop - scroller.scrollTop };
        } else {
          anchorRef.current = "bottom"; // nothing was shown yet (an empty part of the transcript)
        }
        setHistory((prev) => {
          if (!prev) return prev;
          const merged = [...items, ...prev.items];
          let total = merged.reduce((sum, it) => sum + it.bytes, 0);
          let trimmed = prev.trimmed;
          // Over the cap: drop the newest text, at the bottom, away from the viewport.
          while (total > HISTORY_TEXT_CAP && merged.length > 1) {
            total -= merged.pop()!.bytes;
            trimmed = true;
          }
          // The cut note stays above its item. When that item is dropped, the cut is
          // part of the newer text that was removed, and TRIMMED_NOTE tells it.
          const cutBefore = merged.some((it) => it.key === prev.cutBefore) ? prev.cutBefore : null;
          return { ...prev, status: "ready", items: merged, before, more, note, trimmed, cutBefore, olderLoading: false };
        });
      }
    };

    const sendResize = () => {
      const cols = Math.min(500, Math.max(20, term.cols));
      const rows = Math.min(200, Math.max(5, term.rows));
      lastCols = cols;
      lastRows = rows;
      sendJson({ type: "resize", cols, rows });
    };

    const sendPing = () => {
      if (!ws || ws.readyState !== WebSocket.OPEN) return;
      if (awaitingSince == null) awaitingSince = Date.now();
      sendJson({ type: "ping" });
    };

    // Drop a socket that stopped answering and open a new one at once.
    const replaceSocket = () => {
      const old = ws;
      ws = null;
      bridgeReady = false;
      if (probeTimer != null) window.clearTimeout(probeTimer);
      probeTimer = null;
      setProbe(null);
      dropEnter(); // the Enter belonged to the old socket
      awaitingSince = null;
      resetHistory();
      hideNote();
      if (old) {
        old.onopen = null;
        old.onmessage = null;
        old.onclose = null;
        old.onerror = null;
        try {
          old.close();
        } catch {
          /* ignore */
        }
      }
      if (closedByUs) return;
      if (reconnectTimer != null) {
        window.clearTimeout(reconnectTimer);
        reconnectTimer = null;
      }
      attempt = 0;
      setPhase("reconnecting");
      connect();
    };

    const connect = () => {
      try {
        fit.fit();
      } catch {
        /* ignore */
      }
      const proto = location.protocol === "https:" ? "wss" : "ws";
      const cols = Math.min(500, Math.max(20, term.cols));
      const rows = Math.min(200, Math.max(5, term.rows));
      lastCols = cols;
      lastRows = rows;
      const url = `${proto}://${location.host}/api/sessions/${encodeURIComponent(
        s.id,
      )}/terminal?cols=${cols}&rows=${rows}`;
      let socket: WebSocket;
      try {
        socket = new WebSocket(url);
      } catch {
        scheduleReconnect();
        return;
      }
      socket.binaryType = "arraybuffer";
      ws = socket;
      awaitingSince = null;

      // Transport open is not enough: input waits for the bridge's 'ready' frame.
      socket.onopen = () => {
        if (ws !== socket) return;
        attempt = 0;
        bridgeReady = false;
        setProbe(null);
        setExitMessage(null);
        setScrolled(false);
        resetHistory(); // a new bridge has no history view
        sendResize();
      };
      socket.onmessage = (ev) => {
        if (ws !== socket) return;
        awaitingSince = null; // any frame proves the socket is alive (heartbeat only)
        if (typeof ev.data === "string") {
          try {
            const frame = JSON.parse(ev.data) as {
              type: string;
              message?: string;
              active?: boolean;
              nonce?: string;
            };
            if (frame.type === "ready") {
              bridgeReady = true;
              syncInput();
              setPhase("open");
            } else if (frame.type === "pong") {
              // Only the answer to the current wake probe ends it; an older reply does not.
              if (probeNonce != null && frame.nonce === probeNonce) {
                if (probeTimer != null) window.clearTimeout(probeTimer);
                probeTimer = null;
                setProbe(null);
                if (enterHeld) finishEnter();
              }
            } else if (frame.type === "exit") {
              closedByUs = true;
              bridgeReady = false;
              syncInput();
              setPhase("exited");
              setExitMessage(frame.message ?? "The session ended.");
              socket.close();
            } else if (frame.type === "scroll") {
              setScrolled(Boolean(frame.active));
            } else if (frame.type === "history") {
              onHistoryFrame(frame as HistoryFrame);
            }
          } catch {
            /* ignore non-JSON text */
          }
          return;
        }
        term.write(new Uint8Array(ev.data as ArrayBuffer));
      };
      socket.onclose = (ev) => {
        if (ws !== socket) return;
        ws = null;
        bridgeReady = false;
        if (probeTimer != null) window.clearTimeout(probeTimer);
        probeTimer = null;
        setProbe(null);
        dropEnter();
        awaitingSince = null;
        resetHistory();
        hideNote();
        if (closedByUs) return;
        if (ev.code === 4404) {
          setPhase("exited");
          setExitMessage("This session is gone.");
          return;
        }
        if (ev.code === 4409) {
          setPhase("exited");
          setExitMessage("This session is no longer live.");
          return;
        }
        if (ev.code === 4401 || ev.code === 4403) {
          setPhase("exited");
          setExitMessage("The connection was refused. Sign in again.");
          return;
        }
        setPhase("reconnecting");
        scheduleReconnect();
      };
      socket.onerror = () => {
        socket.close();
      };
    };

    const scheduleReconnect = () => {
      if (closedByUs || reconnectTimer != null) return;
      const delay = RECONNECT_DELAYS[Math.min(attempt, RECONNECT_DELAYS.length - 1)];
      attempt += 1;
      reconnectTimer = window.setTimeout(() => {
        reconnectTimer = null;
        connect();
      }, delay);
    };

    // Heartbeat: a ping every 10 s; no answer within 8 s means the socket is dead.
    const heartbeat = window.setInterval(() => {
      if (!ws || ws.readyState !== WebSocket.OPEN) return;
      if (awaitingSince != null && Date.now() - awaitingSince > PONG_TIMEOUT_MS) {
        replaceSocket();
        return;
      }
      sendPing();
    }, HEARTBEAT_MS);

    // A phone freezes background tabs; the socket can look open but be dead.
    // On return, probe it and replace it when the probe gets no answer.
    const wake = () => {
      if (document.visibilityState !== "visible" || closedByUs) return;
      if (!ws) {
        if (reconnectTimer != null) {
          window.clearTimeout(reconnectTimer);
          reconnectTimer = null;
        }
        attempt = 0;
        connect();
        return;
      }
      if (ws.readyState !== WebSocket.OPEN) return; // still connecting: 'ready' decides
      // Until the bridge answers this probe, input is held back like a
      // disconnect: text sent into a dead socket would be lost, and nothing is
      // ever replayed. The text field stays editable and keeps its draft.
      probeSeq += 1;
      const nonce = `w${probeSeq}-${Math.random().toString(36).slice(2, 10)}`;
      ws.send(JSON.stringify({ type: "ping", nonce }));
      awaitingSince = Date.now(); // forget pings sent before the pause
      setProbe(nonce);
      if (probeTimer != null) window.clearTimeout(probeTimer);
      probeTimer = window.setTimeout(() => {
        probeTimer = null;
        if (probeNonce === nonce) replaceSocket();
      }, WAKE_PROBE_MS);
    };
    document.addEventListener("visibilitychange", wake);
    window.addEventListener("online", wake);

    // A key (keyboard or key bar) first ends any history view or pending scroll
    // request, so no late page can reopen it. The server leaves copy mode and
    // drops its history view before it writes the key.
    const dataSub = term.onData((data) => {
      if (historyOpen || scrollSent || noteShown) leave();
      sendJson({ type: "input", data });
    });

    // -- history scroll ------------------------------------------------------
    const flushScroll = () => {
      scrollTimer = null;
      if (scrollPending !== 0 && !historyOpen) {
        sendJson({ type: "scroll", lines: scrollPending, epoch: epochRef.current });
        scrollSent = true;
      }
      scrollPending = 0;
    };
    const queueScroll = (lines: number) => {
      // In a history view the gesture scrolls the view itself. While the note
      // says nothing older exists, a swipe up does not ask again.
      if (lines === 0 || historyOpen || (lines > 0 && noteShown)) return;
      scrollPending += lines;
      if (scrollTimer == null) scrollTimer = window.setTimeout(flushScroll, 60);
    };
    // The explicit History key-bar control: the same {type:'scroll', lines,
    // epoch} request a swipe or wheel sends, never arrow-key input to the agent.
    const openHistory = () => {
      if (historyOpen) return;
      sendJson({ type: "scroll", lines: 1, epoch: epochRef.current });
      scrollSent = true;
    };
    openHistoryRef.current = openHistory;
    const cellHeight = () => Math.max(8, host.clientHeight / Math.max(1, term.rows));

    // Wheel: xterm turns the wheel into arrow keys in the alternate screen,
    // which would scroll the agent's prompt history. Send it to the server instead.
    term.attachCustomWheelEventHandler((ev: WheelEvent) => {
      const px = ev.deltaMode === 1 ? ev.deltaY * cellHeight() : ev.deltaMode === 2 ? ev.deltaY * host.clientHeight : ev.deltaY;
      wheelRest += -px / cellHeight(); // wheel up (negative) -> older output (positive lines)
      const lines = Math.trunc(wheelRest);
      if (lines !== 0) {
        wheelRest -= lines;
        queueScroll(lines);
      }
      return false;
    });

    // Touch: a vertical swipe scrolls history; a tap still focuses the terminal.
    const onTouchStart = (e: TouchEvent) => {
      if (e.touches.length !== 1) {
        touchY = null;
        return;
      }
      touchY = e.touches[0].clientY;
      touchRest = 0;
      swiping = false;
    };
    const onTouchMove = (e: TouchEvent) => {
      if (touchY == null || e.touches.length !== 1) return;
      const y = e.touches[0].clientY;
      touchRest += y - touchY;
      touchY = y;
      if (!swiping && Math.abs(touchRest) < SWIPE_SLOP_PX) return;
      swiping = true;
      e.preventDefault();
      e.stopPropagation();
      const ch = cellHeight();
      const lines = Math.trunc(touchRest / ch); // finger down -> older output
      if (lines !== 0) {
        touchRest -= lines * ch;
        queueScroll(lines);
      }
    };
    const onTouchEnd = () => {
      touchY = null;
      swiping = false;
    };
    host.addEventListener("touchstart", onTouchStart, { capture: true, passive: true });
    host.addEventListener("touchmove", onTouchMove, { capture: true, passive: false });
    host.addEventListener("touchend", onTouchEnd, { capture: true });
    host.addEventListener("touchcancel", onTouchEnd, { capture: true });

    const observer = new ResizeObserver(() => {
      measure(); // the history view covers exactly the terminal area, never the key bar
      if (resizeTimer != null) window.clearTimeout(resizeTimer);
      resizeTimer = window.setTimeout(() => {
        resizeTimer = null;
        try {
          fit.fit();
        } catch {
          return;
        }
        if (term.cols !== lastCols || term.rows !== lastRows) sendResize();
      }, RESIZE_DEBOUNCE_MS);
    });
    observer.observe(host);

    connect();

    return () => {
      closedByUs = true;
      sendRef.current = () => {};
      leaveRef.current = () => {};
      openHistoryRef.current = () => {};
      epochRef.current += 1; // a session change drops every pending history frame
      if (noteTimer != null) window.clearTimeout(noteTimer);
      dataSub.dispose();
      observer.disconnect();
      window.clearInterval(heartbeat);
      document.removeEventListener("visibilitychange", wake);
      window.removeEventListener("online", wake);
      host.removeEventListener("touchstart", onTouchStart, { capture: true });
      host.removeEventListener("touchmove", onTouchMove, { capture: true });
      host.removeEventListener("touchend", onTouchEnd, { capture: true });
      host.removeEventListener("touchcancel", onTouchEnd, { capture: true });
      if (reconnectTimer != null) window.clearTimeout(reconnectTimer);
      if (resizeTimer != null) window.clearTimeout(resizeTimer);
      if (probeTimer != null) window.clearTimeout(probeTimer);
      if (scrollTimer != null) window.clearTimeout(scrollTimer);
      sendTextRef.current = () => "offline";
      const socket = ws;
      const unproven = probeNonce != null;
      ws = null; // the handlers of this socket now ignore it
      probeNonce = null;
      if (enterTimer != null && socket && enterSocket === socket && !unproven) {
        // A Send that waits for its Enter (a switch right after Send): finish it
        // on the same socket, then close.
        window.clearTimeout(enterTimer);
        window.setTimeout(() => {
          finishEnter();
          socket.close();
        }, PASTE_ENTER_DELAY_MS);
      } else {
        // No pending Enter, or its socket is unproven after a wake: no Enter, close.
        dropEnter();
        socket?.close();
      }
      term.dispose();
      termRef.current = null;
    };
  }, [s.id]);

  // Route a key-bar key straight to the terminal input handler. No focus call:
  // a key tap must not open the on-screen keyboard.
  const sendKey = (data: string) => {
    if (phase !== "open" || verifying) return;
    termRef.current?.input(data, true);
  };

  const editDraft = (text: string) => {
    setDraftState(text);
    setDraft(s.id, text);
    setComposeNote(null);
  };

  const submitDraft = (submit: boolean) => {
    if (sending || !draft || phase !== "open" || verifying) return;
    setSending(true); // the done callback below can run at once and clear it
    setComposeNote(null);
    const result = sendTextRef.current(draft, submit, (enterSent) => {
      setSending(false);
      if (submit && !enterSent) {
        setComposeNote("The text was sent, but the connection dropped before Enter. Press Enter when it is back.");
      }
    });
    if (result !== "sent") {
      setSending(false);
      setComposeNote(
        result === "too-long"
          ? "This text is too long to send at once (60 KB maximum)."
          : "Not connected. Your text stays here.",
      );
      return;
    }
    // Sent once: the draft is cleared so a second tap cannot send it again.
    setDraftState("");
    setDraft(s.id, "");
  };

  const onComposeKey = (e: ReactKeyboardEvent<HTMLTextAreaElement>) => {
    // Enter alone adds a line (the phone keyboard's Enter). Ctrl/Cmd+Enter sends.
    // Never during composition: the IME still owns those keys.
    if (e.nativeEvent.isComposing || e.keyCode === 229) return;
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      submitDraft(true);
    }
  };

  const backToLive = () => {
    leaveRef.current();
    sendRef.current({ type: "scroll", to: "bottom", epoch: epochRef.current });
  };

  const loadOlder = () => {
    const h = history;
    if (!h || h.status !== "ready" || !h.more || h.olderLoading || h.before == null) return;
    setHistory({ ...h, olderLoading: true });
    sendRef.current({ type: "history", epoch: epochRef.current, before: h.before });
  };

  const onHistoryKey = (e: ReactKeyboardEvent<HTMLDivElement>) => {
    if (e.target !== e.currentTarget) return; // keys on the Older button stay with the button
    if (e.key === "Escape") {
      // Escape closes the view only: it must not interrupt the agent by accident.
      e.preventDefault();
      backToLive();
      termRef.current?.focus();
      return;
    }
    const data = typedInput(e);
    if (data == null) return;
    e.preventDefault();
    termRef.current?.focus();
    termRef.current?.input(data, true); // onData ends the view before it sends the key
  };

  // After a page renders: the first page opens at its newest text; after Older,
  // the item that was oldest keeps its screen position.
  useLayoutEffect(() => {
    const el = historyElRef.current;
    const anchor = anchorRef.current;
    if (!el || !anchor || !history || history.status !== "ready") return;
    anchorRef.current = null;
    if (anchor === "bottom") {
      el.scrollTop = el.scrollHeight;
      return;
    }
    const node = el.querySelector<HTMLElement>(`[data-hkey="${anchor.key}"]`);
    if (node) el.scrollTop = node.offsetTop - anchor.offset;
  }, [history]);

  const viewing = (scrolled || history != null) && phase === "open";
  const ready = phase === "open" && !verifying;
  const offline = showOverlay && (phase === "connecting" || phase === "reconnecting");

  return (
    <div className={isPhone ? "terminal-wrap term-phone" : "terminal-wrap"} ref={wrapRef} style={wrapStyle}>
      <div className="terminal-host" ref={hostRef} />
      {history && phase === "open" && box ? (
        <div
          className="term-history"
          ref={historyElRef}
          style={{ top: box.top, left: box.left, width: box.width, height: box.height }}
          role="log"
          aria-label="Saved transcript"
          aria-busy={history.status === "loading" || history.olderLoading}
          tabIndex={0}
          onKeyDown={onHistoryKey}
        >
          <p className="term-history-label">Saved transcript. This is not the live screen.</p>
          {history.more ? (
            <button
              type="button"
              className="term-history-older"
              onClick={loadOlder}
              disabled={history.olderLoading || history.status !== "ready"}
            >
              {history.olderLoading ? "Loading older messages…" : "Older"}
            </button>
          ) : null}
          {history.note ? <p className="term-history-note">{history.note}</p> : null}
          {history.status === "loading" ? <p className="term-history-note">Loading earlier messages…</p> : null}
          {history.items.map((it) => (
            <Fragment key={it.key}>
              {it.key === history.cutBefore ? <p className="term-history-note">{CUT_NOTE}</p> : null}
              <div data-hkey={it.key} className={`term-history-item ${it.role}`}>
                <span className="term-history-role">{ROLE_LABEL[it.role]}</span>
                {it.text}
              </div>
            </Fragment>
          ))}
          {history.trimmed ? <p className="term-history-note">{TRIMMED_NOTE}</p> : null}
        </div>
      ) : null}
      {liveNote && phase === "open" && box ? (
        <div
          className="term-history-live-note"
          role="status"
          style={{ left: box.left + 8, width: Math.max(0, box.width - 16), top: box.top + box.height - 8 }}
        >
          {liveNote}
        </div>
      ) : null}
      {viewing ? (
        <button type="button" className="term-live-pill" onClick={backToLive} onPointerDown={keepFocus} onMouseDown={keepFocus}>
          {history != null ? "Saved transcript" : "Terminal scrollback"} · Back to live
          <BackIcon style={{ transform: "rotate(-90deg)" }} />
        </button>
      ) : null}
      {showOverlay && (phase === "reconnecting" || phase === "connecting") ? (
        <div className="term-overlay" role="status">
          {phase === "connecting" ? "Connecting…" : "Reconnecting…"}
        </div>
      ) : null}
      {phase === "exited" ? (
        <div className="term-overlay" role="status">
          {exitMessage ?? "The session ended."}
        </div>
      ) : null}
      {isTouch ? (
        <div className="term-input" ref={keyBarRef}>
          <div className="key-bar" role="group" aria-label="Terminal keys">
            <KeyButton disabled={!ready} onClick={() => openHistoryRef.current()}>
              History
            </KeyButton>
            <KeyButton disabled={!ready} onClick={() => sendKey("\x1b")}>
              Esc
            </KeyButton>
            <KeyButton disabled={!ready} onClick={() => sendKey("\t")}>
              Tab
            </KeyButton>
            <KeyButton disabled={!ready} onClick={() => sendKey("\x03")}>
              Ctrl+C
            </KeyButton>
            <KeyButton disabled={!ready} label="Arrow up" onClick={() => sendKey("\x1b[A")}>
              <BackIcon style={{ transform: "rotate(90deg)" }} />
            </KeyButton>
            <KeyButton disabled={!ready} label="Arrow down" onClick={() => sendKey("\x1b[B")}>
              <BackIcon style={{ transform: "rotate(-90deg)" }} />
            </KeyButton>
            <KeyButton disabled={!ready} onClick={() => sendKey("\r")}>
              Enter
            </KeyButton>
          </div>
          {phase !== "exited" ? (
            <div className="term-compose">
              <textarea
                className="term-compose-field"
                aria-label={`Text for ${s.name}`}
                placeholder={`Type for ${s.name}…`}
                rows={2}
                value={draft}
                autoCapitalize="off"
                autoCorrect="off"
                spellCheck={false}
                enterKeyHint="enter"
                onChange={(e) => editDraft(e.target.value)}
                onKeyDown={onComposeKey}
              />
              <button
                type="button"
                className="term-compose-insert"
                disabled={!ready || sending || !draft}
                onPointerDown={keepFocus}
                onMouseDown={keepFocus}
                onClick={() => submitDraft(false)}
                title="Paste the text without Enter"
              >
                Insert
              </button>
              <button
                type="button"
                className="send term-compose-send"
                disabled={!ready || sending || !draft}
                onPointerDown={keepFocus}
                onMouseDown={keepFocus}
                onClick={() => submitDraft(true)}
                title="Paste the text, then press Enter"
              >
                Send
              </button>
              {composeNote || offline || verifySlow ? (
                <p className="term-compose-note" role="status">
                  {composeNote ??
                    (verifySlow ? "Checking the connection. Your text stays here." : "Not connected. Your text stays here.")}
                </p>
              ) : null}
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function KeyButton({
  children,
  disabled,
  label,
  onClick,
}: {
  children: React.ReactNode;
  disabled: boolean;
  label?: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      aria-label={label}
      disabled={disabled}
      onPointerDown={keepFocus}
      onMouseDown={keepFocus}
      onClick={onClick}
    >
      {children}
    </button>
  );
}
