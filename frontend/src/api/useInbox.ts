import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { client } from "@/api/client";
import { unwrap } from "@/api/lib";
import type { components } from "@/api/schema";
import { throwIfRefused } from "@/api/useImport";

/** One inbox backlog row (generated contract). */
export type InboxItem = components["schemas"]["InboxItem"];
/** The inbox backlog listing (generated contract). */
export type InboxListing = components["schemas"]["InboxListing"];
/** Result of starting a single-folder (or whole-inbox) review (generated). */
export type ReviewInboxResponse = components["schemas"]["ReviewInboxResponse"];

/** The "nothing in the inbox" fallback — the probe never throws (an
 * informational backlog list isn't worth a red banner on a hiccup). */
const EMPTY: InboxListing = { items: [] };

/**
 * Poll the inbox backlog (`GET /api/acquisition/inbox/items`).
 *
 * The source-agnostic set-aside list the Review page renders per item. Quiet
 * cadence — the list only changes when a download lands or a review resolves —
 * but it keeps polling so a background drop appears without a manual refresh.
 */
export function useInboxItems() {
  return useQuery<InboxListing>({
    queryKey: ["inbox-items"],
    queryFn: async () => {
      const { data, response } = await client.GET("/api/acquisition/inbox/items");
      if (!response.ok || !data) return EMPTY;
      return data;
    },
    refetchInterval: 30_000,
  });
}

/**
 * Import ONE inbox folder by name (the per-item "Review" action).
 *
 * The server resolves + contains the name under the inbox; a started import
 * returns `{ started: true, job_id }` to navigate into. A 404 (the folder
 * vanished) throws so the caller can react. A 409, 422 or 503 carrying the
 * server's own STRING sentence throws THAT, not the generic one — see
 * {@link throwIfRefused}. This route takes a body, so it can also send
 * FastAPI's array-shaped 422, which stays machine copy and falls through.
 */
export function useImportInboxItem() {
  const qc = useQueryClient();
  return useMutation<ReviewInboxResponse, Error, string>({
    mutationFn: async (name) => {
      const result = await client.POST("/api/acquisition/inbox/items/import", {
        body: { name },
      });
      throwIfRefused(result);
      return unwrap(result, "Failed to start inbox review");
    },
    // Refresh the backlog + the import gate whatever the outcome: a start
    // emptied/changed the inbox, a 404 means the folder vanished (drop the stale
    // row), a 409 means the slot is now busy (disable the actions).
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: ["inbox-items"] });
      void qc.invalidateQueries({ queryKey: ["active-import"] });
    },
  });
}
