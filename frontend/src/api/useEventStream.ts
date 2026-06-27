import { useEffect } from "react";
import { type QueryClient, useQueryClient } from "@tanstack/react-query";

// The library-content query family — the union of what the in-tab mutation
// hooks invalidate. Prefix keys (e.g. ["album"]) match their detail variants
// (["album", id], ["album", id, "missing"]).
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
] as const;

/** Invalidate every library-content query so mounted views background-refetch.
 * Shared by the SSE hook and the in-tab mutation hooks (one definition). */
export function invalidateLibraryContent(qc: QueryClient): void {
  for (const queryKey of LIBRARY_CONTENT_KEYS) {
    void qc.invalidateQueries({ queryKey });
  }
}

/** Open one SSE stream for this tab; refetch the library views when the server
 * says the library changed, and on every (re)connect to catch up on anything
 * missed while disconnected. Silent (no toast). Mounted once at the App shell. */
export function useEventStream(): void {
  const qc = useQueryClient();
  useEffect(() => {
    const es = new EventSource("/api/events");
    es.onopen = () => invalidateLibraryContent(qc);
    es.onmessage = () => invalidateLibraryContent(qc); // any event today = "refetch"
    return () => es.close();
  }, [qc]);
}
