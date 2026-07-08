import { useQuery } from "@tanstack/react-query";

import { client } from "@/api/client";
import { unwrap } from "@/api/lib";
import type { components } from "@/api/schema";

/** The release-completeness report for an album (generated contract). */
export type AlbumMissingReport = components["schemas"]["AlbumMissingReport"];
/** A single absent release track (generated contract). */
export type MissingReleaseTrack = components["schemas"]["MissingReleaseTrack"];

async function fetchAlbumMissing(albumId: number): Promise<AlbumMissingReport> {
  return unwrap(
    await client.GET("/api/albums/{album_id}/missing", {
      params: { path: { album_id: albumId } },
    }),
    "Failed to load missing tracks",
  );
}

/**
 * Fetch the missing-tracks report for an album. Disabled when the album has no
 * `mb_albumid` (a non-MusicBrainz album can't be checked) so no request fires.
 * Statuses (`release_unavailable` / `fetch_failed`) arrive in-band on a 200, so
 * only a transport failure rejects the query. The release data is immutable —
 * `staleTime: Infinity` avoids needless refetches.
 */
export function useAlbumMissing(albumId: number, mbAlbumId: string | null) {
  return useQuery({
    queryKey: ["album", albumId, "missing"],
    queryFn: () => fetchAlbumMissing(albumId),
    enabled: Boolean(mbAlbumId),
    retry: false,
    staleTime: Infinity,
  });
}
