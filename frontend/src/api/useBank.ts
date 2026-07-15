import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { client } from "@/api/client";
import { detailMessage, unwrap } from "@/api/lib";
import type { components } from "@/api/schema";

/** One bank list row (generated contract — no candidate payloads). */
export type BankItemSummary = components["schemas"]["BankItemSummary"];
/** The full banked row, payloads included (generated contract). */
export type BankItem = components["schemas"]["BankItem"];
/** The user's verdict on a banked row (generated contract). */
export type BankDecision = components["schemas"]["BankDecision"];
/** `GET /api/bank` page envelope (generated contract). */
export type BankListResponse = components["schemas"]["BankListResponse"];
/** Row lifecycle status (derived from the generated row type). */
export type BankStatus = BankItemSummary["status"];
/** Why a row was banked (derived from the generated row type) — the
 * `reason` list filter's value space. */
export type BankReason = BankItemSummary["reason"];
/** A banked parked-album payload (generated; `candidate` is the exact shape
 * the live review screen renders). */
export type ParkedAlbum = components["schemas"]["ParkedAlbum"];
/** One in-library album a banked candidate collides with (generated). */
export type ExistingAlbum = components["schemas"]["ExistingAlbum"];

/** Thrown when the bank refuses a transition (409: deciding a queued row,
 * deleting an applying row) or the row vanished under a decision (404).
 * Carries the backend's string `detail` so the UI can show the real reason. */
export class BankConflictError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "BankConflictError";
  }
}

/** Quiet backlog cadence — the list changes when a sweep banks a row or an
 * apply resolves one; mutations invalidate immediately, the poll just keeps a
 * left-open tab honest (the `useInboxItems` posture). While any visible row is
 * queued/applying the list drops to the fast row cadence so the apply runner's
 * progress is seen live, not 30s late. */
const BANK_LIST_POLL_MS = 30_000;

/** Row poll while the apply runner owns it (queued/applying) — between the
 * run page's 1s job poll and the active probe's 5s. */
const BANK_ROW_POLL_MS = 2_000;

/** Fast cadence while the apply runner owns any visible row, lazy otherwise.
 * Exported for tests. */
export function bankListPollMs(
  items: readonly { status: string }[] | undefined,
): number {
  const inFlight = (items ?? []).some(
    (item) => item.status === "queued" || item.status === "applying",
  );
  return inFlight ? BANK_ROW_POLL_MS : BANK_LIST_POLL_MS;
}

export interface BankListParams {
  status?: BankStatus;
  /** `active` narrows an unfiltered list to the needs-attention statuses
   * (the Review page's default); absent/`all` keeps every row. */
  view?: "all" | "active";
  /** Narrows the list to rows banked for one reason (uncertain match /
   * already-in-library / no-match); absent keeps every reason. */
  reason?: BankReason;
  offset: number;
  limit: number;
}

async function fetchBankList(
  params: BankListParams,
): Promise<BankListResponse> {
  return unwrap(
    await client.GET("/api/bank", {
      params: {
        query: {
          // undefined omits the param (unfiltered); the API treats absent as All.
          status: params.status,
          view: params.view,
          reason: params.reason,
          offset: params.offset,
          limit: params.limit,
        },
      },
    }),
    "Failed to load the bank",
  );
}

/** One page of bank rows. `placeholderData` keeps the previous page visible
 * while the next loads (the `useAlbums`/`useBrowse` pagination dialect). */
export function useBankList(params: BankListParams) {
  return useQuery({
    queryKey: [
      "bank",
      "list",
      params.status ?? "all",
      params.view ?? "all",
      params.reason ?? "all",
      params.offset,
      params.limit,
    ],
    queryFn: () => fetchBankList(params),
    placeholderData: (prev) => prev,
    refetchInterval: (query) => bankListPollMs(query.state.data?.items),
  });
}

async function fetchBankItem(itemId: string): Promise<BankItem> {
  const { data, error, response } = await client.GET("/api/bank/{item_id}", {
    params: { path: { item_id: itemId } },
  });
  if (response.status === 404) {
    throw new BankConflictError("This row is no longer in the bank.");
  }
  if (error || !response.ok || !data) {
    throw new Error("Failed to load the bank row");
  }
  return data;
}

