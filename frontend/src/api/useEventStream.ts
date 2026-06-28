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

/** Open one SSE stream for this tab; refetch the library views when the server
 * says the library changed, and re-request images on `art:changed`. Re-connects
 * (after a drop/sleep) also catch up on anything missed while disconnected.
 * Silent (no toast). Mounted once at the App shell. */
export function useEventStream(): void {
  const qc = useQueryClient();
  useEffect(() => {
    let connected = false;
    const es = new EventSource("/api/events");
    es.onopen = () => {
      // First connect has nothing to catch up on; only RE-connections (after a
      // drop/sleep) invalidate + refresh images for anything missed.
      if (connected) {
        invalidateLibraryContent(qc);
        bumpAssetVersion();
      }
      connected = true;
    };
    es.onmessage = (e) => {
      invalidateLibraryContent(qc);
      let type: string | undefined;
      try {
        type = (JSON.parse(e.data) as { type?: string }).type;
      } catch {
        type = undefined;
      }
      if (type === "art:changed") bumpAssetVersion();
    };
    return () => es.close();
  }, [qc]);
}
