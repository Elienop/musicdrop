import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { client } from "@/api/client";
import { unwrap } from "@/api/lib";
import type { components } from "@/api/schema";
import { throwIfRefused } from "@/api/useImport";

/** One Folder source (generated). slskd is never one: its message is the one
 * way in (decision #77). */
export type SourceSummary = components["schemas"]["SourceSummary"];
export type SourceList = components["schemas"]["SourceList"];
export type FolderSourceCreate = components["schemas"]["FolderSourceCreate"];

/** The sentence for an add the server did not word itself (FastAPI's own
 * validation 422, a 5xx, a dead backend). */
export const ADD_SOURCE_FAILED = "Couldn’t add that folder. Try again.";
export const REMOVE_SOURCE_FAILED = "Couldn’t remove that folder. Try again.";

const SOURCES_KEY = ["sources"] as const;

/** The Folder sources, in the order they were added. */
export function useSources() {
  return useQuery<SourceList, Error>({
    queryKey: SOURCES_KEY,
    queryFn: async () =>
      unwrap(await client.GET("/api/sources"), "Failed to load sources"),
  });
}

/**
 * Add a Folder source.
 *
 * A 422 carrying the server's own sentence (`That folder doesn’t exist.`, or
 * one of S1's refusals) throws that sentence, through the reader the import
 * start uses, so FastAPI's array-shaped 422 stays machine copy and takes
 * {@link ADD_SOURCE_FAILED} instead.
 */
export function useAddFolderSource() {
  const qc = useQueryClient();
  return useMutation<SourceSummary, Error, FolderSourceCreate>({
    mutationFn: async (body) => {
      const result = await client.POST("/api/sources/folders", { body });
      throwIfRefused(result);
      return unwrap(result, ADD_SOURCE_FAILED);
    },
    onSuccess: () => void qc.invalidateQueries({ queryKey: SOURCES_KEY }),
  });
}

/**
 * Remove a Folder source. Touches no files.
 *
 * A 404 is the state asked for (the source is already gone, from another tab),
 * so it counts as done, as a bank row's removal does. Either way the row
 * leaves the cached list at once, so the page can move focus off it.
 */
export function useRemoveFolderSource() {
  const qc = useQueryClient();
  return useMutation<void, Error, string>({
    mutationFn: async (id) => {
      const { error, response } = await client.DELETE(
        "/api/sources/folders/{source_id}",
        { params: { path: { source_id: id } } },
      );
      if (response.status === 404) return;
      if (error !== undefined || !response.ok) {
        throw new Error(REMOVE_SOURCE_FAILED);
      }
    },
    onSuccess: (_, id) => {
      qc.setQueryData<SourceList>(SOURCES_KEY, (list) =>
        list === undefined
          ? list
          : { sources: list.sources.filter((source) => source.id !== id) },
      );
    },
    onSettled: () => void qc.invalidateQueries({ queryKey: SOURCES_KEY }),
  });
}