/**
 * One full bank row (`GET /api/bank/{id}`), payloads included. Disabled until
 * an id exists. Polls ONLY while the apply runner owns the row
 * (queued/applying) so "Queued to apply" progresses to done/failed live;
 * settled rows don't poll. Errors stop the loop (a 404 means removed/applied).
 */
export function useBankItem(itemId: string | undefined) {
  return useQuery({
    queryKey: ["bank", "item", itemId],
    queryFn: () => {
      if (!itemId) throw new Error("no bank item id");
      return fetchBankItem(itemId);
    },
    enabled: Boolean(itemId),
    retry: false,
    refetchInterval: (query) => {
      if (query.state.error) {
        return false;
      }
      const status = query.state.data?.status;
      return status === "queued" || status === "applying"
        ? BANK_ROW_POLL_MS
        : false;
    },
  });
}

/** Library-collision check for a banked candidate
 * (`GET /api/bank/{id}/duplicates`). Keyed on the selected candidate so a
 * switcher change re-checks; gate `enabled` to decidable candidate rows. */
export function useBankDuplicates(
  itemId: string,
  candidateIndex: number,
  enabled: boolean,
) {
  return useQuery({
    queryKey: ["bank", "duplicates", itemId, candidateIndex],
    enabled,
    queryFn: async () =>
      unwrap(
        await client.GET("/api/bank/{item_id}/duplicates", {
          params: {
            path: { item_id: itemId },
            query: { candidate_index: candidateIndex },
          },
        }),
        "Failed to check for duplicates",
      ),
  });
}

/** `POST /api/bank/{id}/search` response (generated contract). */
export type BankSearchResponse = components["schemas"]["BankSearchResponse"];
/** Re-lookup parameters (generated; shared with the live import search). */
export type ImportSearch = components["schemas"]["ImportSearch"];

/**
 * Re-look-up a banked folder (`POST /api/bank/{id}/search`) — the attended
 * flow's release search, run offline against the banked files. Success writes
 * the returned row straight into the item cache (a no_match row re-branches
 * to the candidate screen without navigation); a 409 (stale flip / status
 * race) refetches the row so the page lands on the right screen.
 */
export function useBankSearch(itemId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (search: ImportSearch): Promise<BankSearchResponse> => {
      const { data, error, response } = await client.POST(
        "/api/bank/{item_id}/search",
        { params: { path: { item_id: itemId } }, body: search },
      );
      if (response.status === 409 || response.status === 422) {
        throw new BankConflictError(
          detailMessage(error) ?? "This row changed state. Go back and reopen it.",
        );
      }
      if (response.status === 404) {
        throw new BankConflictError("This row is no longer in the bank.");
      }
      if (error || !response.ok || !data) {
        throw new Error("Failed to run the search");
      }
      return data;
    },
    onSuccess: (res) => {
      queryClient.setQueryData(["bank", "item", itemId], res.item);
      void queryClient.invalidateQueries({ queryKey: ["bank", "list"] });
      void queryClient.invalidateQueries({ queryKey: ["bank", "duplicates", itemId] });
    },
    onError: (err) => {
      if (err instanceof BankConflictError) {
        void queryClient.invalidateQueries({ queryKey: ["bank", "item", itemId] });
      }
    },
  });
}

/**
 * Re-scan a banked folder (`POST /api/bank/{id}/rescan`) — re-reads the folder
 * from disk, re-matches with beets' default lookup, and REFRESHES the
 * fingerprint (the explicit "I changed the folder on purpose" gesture; search
 * treats a changed folder as stale instead). Success writes the returned row
 * into the item cache, so the page re-branches to whatever the row now is
 * (stale → candidate, candidate → no-match, dup-prompt → candidate).
 */
