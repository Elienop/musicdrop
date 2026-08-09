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
    return () => {
      es.close();
      if (timer !== null) clearTimeout(timer);
    };
  }, [qc]);
}
