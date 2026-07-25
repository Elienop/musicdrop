import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { client } from "@/api/client";
import { unwrap } from "@/api/lib";
import type { components } from "@/api/schema";

export type LyricsCoverage = components["schemas"]["LyricsCoverage"];
export type LyricsBackfillStatus = components["schemas"]["LyricsBackfillStatus"];

/** Library lyrics coverage (refetched after a backfill completes). */
export function useLyricsCoverage() {
  return useQuery({
    queryKey: ["lyrics", "coverage"],
    queryFn: async (): Promise<LyricsCoverage> =>
      unwrap(await client.GET("/api/lyrics/coverage"), "Failed to load coverage"),
    retry: false,
  });
}

/** Poll the backfill job. Polls fast while running, otherwise infrequently. */
export function useLyricsBackfillStatus() {
  const queryClient = useQueryClient();
  return useQuery({
    queryKey: ["lyrics", "backfill"],
    queryFn: async (): Promise<LyricsBackfillStatus> => {
      const { data, response } = await client.GET("/api/lyrics/backfill");
      if (!response.ok || !data) {
        // Never fabricate `idle` over a live snapshot: a probe hiccup (a 502/504
        // from a proxy while the job hammers the NAS) would overwrite a
        // "running" status, flip refetchInterval to false, and stop the poll for
        // good — hiding a job still running. Keep the last-known status so the
        // running poll self-heals; fall back to idle only on a cold first load.
        return (
          queryClient.getQueryData<LyricsBackfillStatus>(["lyrics", "backfill"]) ?? {
            phase: "idle", job_id: null, total: 0, processed: 0, found: 0,
            not_found: 0, failed: 0, skipped: 0, current: null,
            writes_enabled: false, error: null, album_id: null, scope_label: "library",
          }
        );
      }
      return data;
    },
    // Only poll while a job is actually running. The start mutation invalidates
    // ["lyrics","backfill"], so a backfill kicked off elsewhere still wakes the poll.
    refetchInterval: (query) => (query.state.data?.phase === "running" ? 1000 : false),
  });
}

export function useStartLyricsBackfill() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (vars?: { recheckMisses?: boolean }): Promise<LyricsBackfillStatus> => {
      const { data, response } = await client.POST("/api/lyrics/backfill", {
        params: vars?.recheckMisses ? { query: { recheck_misses: true } } : {},
      });
      if (!response.ok || !data) {
        throw new Error(response.status === 409 ? "A library operation is in progress" : "Backfill failed to start");
      }
      return data;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["lyrics", "backfill"] });
    },
  });
}

export function useStopLyricsBackfill() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (): Promise<LyricsBackfillStatus> =>
      unwrap(
        await client.POST("/api/lyrics/backfill/stop"),
        "Failed to stop backfill",
      ),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["lyrics", "backfill"] });
      // A stop is a terminal transition — the % a backfill already wrote is stale.
      void queryClient.invalidateQueries({ queryKey: ["lyrics", "coverage"] });
    },
  });
}
