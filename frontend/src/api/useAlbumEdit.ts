import { useMutation, useQueryClient } from "@tanstack/react-query";
import { client } from "@/api/client";
import type { components } from "@/api/schema";

type AlbumEditRequest = components["schemas"]["AlbumEditRequest"];
type AlbumEditPreview = components["schemas"]["AlbumEditPreview"];
type AlbumEditResult = components["schemas"]["AlbumEditResult"];

export function usePreviewAlbumEdit(albumId: number) {
  return useMutation<AlbumEditPreview, Error, AlbumEditRequest>({
    mutationFn: async (body) => {
      const { data, error, response } = await client.POST("/api/albums/{album_id}/edit/preview", {
        params: { path: { album_id: albumId } },
        body,
      });
      // Guard on !response.ok: a bodyless 5xx leaves openapi-fetch's `error` undefined.
      if (error || !response.ok || !data) throw new Error("Preview failed");
      return data;
    },
  });
}

export function useApplyAlbumEdit(albumId: number) {
  const queryClient = useQueryClient();
  return useMutation<AlbumEditResult, Error, AlbumEditRequest>({
    mutationFn: async (body) => {
      const { data, error, response } = await client.POST("/api/albums/{album_id}/edit", {
        params: { path: { album_id: albumId } },
        body,
      });
      // Guard on !response.ok: a bodyless 5xx leaves openapi-fetch's `error` undefined.
      if (error || !response.ok || !data) {
        if (response.status === 409) throw new Error("An import is running — try again when it finishes.");
        throw new Error("Edit failed");
      }
      return data;
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ["album", albumId] });
    },
  });
}
