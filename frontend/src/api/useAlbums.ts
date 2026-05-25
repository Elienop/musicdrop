import { useQuery } from "@tanstack/react-query";

import { client } from "@/api/client";
import type { components } from "@/api/schema";

/** Page of albums as returned by `GET /api/albums` (generated contract). */
export type AlbumPage = components["schemas"]["AlbumPage"];
/** A single album row (generated contract). */
export type Album = components["schemas"]["Album"];

export interface UseAlbumsParams {
  limit: number;
  offset: number;
}

async function fetchAlbums({
  limit,
  offset,
}: UseAlbumsParams): Promise<AlbumPage> {
  const { data, error } = await client.GET("/api/albums", {
    params: { query: { limit, offset } },
  });
  if (error || !data) {
    throw new Error("Failed to load albums");
  }
  return data;
}

/** Fetch a page of albums. `keepPreviousData`-style placeholder keeps the
 * previous page visible while the next one loads, so pagination doesn't flash
 * the skeleton on every click. */
export function useAlbums({ limit, offset }: UseAlbumsParams) {
  return useQuery({
    queryKey: ["albums", { limit, offset }],
    queryFn: () => fetchAlbums({ limit, offset }),
    placeholderData: (prev) => prev,
  });
}
