// Live application state: fetches /api/state, subscribes to WS /api/stream,
// throttles refetch on 'changed', polls every 15 s while the stream is down.
// Heartbeat: {type:'ping'} every 10 s; no answer within 8 s, or no answer to
// the probe sent when the page wakes (4 s), replaces the socket at once.
// The badge says "Reconnecting" only when the gap lasts longer than 1.5 s.

import { useCallback, useEffect, useRef, useState } from "react";
import { getState } from "./api";
import type { StateView, StreamFrame, StreamTopic } from "./types";

export type ConnStatus = "live" | "reconnecting";

interface UseAppState {
  state: StateView | null;
  status: ConnStatus;
  refresh: () => void;
  // Registers a callback fired when the given topics change (throttled by the caller).
  onTopics: (cb: (topics: StreamTopic[]) => void) => () => void;
}

const RECONNECT_DELAYS = [1000, 2000, 5000];
const HEARTBEAT_MS = 10000;
const PONG_TIMEOUT_MS = 8000;
const WAKE_PROBE_MS = 4000;
const STATUS_GRACE_MS = 1500;

export function useAppState(enabled: boolean): UseAppState {
  const [state, setState] = useState<StateView | null>(null);
  const [status, setStatus] = useState<ConnStatus>("reconnecting");

  const topicListeners = useRef(new Set<(topics: StreamTopic[]) => void>());
  const refetchTimer = useRef<number | null>(null);
  const pollTimer = useRef<number | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectAttempt = useRef(0);
  const reconnectTimer = useRef<number | null>(null);
  const statusTimer = useRef<number | null>(null);
  const awaitingSince = useRef<number | null>(null);
  const closed = useRef(false);

  const doFetch = useCallback(async () => {
    try {
      const next = await getState();
      setState(next);
    } catch {
      // A 401 is handled by the global handler; other errors keep the old state.
    }
  }, []);

  // Throttle refetch to at most one call per 300 ms.
  const scheduleFetch = useCallback(() => {
    if (refetchTimer.current != null) return;
    refetchTimer.current = window.setTimeout(() => {
      refetchTimer.current = null;
      void doFetch();
    }, 300);
  }, [doFetch]);

  const startPolling = useCallback(() => {
    if (pollTimer.current != null) return;
    pollTimer.current = window.setInterval(() => {
      void doFetch();
    }, 15000);
  }, [doFetch]);

  const stopPolling = useCallback(() => {
    if (pollTimer.current != null) {
      window.clearInterval(pollTimer.current);
      pollTimer.current = null;
    }
  }, []);

  const markLive = useCallback(() => {
    if (statusTimer.current != null) {
      window.clearTimeout(statusTimer.current);
      statusTimer.current = null;
    }
    setStatus("live");
  }, []);

  // A short gap (a wake, a quick reconnect) keeps the badge at "Live".
  const markDown = useCallback(() => {
    if (statusTimer.current != null) return;
    statusTimer.current = window.setTimeout(() => {
      statusTimer.current = null;
      if (!closed.current) setStatus("reconnecting");
    }, STATUS_GRACE_MS);
  }, []);

  const connectRef = useRef<() => void>(() => {});

  const scheduleReconnect = useCallback(() => {
    if (closed.current || reconnectTimer.current != null) return;
    const delay = RECONNECT_DELAYS[Math.min(reconnectAttempt.current, RECONNECT_DELAYS.length - 1)];
    reconnectAttempt.current += 1;
    reconnectTimer.current = window.setTimeout(() => {
      reconnectTimer.current = null;
      connectRef.current();
    }, delay);
  }, []);

  const connect = useCallback(() => {
    if (closed.current) return;
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const url = `${proto}://${location.host}/api/stream`;
    let ws: WebSocket;
    try {
      ws = new WebSocket(url);
    } catch {
      scheduleReconnect();
      return;
    }
    wsRef.current = ws;
    awaitingSince.current = null;

    ws.onopen = () => {
      if (wsRef.current !== ws) return;
      reconnectAttempt.current = 0;
      markLive();
      stopPolling();
      void doFetch();
    };
    ws.onmessage = (ev) => {
      if (wsRef.current !== ws) return;
      awaitingSince.current = null; // any frame proves the socket is alive
      let frame: StreamFrame;
      try {
        frame = JSON.parse(ev.data as string) as StreamFrame;
      } catch {
        return;
      }
      if (frame.type === "changed") {
        scheduleFetch();
        for (const cb of topicListeners.current) cb(frame.topics);
      }
    };
    ws.onclose = () => {
      if (wsRef.current !== ws) return;
      wsRef.current = null;
      awaitingSince.current = null;
      if (closed.current) return;
      markDown();
      startPolling();
      scheduleReconnect();
    };
    ws.onerror = () => {
      ws.close();
    };
  }, [doFetch, markDown, markLive, scheduleFetch, scheduleReconnect, startPolling, stopPolling]);
  connectRef.current = connect;

  // Drop a socket that stopped answering and open a new one at once.
  const replaceSocket = useCallback(() => {
    const old = wsRef.current;
    wsRef.current = null;
    awaitingSince.current = null;
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
    if (closed.current) return;
    if (reconnectTimer.current != null) {
      window.clearTimeout(reconnectTimer.current);
      reconnectTimer.current = null;
    }
    reconnectAttempt.current = 0;
    markDown();
    connect();
  }, [connect, markDown]);

  const sendPing = useCallback(() => {
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    if (awaitingSince.current == null) awaitingSince.current = Date.now();
    ws.send(JSON.stringify({ type: "ping" }));
  }, []);

  useEffect(() => {
    if (!enabled) return;
    closed.current = false;
    void doFetch();
    connect();

    const heartbeat = window.setInterval(() => {
      const ws = wsRef.current;
      if (!ws || ws.readyState !== WebSocket.OPEN) return;
      const since = awaitingSince.current;
      if (since != null && Date.now() - since > PONG_TIMEOUT_MS) {
        replaceSocket();
        return;
      }
      sendPing();
    }, HEARTBEAT_MS);

    // A phone suspends background tabs and can leave a socket that looks open
    // but is dead. On return, refetch, then probe the socket: no answer within
    // 4 s replaces it. A closed socket reconnects at once.
    let probeTimer: number | null = null;
    const wake = () => {
      if (document.visibilityState !== "visible" || closed.current) return;
      void doFetch();
      if (wsRef.current == null) {
        if (reconnectTimer.current != null) {
          window.clearTimeout(reconnectTimer.current);
          reconnectTimer.current = null;
        }
        reconnectAttempt.current = 0;
        connect();
        return;
      }
      awaitingSince.current = null; // forget pings sent before the pause
      sendPing();
      if (probeTimer != null) window.clearTimeout(probeTimer);
      probeTimer = window.setTimeout(() => {
        probeTimer = null;
        if (awaitingSince.current != null) replaceSocket();
      }, WAKE_PROBE_MS);
    };
    document.addEventListener("visibilitychange", wake);
    window.addEventListener("online", wake);
    return () => {
      closed.current = true;
      window.clearInterval(heartbeat);
      if (probeTimer != null) window.clearTimeout(probeTimer);
      document.removeEventListener("visibilitychange", wake);
      window.removeEventListener("online", wake);
      if (refetchTimer.current != null) window.clearTimeout(refetchTimer.current);
      if (reconnectTimer.current != null) window.clearTimeout(reconnectTimer.current);
      if (statusTimer.current != null) window.clearTimeout(statusTimer.current);
      refetchTimer.current = null;
      reconnectTimer.current = null;
      statusTimer.current = null;
      stopPolling();
      wsRef.current?.close();
      wsRef.current = null;
    };
  }, [enabled, connect, doFetch, replaceSocket, sendPing, stopPolling]);

  const onTopics = useCallback((cb: (topics: StreamTopic[]) => void) => {
    topicListeners.current.add(cb);
    return () => {
      topicListeners.current.delete(cb);
    };
  }, []);

  return { state, status, refresh: doFetch, onTopics };
}
