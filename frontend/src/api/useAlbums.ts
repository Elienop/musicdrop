import { useQuery } from "@tanstack/react-query";

import { client } from "@/api/client";
import { unwrap } from "@/api/lib";
import type { components } from "@/api/schema";

/** Page of albums as returned by `GET /api/albums` (generated contract). */
export type AlbumPage = components["schemas"]["AlbumPage"];
/** A single album row (generated contract). */
export type Album = components["schemas"]["Album"];

export interface UseAlbumsParams {
  limit: number;
  offset: number;
  /** Optional artist filter (`?artist=`). Omitted from the query when unset. */
  artist?: string;
}

async function fetchAlbums({
  limit,
  offset,
  artist,
}: UseAlbumsParams): Promise<AlbumPage> {
  return unwrap(
    await client.GET("/api/albums", {
      params: { query: { limit, offset, artist } },
    }),
    "Failed to load albums",
  );
}

/** Fetch a page of albums. `keepPreviousData`-style placeholder keeps the
 * previous page visible while the next one loads, so pagination doesn't flash
 * the skeleton on every click. */
export function useAlbums({ limit, offset, artist }: UseAlbumsParams) {
  return useQuery({
    queryKey: ["albums", { limit, offset, artist }],
    queryFn: () => fetchAlbums({ limit, offset, artist }),
    placeholderData: (prev) => prev,
  });
}
