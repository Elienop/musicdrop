import { useQuery } from "@tanstack/react-query";

import { client } from "@/api/client";
import type { components } from "@/api/schema";

/** Response of `GET /api/imports/active` (generated contract). */
export type ActiveImportStatus = components["schemas"]["ActiveImportStatus"];

/** Default cadence — quiet polling while no import is running.
 *
 * 30s is long enough that an idle Settings tab is essentially free; the live
 * cadence kicks in only once an import actually starts (then the Apply button
 * needs to flip back the moment it ends, so 5s wins over 30s).
 */
const IDLE_INTERVAL_MS = 30_000;

/** Faster cadence while an import is running, so the Apply button re-enables
 * within a few seconds of the worker finishing. Matches the import-page poll
 * interval used in {@link useImport} (`IMPORT_POLL_MS = 1000`) order-of-magnitude
 * without being chatty. */
const ACTIVE_INTERVAL_MS = 5_000;

/**
 * Poll the import-active probe (`GET /api/imports/active`).
 *
 * The SettingsPage's Apply button is the only caller: a Save leaves
 * `apply_pending = true`, then Apply does the in-process beets reload — but
 * that reload 409s if an import is in flight, so the page must gate the click
 * proactively. The cadence is adaptive: 5s while an import runs (so Apply
 * re-enables promptly after the import finishes), 30s otherwise (so an idle
 * Settings tab is essentially free).
 *
 * The probe deliberately *never throws*: a transient backend hiccup on a
 * probe used only to disable a button is not worth a red banner under that
 * button. The fallback is `{ active: false }`, matching the existing UX where
 * a stale-false followed by a 409 click is already handled by the Apply
 * mutation's structured error path.
 */
export function useActiveImport() {
  return useQuery<ActiveImportStatus>({
    queryKey: ["imports-active"],
    queryFn: async () => {
      const { data, response } = await client.GET("/api/imports/active");
      // Treat a non-2xx as "not active" rather than throwing — see docblock.
      if (!response.ok || !data) {
        return { active: false };
      }
      return data;
    },
    refetchInterval: (query) =>
      query.state.data?.active ? ACTIVE_INTERVAL_MS : IDLE_INTERVAL_MS,
    // Keep the probe value warm so a re-mount of the Apply button section
    // (e.g. an unrelated re-render) doesn't briefly show the wrong gate.
    staleTime: ACTIVE_INTERVAL_MS,
  });
}
