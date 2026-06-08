import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { client } from "@/api/client";
import type { components } from "@/api/schema";

export type SlskdSettings = components["schemas"]["SlskdSettings"];
export type SlskdConnection = components["schemas"]["SlskdConnection"];
export type SlskdSettingsUpdate = components["schemas"]["SlskdSettingsUpdate"];

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
