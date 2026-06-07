import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { client } from "@/api/client";
import type { components } from "@/api/schema";

export type PlexSettings = components["schemas"]["PlexSettings"];
export type PlexConnection = components["schemas"]["PlexConnection"];
export type PlexUserList = components["schemas"]["PlexUserList"];

export function usePlexUsers() {
  return useQuery({
    queryKey: ["plex", "users"],
    queryFn: async (): Promise<PlexUserList> => {
      const { data, error, response } = await client.GET("/api/plex/users");
      // Tag the error with the HTTP status so the picker can tell the "Plex not
      // configured" 409 (point at Settings) apart from a generic load failure
      // (offer a Retry).
      if (error || !response.ok || !data) {
        throw Object.assign(new Error("Failed to load Plex users"), {
          status: response.status,
        });
      }
      return data;
    },
    retry: false, // a 409 (Plex not configured) shouldn't be retried, and a
    // generic failure shouldn't silently loop — the picker offers Retry.
  });
}

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
