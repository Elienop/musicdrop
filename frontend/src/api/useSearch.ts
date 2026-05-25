import { useQuery } from "@tanstack/react-query";

import { client } from "@/api/client";
import type { components } from "@/api/schema";

/** Global search results across artists/albums/tracks (generated contract). */
export type SearchResults = components["schemas"]["SearchResults"];
/** A single track hit; carries `album_id` (the playlist seam). */
export type SearchTrack = components["schemas"]["SearchTrack"];

async function fetchSearch(q: string): Promise<SearchResults> {
  const { data, error } = await client.GET("/api/search", {
    params: { query: { q } },
  });
  if (error || !data) {
    throw new Error("Search failed");
  }
  return data;
}

/**
 * Search the library for `q` (`GET /api/search`). Disabled while the trimmed
 * query is empty (no request, no error — the page shows its idle prompt).
 * `placeholderData` keeps the previous results visible between keystrokes so the
 * list doesn't flash empty while the next query is in flight.
 */
export function useSearch(q: string) {
  const trimmed = q.trim();
  return useQuery({
    queryKey: ["search", trimmed],
    queryFn: () => fetchSearch(trimmed),
    enabled: trimmed.length > 0,
    placeholderData: (prev) => prev,
  });
}