export function useBankRescan(itemId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (): Promise<BankItem> => {
      const { data, error, response } = await client.POST(
        "/api/bank/{item_id}/rescan",
        { params: { path: { item_id: itemId } } },
      );
      if (response.status === 409) {
        throw new BankConflictError(
          detailMessage(error) ?? "This row can’t be rescanned. Go back and reopen it.",
        );
      }
      if (response.status === 404) {
        throw new BankConflictError("This row is no longer in the bank.");
      }
      if (error || !response.ok || !data) {
        throw new Error("Failed to rescan the folder");
      }
      return data;
    },
    onSuccess: (item) => {
      queryClient.setQueryData(["bank", "item", itemId], item);
      void queryClient.invalidateQueries({ queryKey: ["bank", "list"] });
      void queryClient.invalidateQueries({ queryKey: ["bank", "duplicates", itemId] });
    },
    onError: (err) => {
      if (err instanceof BankConflictError) {
        void queryClient.invalidateQueries({ queryKey: ["bank", "item", itemId] });
      }
    },
  });
}

async function decideBankItem(
  itemId: string,
  decision: BankDecision,
): Promise<BankItem> {
  const { data, error, response } = await client.POST(
    "/api/bank/{item_id}/decision",
    {
      params: { path: { item_id: itemId } },
      body: decision,
    },
  );
  // 409 = invalid transition (e.g. the row went queued in another tab). The
  // backend's detail is a STRING here; 422 (shape validation) is the ARRAY —
  // detailMessage tolerates both (the dual-shape carry-forward).
  if (response.status === 409 || response.status === 422) {
    throw new BankConflictError(
      detailMessage(error) ?? "This row changed state. Go back and reopen it.",
    );
  }
  if (response.status === 404) {
    throw new BankConflictError("This row is no longer in the bank.");
  }
  if (error || !response.ok || !data) {
    throw new Error("Failed to submit the decision");
  }
  return data;
}

/**
 * Post a `BankDecision` (`POST /api/bank/{id}/decision`). A decision is a
 * store-only write — it NEVER needs the import slot (the backend queues the
 * row; its apply runner waits for the slot on its own). Invalidates every
 * bank query on settle so the list shows the row as queued/ignored.
 */
export function useBankDecision(itemId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (decision: BankDecision) => decideBankItem(itemId, decision),
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ["bank"] });
    },
  });
}

/** List-row ignore: the same decision POST, id supplied per call (the
 * Review page's backlog renders many rows under one mutation). */
export function useIgnoreBankItem() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (itemId: string) =>
      decideBankItem(itemId, { action: "ignore" }),
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ["bank"] });
    },
  });
}

async function deleteBankItem(itemId: string): Promise<void> {
  const { error, response } = await client.DELETE("/api/bank/{item_id}", {
    params: { path: { item_id: itemId } },
  });
  if (response.status === 404) {
    return; // already gone — exactly the state the caller wanted
  }
  if (response.status === 409) {
    throw new BankConflictError(
      detailMessage(error) ?? "The row is applying. Wait for it to finish.",
    );
  }
  if (error || !response.ok) {
    throw new Error("Failed to remove the row");
  }
}

/** Remove a row (`DELETE /api/bank/{id}`) — files untouched. 404 resolves
 * quietly; 409 (mid-apply) surfaces as {@link BankConflictError}. */
export function useDeleteBankItem() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: deleteBankItem,
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ["bank"] });
    },
  });
}

async function bulkIgnoreBank(
  ids: string[],
): Promise<components["schemas"]["BankBulkIgnoreResponse"]> {
  return unwrap(
    await client.POST("/api/bank/bulk-ignore", { body: { ids } }),
    "Failed to ignore the selected rows",
  );
}

/** Bulk-ignore (`POST /api/bank/bulk-ignore`). The backend skips ids that are
 * not `needs_review` and reports how many actually flipped. */
export function useBulkIgnoreBank() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: bulkIgnoreBank,
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ["bank"] });
    },
  });
}

async function bulkDeleteBank(
  ids: string[],
): Promise<components["schemas"]["BankBulkDeleteResponse"]> {
  return unwrap(
    await client.POST("/api/bank/bulk-delete", { body: { ids } }),
    "Failed to remove the selected rows",
  );
}

/** Bulk-delete (`POST /api/bank/bulk-delete`). The backend skips `applying`
 * and missing ids and reports how many rows were actually removed. */
export function useBulkDeleteBank() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: bulkDeleteBank,
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ["bank"] });
    },
  });
}
