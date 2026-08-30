import { useEffect } from "react";
import { type QueryClient, useQueryClient } from "@tanstack/react-query";

import { bumpAssetVersion } from "@/api/assetVersion";

// The library-content query family — the union of what the in-tab mutation
// hooks invalidate. Prefix keys (e.g. ["album"]) match their detail variants
// (["album", id], ["album", id, "missing"]; ["playlist"] covers
// ["playlist", id]).
const LIBRARY_CONTENT_KEYS = [
  ["albums"],
  ["artists"],
  ["browse"],
  ["search"],
  ["stats"],
  ["album"],
  ["duplicates"],
  ["trash"],
  ["lyrics"],
  ["playlists"],
  ["playlist"],
] as const;

/** Invalidate every library-content query so mounted views background-refetch.
 * Shared by the SSE hook and the in-tab mutation hooks (one definition). */
export function invalidateLibraryContent(qc: QueryClient): void {
  for (const queryKey of LIBRARY_CONTENT_KEYS) {
    void qc.invalidateQueries({ queryKey });
  }
}

/** Size of one invalidation round, for tests that assert "exactly one round"
 * without hard-coding the family list. */
export const LIBRARY_CONTENT_KEY_COUNT = LIBRARY_CONTENT_KEYS.length;

/** Open one SSE stream for this tab; refetch the library views when the server
 * says the library changed, and re-request images on `art:changed`. Re-connects
 * (after a drop/sleep) also catch up on anything missed while disconnected.
 * Silent (no toast). Mounted once at the App shell. */
// The backend emits one `library:changed` SSE message per FINISHED import
// job, so an inbox drain of N albums used to fire N full invalidation rounds
// back-to-back — each one refetching all 11 query families against a server
// cache the previous round had only just invalidated. Coalesce a burst into
// one round: wait for a quiet gap (FLUSH_AFTER_MS) after the last message,
// but never let a continuous stream starve real-world listeners past
// MAX_WAIT_MS.
const FLUSH_AFTER_MS = 300;
const MAX_WAIT_MS = 2000;

export function useEventStream(): void {
  const qc = useQueryClient();
  useEffect(() => {
    let connected = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let firstQueuedAt = 0;
    const flush = () => {
      timer = null;
      invalidateLibraryContent(qc);
    };
    const scheduleInvalidate = () => {
      const now = Date.now();
      if (timer === null) {
        firstQueuedAt = now;
      } else {
        clearTimeout(timer);
        if (now - firstQueuedAt >= MAX_WAIT_MS - FLUSH_AFTER_MS) {
          flush();
          return;
        }
      }
      timer = setTimeout(flush, FLUSH_AFTER_MS);
    };
    const es = new EventSource("/api/events");
    es.onopen = () => {
      // First connect has nothing to catch up on; only RE-connections (after a
      // drop/sleep) invalidate + refresh images for anything missed. A
      // reconnect is not a burst, so this stays immediate (not coalesced).
      if (connected) {
        // A message just before the drop may have armed the trailing
        // debounce; native EventSource reuses this same closure across a
        // reconnect, so that timer would otherwise survive and fire a
        // redundant second round after this catch-up already covered it.
        if (timer !== null) {
          clearTimeout(timer);
          timer = null;
        }
        invalidateLibraryContent(qc);
        // We can't know WHICH assets changed while disconnected → global bump.
        bumpAssetVersion();
      }
      connected = true;
    };
    es.onmessage = (e) => {
      scheduleInvalidate();
      let type: string | undefined;
      // `scope` names the one asset whose bytes changed ("album:7",
      // "artist:ABBA"); absent/null means library-wide.
      let scope: string | undefined;
      try {
        const payload = JSON.parse(e.data) as { type?: string; scope?: string | null };
        type = payload.type;
        scope = payload.scope ?? undefined;
      } catch {
        type = undefined;
        scope = undefined;
      }
      // The asset-version bump stays immediate — it's cheap (an in-memory
      // counter) and images should refresh without waiting on the debounce.
      if (type === "art:changed") bumpAssetVersion(scope);
    };
    es.onerror = () => {
      // A transient drop leaves readyState CONNECTING: the browser is already
      // retrying and onopen's `connected` branch catches up on the gap, so
      // there is nothing to do. CLOSED means it has given up permanently —
      // which is what an HTTP error on the INITIAL response produces, and the
      // session gate's 401 (an expired cookie) is exactly that. No message can
      // arrive after this, so the trailing debounce is waiting for a quiet gap
      // that has already begun: flush now rather than sit on invalidations for
      // messages that did arrive.
      // `EventSource.CLOSED` off the platform class rather than a literal 2 —
      // the test stubs carry the same static, so the constant's authority
      // stays with the DOM instead of with a test double.
      if (es.readyState !== EventSource.CLOSED || timer === null) return;
      clearTimeout(timer);
      flush();
      // This handler does NOT sign anybody out, and that is deliberate:
      // EventSource exposes no status code, so a 401, a 500 and a proxy
      // dropping an idle connection all arrive here identically — flipping the
      // auth store would sign a user out over an outage. What actually detects
      // an expired cookie on an otherwise idle shell is the 30s
      // `useActiveImport` / `useAcquisitionStatus` polls: their 401s go through
      // the client middleware, which flips the store and bounces once. Those
      // two intervals are load-bearing for expiry detection, not just for
      // freshness — dropping them would leave a dead session rendering a live
      // shell until the user clicked something.
      //
      // Nothing here reopens the stream, and nothing needs to. /login is a
      // top-level sibling of the App layout route (main.tsx), so bouncing
      // there unmounts App, which runs this effect's cleanup; returning to the
      // shell after signing in mounts a fresh hook and a fresh EventSource.
      // That new stream replays nothing (onopen only catches up when
      // `connected` is already true, and a new closure starts false) — the
      // sign-in path invalidates the library families itself for that reason
      // (see useLogin in api/auth.ts).
    };
    return () => {
      es.close();
      if (timer !== null) clearTimeout(timer);
    };
  }, [qc]);
}
