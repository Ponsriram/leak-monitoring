import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

/**
 * The live connection, replacing the dashboard's 60-second poll.
 *
 * `EventSource` is the browser's built-in SSE client and it reconnects on its own with a
 * backoff — behaviour we would otherwise have to write, and get wrong, on top of a
 * WebSocket. It sends cookies same-origin, so the session travels with it exactly like every
 * other request.
 *
 * What arrives is treated as a *signal*, not as data. A `leak_inserted` event invalidates the
 * queries that would show it and lets TanStack Query refetch through the normal, typed,
 * authenticated path. Patching the notification payload straight into the cache would be
 * faster by one round trip and wrong in two ways: the payload is deliberately truncated to
 * stay under the `pg_notify` size limit, and a client that missed an event while
 * disconnected would hold a cache that silently disagrees with the database.
 */

type LiveState = {
  /** Whether the stream is currently open. Drives the "Live" indicator. */
  connected: boolean;
  /** Leaks seen since this page loaded. Lets the UI offer "N new — refresh". */
  newLeaks: number;
};

const RECONNECT_NOTICE_MS = 3_000;

export function useLiveUpdates(): LiveState {
  const queryClient = useQueryClient();
  const [connected, setConnected] = useState(false);
  const [newLeaks, setNewLeaks] = useState(0);

  // Held in a ref so the effect below never re-runs when the count changes — reconnecting
  // the stream on every arriving leak would be the opposite of what this is for.
  const countRef = useRef(0);

  useEffect(() => {
    const source = new EventSource("/api/stream", { withCredentials: true });
    let reconnectTimer: number | undefined;

    source.addEventListener("connected", () => {
      window.clearTimeout(reconnectTimer);
      setConnected(true);
    });

    source.addEventListener("leak_inserted", () => {
      countRef.current += 1;
      setNewLeaks(countRef.current);
      // Invalidate rather than patch: refetch through the typed API path.
      void queryClient.invalidateQueries({ queryKey: ["leaks"] });
      void queryClient.invalidateQueries({ queryKey: ["stats"] });
    });

    source.addEventListener("hunt_changed", (event) => {
      // The one event carrying an id we can act on precisely — refetching only the hunt
      // that moved, instead of every hunt this session has ever opened.
      try {
        const { jobId } = JSON.parse((event as MessageEvent).data) as { jobId: number };
        void queryClient.invalidateQueries({ queryKey: ["hunt", jobId] });
      } catch {
        void queryClient.invalidateQueries({ queryKey: ["hunt"] });
      }
    });

    source.onerror = () => {
      /**
       * EventSource reconnects by itself, and it fires `error` on every attempt. Flipping
       * the indicator to "offline" immediately would make it flicker on a routine
       * reconnect, so the badge only drops after the connection has actually stayed down.
       */
      window.clearTimeout(reconnectTimer);
      reconnectTimer = window.setTimeout(() => setConnected(false), RECONNECT_NOTICE_MS);
    };

    return () => {
      window.clearTimeout(reconnectTimer);
      source.close();
    };
  }, [queryClient]);

  return { connected, newLeaks };
}
