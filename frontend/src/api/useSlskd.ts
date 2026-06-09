import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { client } from "@/api/client";
import type { components } from "@/api/schema";

export type SlskdSettings = components["schemas"]["SlskdSettings"];
export type SlskdConnection = components["schemas"]["SlskdConnection"];
export type SlskdSettingsUpdate = components["schemas"]["SlskdSettingsUpdate"];
export type ReviewInboxResponse =
  components["schemas"]["ReviewInboxResponse"];

async function fetchSlskdSettings(): Promise<SlskdSettings> {
  const { data, error, response } = await client.GET("/api/slskd/settings");
  if (error || !response.ok || !data) throw new Error("Failed to load slskd settings");
  return data;
}

export function useSlskdSettings() {
  return useQuery({ queryKey: ["slskd", "settings"], queryFn: fetchSlskdSettings });
}

export function useSaveSlskdSettings() {
  const qc = useQueryClient();
  return useMutation<SlskdSettings, Error, SlskdSettingsUpdate>({
    mutationFn: async (body) => {
      const { data, error, response } = await client.PUT("/api/slskd/settings", { body });
      if (error || !response.ok || !data) throw new Error("Save failed");
      return data;
    },
    onSettled: () => void qc.invalidateQueries({ queryKey: ["slskd", "settings"] }),
  });
}

export function useTestSlskd() {
  return useMutation<SlskdConnection, Error, void>({
    mutationFn: async () => {
      const { data, error, response } = await client.POST("/api/slskd/test");
      if (error || !response.ok || !data) throw new Error("Test failed");
      return data;
    },
  });
}

/** Start an attended import of the fixed slskd inbox (one-click set-aside review).
 *
 * The server resolves the inbox path — none is sent from the browser. A
 * non-empty inbox returns `started: true` with the `job_id` to navigate to; an
 * empty inbox returns `started: false` (a no-op, not an error). A 409 (an import
 * already running) surfaces as a thrown Error so the caller can show a retry. */
export function useReviewInbox() {
  const qc = useQueryClient();
  return useMutation<ReviewInboxResponse, Error, void>({
    mutationFn: async () => {
      const { data, error, response } = await client.POST(
        "/api/acquisition/review-inbox",
      );
      if (error || !response.ok || !data)
        throw new Error("Failed to start inbox review");
      return data;
    },
    // Refresh the backlog + the import gate whatever the outcome (a start changed
    // the inbox; an empty result / 409 should re-sync the list and the gate).
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: ["inbox-items"] });
      void qc.invalidateQueries({ queryKey: ["active-import"] });
    },
  });
}
