import { useQuery } from "@tanstack/react-query";

import { client } from "@/api/client";
import type { components } from "@/api/schema";

/** Global search results across artists/albums/tracks (generated contract). */
export type SearchResults = components["schemas"]["SearchResults"];
/** A single track hit; carries `album_id` (the playlist seam). */
export type SearchTrack = components["schemas"]["SearchTrack"];
/** One entity of the search, paged — `GET /api/search?type=…` ("View all"). */
export type TypedSearchPage = components["schemas"]["TypedSearchPage"];

/** The three single-type views `/api/search?type=` can page through. */
export type SearchType = "artists" | "albums" | "tracks";

/** Parse a `?type=` URL param; anything unknown means "sectioned view". */
export function parseSearchType(value: string | null): SearchType | null {
  return value === "artists" || value === "albums" || value === "tracks"
    ? value
    : null;
}

async function fetchSearch(q: string): Promise<SearchResults> {
  const { data, error } = await client.GET("/api/search", {
    params: { query: { q } },
  });
  // `"type" in data` discriminates the typed (paged) shape, which a request
  // WITHOUT the `type` param never returns — this narrows the generated
  // union for tsc and is unreachable at runtime.
  if (error || !data || "type" in data) {
    throw new Error("Search failed");
  }
  return data;
}

async function fetchTypedSearch(query: {
  q: string;
  type: SearchType;
  limit: number;
  offset: number;
}): Promise<TypedSearchPage> {
  const { data, error } = await client.GET("/api/search", {
    params: { query },
  });
  // The mirror narrowing: a request WITH `type` always returns the typed
  // (paged) shape, so a missing `type` key is unreachable at runtime.
  if (error || !data || !("type" in data)) {
    throw new Error("Search failed");
  }
  return data;
}

/**
 * Search the library for `q` (`GET /api/search`, sectioned default). Disabled
 * while the trimmed query is empty (no request, no error — the page shows its
 * idle prompt). `placeholderData` keeps the previous results visible between
 * keystrokes so the list doesn't flash empty while the next query is in flight.
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

/**
 * Paged single-type search (`GET /api/search?type=…&limit=…&offset=…`): the
 * response is a `TypedSearchPage` with ONLY the requested section populated
 * as the `[offset, offset+limit)` page and `total` carrying the full match
 * count (the other lists stay empty).
 */
export function useTypedSearch(
  q: string,
  type: SearchType,
  limit: number,
  offset: number,
) {
  const trimmed = q.trim();
  return useQuery({
    queryKey: ["search", trimmed, type, limit, offset],
    queryFn: () => fetchTypedSearch({ q: trimmed, type, limit, offset }),
    enabled: trimmed.length > 0,
    placeholderData: (prev) => prev,
  });
}
