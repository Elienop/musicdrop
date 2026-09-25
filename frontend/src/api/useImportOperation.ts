import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { client } from "@/api/client";
import { unwrap } from "@/api/lib";
import type { components } from "@/api/schema";
import {
  type BeetsConfigSnapshot,
  type ConfigOpError,
  IMPORT_OPERATION_KEY,
  configOpError,
} from "@/api/useBeetsConfig";
import { NAMING_KEY } from "@/api/useNaming";

/** How beets places an imported file, as the loaded config resolves it
 * (generated; the backend's `Literal` is inlined, so it is read off the field). */
export type FileOperation = components["schemas"]["ImportOperation"]["operation"];
/** Body of `POST /api/config/import-operation` (generated contract). */
export type SetImportOperation = components["schemas"]["SetImportOperation"];

/**
 * What an import does with the files it adds, one line per operation. Add from
 * folder shows it under the path box; the slskd card reuses it, so the words
 * live here and nowhere else.
 */
export const IMPORT_OPERATION_LINE: Record<FileOperation, string> = {
  move: "Files move into your library.",
  hardlink: "Files stay, hardlinked into your library.",
  copy: "Files stay, copied into your library.",
  link: "Files stay, symlinked into your library.",
  reflink: "Files stay, cloned into your library.",
  reflink_auto: "Files stay, cloned into your library.",
  in_place: "Files stay where they are.",
};

/**
 * What imports use now (`GET /api/config/import-operation`): the operation
 * read at boot or at the last Apply, never the file as it stands. A saved but
 * unapplied edit therefore does not change it; an Apply or the Keep downloads
 * switch does, and both invalidate this key.
 */
export function useImportOperation() {
  return useQuery({
    queryKey: IMPORT_OPERATION_KEY,
    queryFn: async (): Promise<FileOperation> =>
      unwrap(
        await client.GET("/api/config/import-operation"),
        "Failed to load the import setting",
      ).operation,
  });
}

/**
 * The Keep downloads switch (`POST /api/config/import-operation`): writes
 * `hardlink: yes` or `move: yes` into config.yaml and reloads beets in one
 * request. Throws a {@link ConfigOpError} so the caller can read the status
 * and the body (a sentence, the Save's 409 conflict body, or Apply's
 * `{message, recovery}`).
 *
 * `onSettled` lives here, not in a `mutate()` callback (those do not run after
 * an unmount), and it RETURNS the refetches: TanStack awaits it before the
 * mutation settles, so `isPending` holds until the new operation and the new
 * file are both in the cache. The switch's thumb sits at the target until
 * then instead of flicking back to the old value for one refetch. It runs on
 * failure too: after the Save's 409 the refetched snapshot carries the sha the
 * retry needs.
 */
export function useSetImportOperation() {
  const queryClient = useQueryClient();
  return useMutation<BeetsConfigSnapshot, ConfigOpError, SetImportOperation>({
    mutationFn: async (body): Promise<BeetsConfigSnapshot> => {
      const { data, error, response } = await client.POST(
        "/api/config/import-operation",
        { body },
      );
      if (!response.ok || !data) {
        throw configOpError(
          "Import setting change failed",
          response.status,
          error,
        );
      }
      return data;
    },
    onSettled: () =>
      Promise.all([
        queryClient.invalidateQueries({ queryKey: ["beets-config"] }),
        queryClient.invalidateQueries({ queryKey: IMPORT_OPERATION_KEY }),
        queryClient.invalidateQueries({ queryKey: NAMING_KEY }),
        queryClient.invalidateQueries({ queryKey: ["active-import"] }),
      ]),
  });
}
