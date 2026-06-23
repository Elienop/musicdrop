import { useQuery } from "@tanstack/react-query";

import { client } from "@/api/client";
import type { components } from "@/api/schema";

/** Album detail with tracklist as returned by `GET /api/albums/{album_id}`
 * (generated contract). */
export type AlbumDetail = components["schemas"]["AlbumDetail"];
/** A single tracklist row (generated contract). */
export type Track = components["schemas"]["Track"];
/** Which release an album is — source, edition, label, release link (generated). */
export type ReleaseIdentity = components["schemas"]["ReleaseIdentity"];

/** Thrown when the album id is unknown (API 404). Lets the page distinguish a
 * genuine not-found from a transient/server error and render the dedicated
 * "Album not found" state instead of the generic retry error. */
export class AlbumNotFoundError extends Error {
  constructor(albumId: number) {
    super(`Album ${albumId} not found`);
    this.name = "AlbumNotFoundError";
  }
}

async function fetchAlbum(albumId: number): Promise<AlbumDetail> {
  const { data, error, response } = await client.GET(
    "/api/albums/{album_id}",
    { params: { path: { album_id: albumId } } },
  );
  if (response.status === 404) {
    throw new AlbumNotFoundError(albumId);
  }
  if (error || !data) {
    throw new Error("Failed to load album");
  }
  return data;
}

export interface UseAlbumOptions {
  /** When false the query is disabled (no request fires). Used by the page to
   * skip fetching for a non-numeric / invalid id. Defaults to true. */
  enabled?: boolean;
}

/** Fetch a single album with its tracklist. A 404 surfaces as
 * {@link AlbumNotFoundError} (not retried) so the page can branch to the
 * not-found state. */
export function useAlbum(
  albumId: number,
  { enabled = true }: UseAlbumOptions = {},
) {
  return useQuery({
    queryKey: ["album", albumId],
    queryFn: () => fetchAlbum(albumId),
    enabled,
    // No auto-retry: a not-found is terminal, and a transient/server error
    // surfaces immediately behind the page's explicit Retry button (mirroring
    // the Albums grid). Skipping retries also keeps the error state
    // deterministic instead of waiting out a backoff.
    retry: false,
  });
}
