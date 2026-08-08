import { useQuery } from "@tanstack/react-query";

import { client } from "@/api/client";
import { unwrap } from "@/api/lib";
import type { components } from "@/api/schema";

/** Response of `GET /api/stats` (generated contract). */
export type LibraryStatsResponse = components["schemas"]["LibraryStatsResponse"];

async function fetchStats(): Promise<LibraryStatsResponse> {
  return unwrap(await client.GET("/api/stats"), "Failed to load library stats");
}

/**
 * Fetch the library dashboard stats (`GET /api/stats`).
 *
 * These families are invalidated by SSE the moment the library actually
 * changes (and on SSE reconnect), so the clock is not their freshness
 * signal; five minutes only bounds staleness if the event stream is silently
 * broken (e.g. a proxy buffering SSE).
 */
export function useStats() {
  return useQuery({
    queryKey: ["stats"],
    queryFn: fetchStats,
    staleTime: 5 * 60_000,
  });
}
