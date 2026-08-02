// frontend/src/api/useReorganize.ts
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import type { components } from "./schema";
import { client } from "./client";
import { unwrap } from "./lib";

export type ReorganizePlan = components["schemas"]["ReorganizePlan"];
export type ReorganizeMove = components["schemas"]["ReorganizeMove"];
export type ReorganizeBackfillStatus = components["schemas"]["ReorganizeBackfillStatus"];
export type ReorganizeUnitFailure = components["schemas"]["ReorganizeUnitFailure"];

export const REORGANIZE_STATUS_KEY = ["reorganize", "status"] as const;

export type ReorganizeScope =
  | { scope: "library" }
  | { scope: "artist"; artist: string }
  | { scope: "album"; albumId: number };

export function useReorganizeStatus() {
  const qc = useQueryClient();
  return useQuery({
    queryKey: REORGANIZE_STATUS_KEY,
    queryFn: async (): Promise<ReorganizeBackfillStatus> => {
      const { data, response } = await client.GET("/api/reorganize/status");
      if (!response.ok || !data) {
        // Never fabricate `idle` over a live snapshot: a 502/504 (proxy hiccup
        // while the reorganize hammers the NAS) would overwrite a "running"
        // status and flip refetchInterval to false, stopping the poll for good.
        // Keep the last-known status so the running poll self-heals.
        return (
          qc.getQueryData<ReorganizeBackfillStatus>(REORGANIZE_STATUS_KEY) ?? {
            phase: "idle", job_id: null, scope: null, total: 0, processed: 0, moved: 0,
            skipped: 0, failed: 0, orphans_trashed: 0, current: null, error: null, artist: null,
            album_id: null, scope_label: "library", failures: [], finished_at: null,
          }
        );
      }
      return data;
    },
    refetchInterval: (q) => (q.state.data?.phase === "running" ? 1000 : false),
  });
}

export function usePreviewReorganize() {
  return useMutation<ReorganizePlan, Error, ReorganizeScope>({
    mutationFn: async (s): Promise<ReorganizePlan> => {
      if (s.scope === "album") {
        const { data, response } = await client.GET(
          "/api/albums/{album_id}/reorganize/preview",
          { params: { path: { album_id: s.albumId } } },
        );
        if (response.status === 404) throw new Error("Album not found");
        if (!response.ok || !data) throw new Error("Failed to build preview");
        return data;
      }
      const query = s.scope === "artist" ? { artist: s.artist } : {};
      const { data, response } = await client.GET("/api/reorganize/preview", {
        params: { query },
      });
      if (!response.ok || !data) throw new Error("Failed to build preview");
      return data;
    },
  });
}

export function useStartReorganize() {
  const qc = useQueryClient();
  return useMutation<ReorganizeBackfillStatus, Error, ReorganizeScope>({
    mutationFn: async (s): Promise<ReorganizeBackfillStatus> => {
      if (s.scope === "album") {
        const { data, response } = await client.POST(
          "/api/albums/{album_id}/reorganize",
          { params: { path: { album_id: s.albumId } } },
        );
        if (response.status === 409) throw new Error("A library operation is in progress");
        if (response.status === 404) throw new Error("Album not found");
        if (!response.ok || !data) throw new Error("Failed to start reorganize");
        return data;
      }
      const query = s.scope === "artist" ? { artist: s.artist } : {};
      const { data, response } = await client.POST("/api/reorganize", { params: { query } });
      if (response.status === 409) throw new Error("A library operation is in progress");
      if (!response.ok || !data) throw new Error("Failed to start reorganize");
      return data;
    },
    onSuccess: () => void qc.invalidateQueries({ queryKey: REORGANIZE_STATUS_KEY }),
  });
}

export function useStopReorganize() {
  const qc = useQueryClient();
  return useMutation<ReorganizeBackfillStatus, Error, void>({
    mutationFn: async (): Promise<ReorganizeBackfillStatus> =>
      unwrap(await client.POST("/api/reorganize/stop"), "Failed to stop"),
    onSuccess: () => void qc.invalidateQueries({ queryKey: REORGANIZE_STATUS_KEY }),
  });
}

/** Clear a FINISHED job out of the single slot, which otherwise holds its
 * failure rows until the next job starts — so a fixed problem keeps showing.
 * Idempotent server-side (an empty slot answers 200 with the idle status), and
 * refused with 409 while a job is running. Same invalidation as stop: the
 * status query is the one source of truth for what the control renders. */
export function useDismissReorganize() {
  const qc = useQueryClient();
  return useMutation<ReorganizeBackfillStatus, Error, void>({
    mutationFn: async (): Promise<ReorganizeBackfillStatus> =>
      unwrap(await client.POST("/api/reorganize/dismiss"), "Failed to dismiss the result"),
    onSuccess: () => void qc.invalidateQueries({ queryKey: REORGANIZE_STATUS_KEY }),
  });
}
