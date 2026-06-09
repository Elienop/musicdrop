import { useMutation, useQuery } from "@tanstack/react-query";

import { client } from "@/api/client";
import type { components } from "@/api/schema";

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
 * returns `{ started: true, job_id }` to navigate into. A 409 (an import already
 * running) or a 404 (the folder vanished) throws so the caller can react.
 */
export function useImportInboxItem() {
  return useMutation<ReviewInboxResponse, Error, string>({
    mutationFn: async (name) => {
      const { data, error, response } = await client.POST(
        "/api/acquisition/inbox/items/import",
        { body: { name } },
      );
      if (error || !response.ok || !data)
        throw new Error("Failed to start inbox review");
      return data;
    },
  });
}
