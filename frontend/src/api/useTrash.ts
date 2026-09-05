import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { client } from "@/api/client";
import { invalidateLibraryContent } from "@/api/useEventStream";
import type { components } from "@/api/schema";

export type TrashedAlbum = components["schemas"]["TrashedAlbum"];
export type TrashListing = components["schemas"]["TrashListing"];
export type RestoreResult = components["schemas"]["RestoreResult"];

/** Pull the server's own sentence out of a trash op's error body
 * (`detail: string`), or null when there is none to pull.
 *
 * Reads the SAME property on both ends: openapi-fetch hands the parsed
 * `{detail: ...}` body over as its `error`, and `trashError` below re-attaches
 * that string to the Error it throws — so a caller holding the thrown Error
 * asks this one function too. One helper rather than two, because the two would
 * have to agree about which shapes count and nothing would check that they did.
 *
 * Null is a distinct answer from "Something went wrong", and callers need it:
 * `.message` alone cannot tell "the server explained itself" from "the request
 * never got an answer" — a transport failure carries a message too ("Failed to
 * fetch"), and showing that as if it were the server's advice sends the
 * operator looking for a setting to change that no one named. */
export function trashErrorDetail(error: unknown): string | null {
  const detail = (error as { detail?: unknown } | undefined)?.detail;
  return typeof detail === "string" ? detail : null;
}

/** The Error a failed trash call throws: the server's sentence as the message
 * when the body carried one, the generic line when it did not, and the sentence
 * (or null) kept on `detail` for a caller that needs to know which it got.
 *
 * `Object.assign` onto a real Error rather than a subclass, matching
 * `configOpError` in useBeetsConfig — `instanceof Error` still holds, which is
 * what TanStack Query's default error type and every existing `.message` reader
 * on these hooks rely on. */
function trashError(error: unknown): Error {
  const detail = trashErrorDetail(error);
  return Object.assign(new Error(detail ?? "Something went wrong"), { detail });
}

/** List the albums currently in Trash. */
export function useTrashList() {
  return useQuery({
    queryKey: ["trash"],
    queryFn: async (): Promise<TrashListing> => {
      const { data, error, response } = await client.GET("/api/trash");
      if (!response.ok || !data) throw trashError(error);
      return data;
    },
  });
}

/** Put a trashed album back. NOT always "as-is": the row's `restore_mode`
 * decides whether the backend moves the folder to its recorded origin or
 * re-imports it under the current naming rules — see `RestoreOutlook` in
 * SettingsTrashPage, which says which before the user commits. On success it
 * may rejoin the library, so blow the cache (roster/grids/stats) AND refresh
 * the trash list. */
export function useRestoreTrash() {
  const qc = useQueryClient();
  return useMutation<RestoreResult, Error, string>({
    mutationFn: async (folder: string): Promise<RestoreResult> => {
      const { data, error, response } = await client.POST("/api/trash/restore", {
        body: { folder },
      });
      if (!response.ok || !data) throw trashError(error);
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
      if (!response.ok) throw trashError(error);
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
      if (!response.ok) throw trashError(error);
    },
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["trash"] }),
  });
}
