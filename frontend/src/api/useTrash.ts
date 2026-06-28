import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { client } from "@/api/client";
import { invalidateLibraryContent } from "@/api/useEventStream";
import type { components } from "@/api/schema";

export type TrashedAlbum = components["schemas"]["TrashedAlbum"];
export type TrashListing = components["schemas"]["TrashListing"];
export type RestoreResult = components["schemas"]["RestoreResult"];

/** Pull a human message out of a trash op's error body (`detail: string`). */
function trashErrorMessage(error: unknown): string {
  const detail = (error as { detail?: unknown } | undefined)?.detail;
  if (typeof detail === "string") return detail;
  return "Something went wrong";
}

/** List the albums currently in Trash. */
export function useTrashList() {
  return useQuery({
    queryKey: ["trash"],
    queryFn: async (): Promise<TrashListing> => {
      const { data, error, response } = await client.GET("/api/trash");
      if (!response.ok || !data) throw new Error(trashErrorMessage(error));
      return data;
    },
  });
}

/** Re-import a trashed album as-is. On success it may rejoin the library, so
 * blow the cache (roster/grids/stats) AND refresh the trash list. */
export function useRestoreTrash() {
  const qc = useQueryClient();
  return useMutation<RestoreResult, Error, string>({
    mutationFn: async (folder: string): Promise<RestoreResult> => {
      const { data, error, response } = await client.POST("/api/trash/restore", {
        body: { folder },
      });
      if (!response.ok || !data) throw new Error(trashErrorMessage(error));
      return data;
    },
    onSuccess: () => invalidateLibraryContent(qc),
  });
}

/** Permanently remove one trashed album folder. */
export function useEmptyTrashAlbum() {
  const qc = useQueryClient();
  return useMutation<void, Error, string>({
    mutationFn: async (folder: string): Promise<void> => {
      const { error, response } = await client.DELETE("/api/trash", {
        params: { query: { folder } },
      });
      if (!response.ok) throw new Error(trashErrorMessage(error));
    },
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["trash"] }),
  });
}

/** Permanently clear the whole Trash dir. */
export function useEmptyAllTrash() {
  const qc = useQueryClient();
  return useMutation<void, Error, void>({
    mutationFn: async (): Promise<void> => {
      const { error, response } = await client.DELETE("/api/trash/all");
      if (!response.ok) throw new Error(trashErrorMessage(error));
    },
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["trash"] }),
  });
}
