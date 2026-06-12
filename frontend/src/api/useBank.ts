import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { client } from "@/api/client";
import { detailMessage } from "@/api/lib";
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
/** A banked parked-album payload (generated; `candidate` is the exact shape
 * the live review screen renders). */
export type ParkedAlbum = components["schemas"]["ParkedAlbum"];

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
 * left-open tab honest (the `useInboxItems` posture). */
const BANK_LIST_POLL_MS = 30_000;

/** Row poll while the apply runner owns it (queued/applying) — between the
 * run page's 1s job poll and the active probe's 5s. */
const BANK_ROW_POLL_MS = 2_000;

export interface BankListParams {
  status?: BankStatus;
  offset: number;
  limit: number;
}

async function fetchBankList(params: BankListParams): Promise<BankListResponse> {
  const { data, error, response } = await client.GET("/api/bank", {
    params: {
      query: {
        // undefined omits the param (unfiltered); the API treats absent as All.
        status: params.status,
        offset: params.offset,
        limit: params.limit,
      },
    },
  });
  if (error || !response.ok || !data) {
    throw new Error("Failed to load the bank");
  }
  return data;
}

/** One page of bank rows. `placeholderData` keeps the previous page visible
 * while the next loads (the `useAlbums`/`useBrowse` pagination dialect). */
export function useBankList(params: BankListParams) {
  return useQuery({
    queryKey: ["bank", "list", params.status ?? "all", params.offset, params.limit],
    queryFn: () => fetchBankList(params),
    placeholderData: (prev) => prev,
    refetchInterval: BANK_LIST_POLL_MS,
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
      return status === "queued" || status === "applying" ? BANK_ROW_POLL_MS : false;
    },
  });
}

async function decideBankItem(itemId: string, decision: BankDecision): Promise<BankItem> {
  const { data, error, response } = await client.POST("/api/bank/{item_id}/decision", {
    params: { path: { item_id: itemId } },
    body: decision,
  });
  // 409 = invalid transition (e.g. the row went queued in another tab). The
  // backend's detail is a STRING here; 422 (shape validation) is the ARRAY —
  // detailMessage tolerates both (the dual-shape carry-forward).
  if (response.status === 409 || response.status === 422) {
    throw new BankConflictError(
      detailMessage(error) ?? "This row changed state — go back and reopen it.",
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
    mutationFn: (itemId: string) => decideBankItem(itemId, { action: "ignore" }),
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
      detailMessage(error) ?? "The row is applying — wait for it to finish.",
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
  const { data, error, response } = await client.POST("/api/bank/bulk-ignore", {
    body: { ids },
  });
  if (error || !response.ok || !data) {
    throw new Error("Failed to ignore the selected rows");
  }
  return data;
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
