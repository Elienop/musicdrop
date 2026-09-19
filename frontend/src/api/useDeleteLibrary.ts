import { useMutation, useQueryClient } from "@tanstack/react-query";

import { client } from "@/api/client";
import type { components } from "@/api/schema";

export type DeleteResult = components["schemas"]["DeleteResult"];

/** A failed delete, carrying the backend's recovery hint when it sent one.
 *
 * `recovery` is the only user-facing channel for the data-loss guard in
 * `backend/app/beets/delete.py` (`_recovery`): a delete that stops part-way
 * leaves files in Trash, and emptying Trash then can destroy the only copy.
 * Null whenever the body carried none — the 404/409 arms send a flat string. */
export class DeleteFailedError extends Error {
  readonly recovery: string | null;
  constructor(message: string, recovery: string | null) {
    super(message);
    this.name = "DeleteFailedError";
    this.recovery = recovery;
  }
}

/** The recovery hint a failed delete carries, or null. Exported so both
 * dialogs read it the same way; a non-delete error answers null. */
export function deleteRecovery(error: unknown): string | null {
  return error instanceof DeleteFailedError ? error.recovery : null;
}

/** Pull the message and any recovery hint out of a delete op's error body. The
 * 404/409 ops nest a flat `detail: string`; the 500 nests
 * `detail: { message, recovery }`. A blank recovery is no recovery (the rule
 * `detailMessage` states for `detail`). */
function deleteError(error: unknown): DeleteFailedError {
  const detail = (error as { detail?: unknown } | undefined)?.detail;
  if (typeof detail === "string") return new DeleteFailedError(detail, null);
  if (detail && typeof detail === "object" && "message" in detail) {
    const { message, recovery } = detail as {
      message?: unknown;
      recovery?: unknown;
    };
    if (typeof message === "string") {
      const hint =
        typeof recovery === "string" && recovery.trim() !== "" ? recovery : null;
      return new DeleteFailedError(message, hint);
    }
  }
  return new DeleteFailedError("Delete failed", null);
}

/** Move an album's tracks, cover and lyrics to Trash and drop its DB rows.
 * Other files in the folder stay, and Restore re-imports only the tracks.
 * On success the album is gone library-wide, so blow the cache to refetch every
 * roster/grid/stat. */
export function useDeleteAlbum() {
  const qc = useQueryClient();
  return useMutation<DeleteResult, Error, number>({
    mutationFn: async (albumId: number): Promise<DeleteResult> => {
      const { data, error, response } = await client.DELETE("/api/albums/{album_id}", {
        params: { path: { album_id: albumId } },
      });
      if (!response.ok || !data) {
        throw deleteError(error);
      }
      return data;
    },
    onSuccess: () => void qc.invalidateQueries(),
  });
}

/** Move every album of an artist to Trash, on the same terms as
 * {@link useDeleteAlbum}. `name` is a query param so slashes (e.g. "AC/DC")
 * survive routing. */
export function useDeleteArtist() {
  const qc = useQueryClient();
  return useMutation<DeleteResult, Error, string>({
    mutationFn: async (name: string): Promise<DeleteResult> => {
      const { data, error, response } = await client.DELETE("/api/artists", {
        params: { query: { name } },
      });
      if (!response.ok || !data) {
        throw deleteError(error);
      }
      return data;
    },
    onSuccess: () => void qc.invalidateQueries(),
  });
}
