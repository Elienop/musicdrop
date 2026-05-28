import { useQuery } from "@tanstack/react-query";

import { client } from "@/api/client";
import type { components } from "@/api/schema";

/** Effective beets config + file freshness for the `/settings` view (generated). */
export type BeetsConfigSnapshot = components["schemas"]["BeetsConfigSnapshot"];

async function fetchConfig(): Promise<BeetsConfigSnapshot> {
  const { data, error, response } = await client.GET("/api/config");
  // Guard on !response.ok rather than `error` alone: a bodyless 5xx leaves
  // openapi-fetch's `error` undefined, and we must still surface a failure
  // instead of returning an undefined snapshot to the UI.
  if (error || !response.ok || !data) {
    throw new Error("Failed to load config");
  }
  return data;
}

/**
 * Fetch the effective beets config snapshot (`GET /api/config`). The page is
 * passive: read-only YAML + a mtime-derived restart hint. `staleTime: 30_000`
 * keeps the snapshot warm across short tab-switches without polling.
 */
export function useBeetsConfig() {
  return useQuery({
    queryKey: ["beets-config"],
    queryFn: fetchConfig,
    staleTime: 30_000,
  });
}
