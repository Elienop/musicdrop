import { useQuery } from "@tanstack/react-query";

import { client } from "@/api/client";
import type { components } from "@/api/schema";

/** Response of `GET /api/acquisition/status` (generated contract). */
export type AcquisitionQueueStatus =
  components["schemas"]["AcquisitionQueueStatus"];

/** The "no queue / idle" fallback returned when the probe fails or is empty.
 *
 * Mirrors the backend's lifespan-less fallback (an idle queue with zeroed
 * counters), so a transient backend hiccup reads as "nothing to report" rather
 * than throwing a red banner under a status line. */
const IDLE_STATUS: AcquisitionQueueStatus = {
  phase: "idle",
  queued: 0,
  current: null,
  processed: 0,
  set_aside: 0,
  failed: 0,
  error: null,
};

/** Faster cadence while the queue is draining a download, so the activity line
 * tracks an in-flight unattended import without being chatty. */
const RUNNING_INTERVAL_MS = 5_000;

/** Quiet cadence while the queue is idle. The counters are process-lifetime
 * totals that only change when a drop is drained, so an idle Settings tab need
 * not poll hard — but it must keep polling so the durable set-aside/failed
 * totals appear after a background inbox import the user never saw start. */
const IDLE_INTERVAL_MS = 30_000;

/**
 * Poll the acquisition-queue status probe (`GET /api/acquisition/status`).
 *
 * This is the DURABLE surface for the unattended-import outcome: an inbox
 * import is webhook-triggered in the background and the transient active-import
 * banner almost always misses it, but these process-lifetime counters
 * (`set_aside` / `failed` / last `error`) persist after the import finishes, so
 * the user learns a manual pass is needed regardless of whether they witnessed
 * the run.
 *
 * The probe never throws: a hiccup on an informational status line is not worth
 * a red banner. The fallback is the idle queue, matching the backend's own
 * lifespan-less fallback.
 */
export function useAcquisitionStatus() {
  return useQuery<AcquisitionQueueStatus>({
    queryKey: ["acquisition-status"],
    queryFn: async () => {
      const { data, response } = await client.GET("/api/acquisition/status");
      if (!response.ok || !data) {
        return IDLE_STATUS;
      }
      return data;
    },
    refetchInterval: (query) =>
      query.state.data?.phase === "running"
        ? RUNNING_INTERVAL_MS
        : IDLE_INTERVAL_MS,
  });
}
