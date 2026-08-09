import { useQuery } from "@tanstack/react-query";

import { client } from "@/api/client";
import { unwrap } from "@/api/lib";
import type { components } from "@/api/schema";

/** A single artist row as returned by `GET /api/artists` (generated contract). */
export type Artist = components["schemas"]["Artist"];

async function fetchArtists(): Promise<Artist[]> {
  return unwrap(await client.GET("/api/artists"), "Failed to load artists");
}

/**
 * Fetch the full artist roster (`GET /api/artists`).
 * staleTime: 5m — SSE-invalidated family; rationale in useStats.
 */
export function useArtists() {
  return useQuery({
    queryKey: ["artists"],
    queryFn: fetchArtists,
    staleTime: 5 * 60_000,
  });
}
