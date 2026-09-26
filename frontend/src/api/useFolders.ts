import { useQuery, useQueryClient } from "@tanstack/react-query";

import { client } from "@/api/client";
import { unwrap } from "@/api/lib";
import type { components } from "@/api/schema";
import { throwIfRefused } from "@/api/useImport";

/** One folder's direct child folders (`GET /api/folders`, generated). */
export type FolderListing = components["schemas"]["FolderListing"];
/** One child folder in a {@link FolderListing} (generated). */
export type FolderEntry = components["schemas"]["FolderEntry"];
/** What a folder is to MusicDrop. Read off the generated field: Pydantic
 * inlines a `Literal`, so there is no named schema for it. */
export type FolderBadge = NonNullable<FolderEntry["badge"]>;

/** The sentence for a failure the server did not word itself. */
export const FOLDER_LIST_FAILED = "Couldn’t open that folder.";

/** `null` asks for the server's default (`/media`, else `/`). */
export function folderListingKey(path: string | null) {
  return ["folders", path] as const;
}

/**
 * List one folder's child folders.
 *
 * A 409 (two folders display alike) or a 422 (unreadable) throws the server's
 * own sentence, through the same reader the start route's refusals use, so
 * FastAPI's array-shaped validation 422 stays machine copy and takes
 * {@link FOLDER_LIST_FAILED} instead.
 *
 * The server may answer with a different folder than the one asked for: the
 * default, or the nearest existing parent of a missing path. That answer is
 * also stored under its own path, because the browser then shows that path in
 * its box and asks for it; without the copy the same listing is fetched twice
 * on every open.
 *
 * No retry: a 409 or 422 is the folder's answer, and a second ask only shows
 * it a second later. The request carries TanStack's `signal`, so closing the
 * dialog stops waiting on a folder that hangs (a dead mount).
 */
export function useFolderListing(path: string | null) {
  const queryClient = useQueryClient();
  return useQuery<FolderListing, Error>({
    queryKey: folderListingKey(path),
    queryFn: async ({ signal }) => {
      const result = await client.GET("/api/folders", {
        params: { query: path === null ? {} : { path } },
        signal,
      });
      throwIfRefused(result);
      const listing = unwrap(result, FOLDER_LIST_FAILED);
      if (listing.path !== path) {
        queryClient.setQueryData(folderListingKey(listing.path), listing);
      }
      return listing;
    },
    retry: false,
  });
}
