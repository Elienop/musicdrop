import { useQuery } from "@tanstack/react-query";

import { client } from "@/api/client";
import type { components } from "@/api/schema";

/** Response of `GET /api/stats` (generated contract). */
export type LibraryStatsResponse = components["schemas"]["LibraryStatsResponse"];

async function fetchStats(): Promise<LibraryStatsResponse> {
  const { data, error } = await client.GET("/api/stats");
  if (error || !data) {
    throw new Error("Failed to load library stats");
  }
  return data;
}

/** Fetch the library dashboard stats (`GET /api/stats`). */
export function useStats() {
  return useQuery({
    queryKey: ["stats"],
    queryFn: fetchStats,
  });
}
