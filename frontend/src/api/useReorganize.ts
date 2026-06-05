// frontend/src/api/useReorganize.ts
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import type { components } from "./schema";
import { client } from "./client";

export type ReorganizePlan = components["schemas"]["ReorganizePlan"];
export type ReorganizeMove = components["schemas"]["ReorganizeMove"];
export type ReorganizeBackfillStatus = components["schemas"]["ReorganizeBackfillStatus"];

export const REORGANIZE_STATUS_KEY = ["reorganize", "status"] as const;

export type ReorganizeScope =
  | { scope: "library" }
  | { scope: "artist"; artist: string }
  | { scope: "album"; albumId: number };

export function useReorganizeStatus() {
  return useQuery({
    queryKey: REORGANIZE_STATUS_KEY,
    queryFn: async (): Promise<ReorganizeBackfillStatus> => {
      const { data, response } = await client.GET("/api/reorganize/status");
      if (!response.ok || !data) {
        return {
          phase: "idle", job_id: null, scope: null, total: 0, processed: 0, moved: 0,
          skipped: 0, failed: 0, current: null, error: null, artist: null,
          album_id: null, scope_label: "library",
        };
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
    mutationFn: async (): Promise<ReorganizeBackfillStatus> => {
      const { data, response } = await client.POST("/api/reorganize/stop");
      if (!response.ok || !data) throw new Error("Failed to stop");
      return data;
    },
    onSuccess: () => void qc.invalidateQueries({ queryKey: REORGANIZE_STATUS_KEY }),
  });
}
