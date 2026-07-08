import { useQuery } from "@tanstack/react-query";

import { client } from "@/api/client";
import { unwrap } from "@/api/lib";
import type { components } from "@/api/schema";

/** Response of `GET /api/stats` (generated contract). */
export type LibraryStatsResponse = components["schemas"]["LibraryStatsResponse"];

async function fetchStats(): Promise<LibraryStatsResponse> {
  return unwrap(await client.GET("/api/stats"), "Failed to load library stats");
}

/** Fetch the library dashboard stats (`GET /api/stats`). */
export function useStats() {
  return useQuery({
    queryKey: ["stats"],
    queryFn: fetchStats,
  });
}
