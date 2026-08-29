// frontend/src/api/useDiskSync.ts
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import type { components } from "./schema";
import { client } from "./client";
import { unwrap } from "./lib";

export type DiskSyncPlan = components["schemas"]["DiskSyncPlan"];
export type DiskSyncStatus = components["schemas"]["DiskSyncStatus"];
export type DiskSyncReadError = components["schemas"]["DiskSyncReadError"];
export type DiskSyncEmptiedAlbum =
  components["schemas"]["DiskSyncEmptiedAlbum"];

export const DISK_SYNC_STATUS_KEY = ["disk-sync", "status"] as const;

const IDLE: DiskSyncStatus = {
  phase: "idle", job_id: null, total: 0, processed: 0, removed: 0, updated: 0,
  unchanged: 0, read_errors: 0, emptied_albums: 0, playlists_reexported: 0,
  current: null, error: null, failures: [],
};

export function useDiskSyncStatus() {
  const qc = useQueryClient();
  return useQuery({
    queryKey: DISK_SYNC_STATUS_KEY,
    queryFn: async (): Promise<DiskSyncStatus> => {
      const { data, response } = await client.GET("/api/disk-sync/status");
      // Never fabricate `idle` over a live snapshot: a 502/504 (proxy hiccup
      // while the sync hammers the NAS) would overwrite a "running" status and
      // flip refetchInterval to false, stopping the poll for good. Keep the
      // last-known status so the running poll self-heals; idle only on cold load.
      if (!response.ok || !data) {
        return qc.getQueryData<DiskSyncStatus>(DISK_SYNC_STATUS_KEY) ?? IDLE;
      }
      return data;
    },
    refetchInterval: (q) => (q.state.data?.phase === "running" ? 1000 : false),
  });
}

export function usePreviewDiskSync() {
  return useMutation<DiskSyncPlan, Error, void>({
    mutationFn: async (): Promise<DiskSyncPlan> => {
      const { data, response } = await client.GET("/api/disk-sync/preview");
      if (response.status === 503) throw new Error("Library folder unavailable. Is the music share mounted?");
      if (!response.ok || !data) throw new Error("Failed to build the sync preview");
      return data;
    },
  });
}

export function useStartDiskSync() {
  const qc = useQueryClient();
  return useMutation<DiskSyncStatus, Error, void>({
    mutationFn: async (): Promise<DiskSyncStatus> => {
      const { data, response, error } = await client.POST("/api/disk-sync");
      if (response.status === 409) throw new Error("Another library operation is running; try again when it finishes");
      if (error || !response.ok || !data) throw new Error("Failed to start the sync");
      return data;
    },
    onSuccess: (s) => qc.setQueryData(DISK_SYNC_STATUS_KEY, s),
  });
}

export function useStopDiskSync() {
  const qc = useQueryClient();
  return useMutation<DiskSyncStatus, Error, void>({
    mutationFn: async (): Promise<DiskSyncStatus> =>
      unwrap(await client.POST("/api/disk-sync/stop"), "Failed to stop the sync"),
    onSuccess: (s) => qc.setQueryData(DISK_SYNC_STATUS_KEY, s),
  });
}
