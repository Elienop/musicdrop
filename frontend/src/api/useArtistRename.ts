import { useMutation, useQueryClient } from "@tanstack/react-query";

import { client } from "@/api/client";
import { detailMessage } from "@/api/lib";
import { invalidateLibraryContent } from "@/api/useEventStream";
import type { components } from "@/api/schema";

type ArtistRenameRequest = components["schemas"]["ArtistRenameRequest"];
type ArtistRenamePreview = components["schemas"]["ArtistRenamePreview"];
type ArtistRenameResult = components["schemas"]["ArtistRenameResult"];

export function usePreviewArtistRename() {
  return useMutation<ArtistRenamePreview, Error, ArtistRenameRequest>({
    mutationFn: async (body) => {
      const { data, error, response } = await client.POST("/api/artists/rename/preview", { body });
      // Guard on !response.ok: a bodyless 5xx leaves openapi-fetch's `error` undefined.
      // detailMessage surfaces the server's own message (this endpoint only ever
      // raises a plain-string 404 detail); the status fallback is last resort.
      if (error || !response.ok || !data) {
        if (response.status === 404)
          throw new Error(detailMessage(error) ?? "Artist not found — reload the page.");
        throw new Error(detailMessage(error) ?? "Preview failed");
      }
      return data;
    },
  });
}

export function useApplyArtistRename() {
  const queryClient = useQueryClient();
  return useMutation<ArtistRenameResult, Error, ArtistRenameRequest>({
    mutationFn: async (body) => {
      const { data, error, response } = await client.POST("/api/artists/rename", { body });
      // Guard on !response.ok: a bodyless 5xx leaves openapi-fetch's `error` undefined.
      // The structured mid-batch-500's message reaches the user (detailMessage returns detail.message; surfacing the recovery line too is a repo-wide lib.ts follow-up).
      if (error || !response.ok || !data) {
        if (response.status === 409)
          throw new Error(
            detailMessage(error) ?? "A library job is running; try again when it finishes.",
          );
        if (response.status === 404)
          throw new Error(detailMessage(error) ?? "Artist not found — reload the page.");
        throw new Error(detailMessage(error) ?? "Rename failed");
      }
      return data;
    },
    // A rename changes artist + album rows everywhere; refresh every library surface.
    onSettled: () => {
      invalidateLibraryContent(queryClient);
    },
  });
}
