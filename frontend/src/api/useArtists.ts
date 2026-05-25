import { useQuery } from "@tanstack/react-query";

import { client } from "@/api/client";
import type { components } from "@/api/schema";

/** A single artist row as returned by `GET /api/artists` (generated contract). */
export type Artist = components["schemas"]["Artist"];

async function fetchArtists(): Promise<Artist[]> {
  const { data, error } = await client.GET("/api/artists");
  if (error || !data) {
    throw new Error("Failed to load artists");
  }
  return data;
}

/** Fetch the full artist roster (`GET /api/artists`). */
export function useArtists() {
  return useQuery({
    queryKey: ["artists"],
    queryFn: fetchArtists,
  });
}
