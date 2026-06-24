import { useMutation, useQueryClient } from "@tanstack/react-query";

import { client } from "@/api/client";
import type { components } from "@/api/schema";

export type DeleteResult = components["schemas"]["DeleteResult"];

/** Pull a human message out of a delete op's error body. The 404/409 ops nest a
 * flat `detail: string`; the 500 nests `detail: { message, recovery }`. */
function deleteErrorMessage(error: unknown): string {
  const detail = (error as { detail?: unknown } | undefined)?.detail;
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object" && "message" in detail) {
    const message = (detail as { message?: unknown }).message;
    if (typeof message === "string") return message;
  }
  return "Delete failed";
}

/** Move a whole album (its folder + DB rows) to Trash. On success the album is
 * gone library-wide, so blow the cache to refetch every roster/grid/stat. */
export function useDeleteAlbum() {
  const qc = useQueryClient();
  return useMutation<DeleteResult, Error, number>({
    mutationFn: async (albumId: number): Promise<DeleteResult> => {
      const { data, error, response } = await client.DELETE("/api/albums/{album_id}", {
        params: { path: { album_id: albumId } },
      });
      if (!response.ok || !data) {
        throw new Error(deleteErrorMessage(error));
      }
      return data;
    },
    onSuccess: () => void qc.invalidateQueries(),
  });
}

/** Move every album of an artist to Trash. `name` is a query param so slashes
 * (e.g. "AC/DC") survive routing. */
export function useDeleteArtist() {
  const qc = useQueryClient();
  return useMutation<DeleteResult, Error, string>({
    mutationFn: async (name: string): Promise<DeleteResult> => {
      const { data, error, response } = await client.DELETE("/api/artists", {
        params: { query: { name } },
      });
      if (!response.ok || !data) {
        throw new Error(deleteErrorMessage(error));
      }
      return data;
    },
    onSuccess: () => void qc.invalidateQueries(),
  });
}
