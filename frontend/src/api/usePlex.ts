import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { client } from "@/api/client";
import type { components } from "@/api/schema";

export type PlexSettings = components["schemas"]["PlexSettings"];
export type PlexConnection = components["schemas"]["PlexConnection"];

async function fetchPlexSettings(): Promise<PlexSettings> {
  const { data, error, response } = await client.GET("/api/plex/settings");
  if (error || !response.ok || !data) throw new Error("Failed to load Plex settings");
  return data;
}

export function usePlexSettings() {
  return useQuery({ queryKey: ["plex", "settings"], queryFn: fetchPlexSettings });
}

export function useSavePlexSettings() {
  const qc = useQueryClient();
  return useMutation<PlexSettings, Error, components["schemas"]["PlexSettingsUpdate"]>({
    mutationFn: async (body) => {
      const { data, error, response } = await client.PUT("/api/plex/settings", { body });
      if (error || !response.ok || !data) throw new Error("Save failed");
      return data;
    },
    onSettled: () => void qc.invalidateQueries({ queryKey: ["plex", "settings"] }),
  });
}

export function useTestPlex() {
  return useMutation<PlexConnection, Error, void>({
    mutationFn: async () => {
      const { data, error, response } = await client.POST("/api/plex/test");
      if (error || !response.ok || !data) throw new Error("Test failed");
      return data;
    },
  });
}
