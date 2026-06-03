import { useMutation, useQueryClient } from "@tanstack/react-query";

import { client } from "@/api/client";
import type { components } from "@/api/schema";

/** Result of `POST /api/albums/{album_id}/lyrics/fetch` (generated contract). */
export type AlbumLyricsResult = components["schemas"]["AlbumLyricsResult"];

async function fetchAlbumLyrics(albumId: number): Promise<AlbumLyricsResult> {
  const { data, response } = await client.POST(
    "/api/albums/{album_id}/lyrics/fetch",
    { params: { path: { album_id: albumId } } },
  );
  // Guard on !response.ok (not just `error`) — a bodyless 5xx leaves
  // openapi-fetch's `error` undefined yet the call must surface a failure.
  if (!response.ok || !data) {
    throw new Error("Failed to fetch lyrics");
  }
  return data;
}

/**
 * Fetch missing lyrics for an album into the files. On success the album query
 * (`["album", albumId]`) is invalidated so the per-track has_lyrics indicators
 * refresh. A 409 (import/backfill running) surfaces as a thrown error the
 * caller renders inline.
 */
export function useAlbumLyricsFetch(albumId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => fetchAlbumLyrics(albumId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["album", albumId] });
    },
  });
}
