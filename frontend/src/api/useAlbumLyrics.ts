import { useMutation, useQueryClient } from "@tanstack/react-query";

import { client } from "@/api/client";
import type { components } from "@/api/schema";

/** Status of the (album- or library-scoped) lyrics job (generated contract). */
export type LyricsBackfillStatus = components["schemas"]["LyricsBackfillStatus"];

/**
 * Start an album-scoped lyrics fetch JOB. Returns its initial status; progress
 * is read by polling `useLyricsBackfillStatus` (the shared single slot). On
 * success we invalidate `["lyrics","backfill"]` so the poll wakes immediately.
 * A 409 (a library op / another fetch running) surfaces as a thrown error.
 */
export function useStartAlbumLyricsFetch(albumId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (): Promise<LyricsBackfillStatus> => {
      const { data, response } = await client.POST("/api/albums/{album_id}/lyrics/fetch", {
        params: { path: { album_id: albumId } },
      });
      if (!response.ok || !data) {
        throw new Error(
          response.status === 409
            ? "A library operation is in progress"
            : "Failed to start lyrics fetch",
        );
      }
      return data;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["lyrics", "backfill"] });
    },
  });
}
