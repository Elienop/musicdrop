import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { client } from "@/api/client";
import { unwrap } from "@/api/lib";
import type { components } from "@/api/schema";

export type ArtistArtWriteSettings = components["schemas"]["ArtistArtWriteSettings"];
export type ArtistArtBackfillStatus = components["schemas"]["ArtistArtBackfillStatus"];

export const ARTIST_ART_SETTINGS_KEY = ["artist-art", "settings"] as const;
const STATUS_KEY = ["artist-art", "backfill"] as const;

export function useArtistArtSettings() {
  return useQuery({
    queryKey: ARTIST_ART_SETTINGS_KEY,
    queryFn: async (): Promise<ArtistArtWriteSettings> =>
      unwrap(
        await client.GET("/api/artists/art/settings"),
        "Failed to load artist-art settings",
      ),
  });
}

export function useSetArtistArtSettings() {
  const qc = useQueryClient();
  return useMutation<ArtistArtWriteSettings, Error, boolean>({
    mutationFn: async (enabled) =>
      unwrap(
        await client.PUT("/api/artists/art/settings", { body: { enabled } }),
        "Failed to update artist-art settings",
      ),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ARTIST_ART_SETTINGS_KEY }),
  });
}

export function useArtistArtBackfillStatus() {
  return useQuery({
    queryKey: STATUS_KEY,
    queryFn: async (): Promise<ArtistArtBackfillStatus> =>
      unwrap(
        await client.GET("/api/artists/art/backfill"),
        "Failed to load artist-art status",
      ),
    refetchInterval: (q) => (q.state.data?.phase === "running" ? 1000 : false),
  });
}

export function useStartArtistArtApply(name: string) {
  const qc = useQueryClient();
  return useMutation<ArtistArtBackfillStatus, Error, void>({
    mutationFn: async () => {
      const { data, error, response } = await client.POST("/api/artists/art/apply", {
        params: { query: { name } },
      });
      if (response.status === 403) throw new Error("Enable 'Write artist art to library' first");
      if (response.status === 409) throw new Error("A library operation is in progress");
      if (error || !data) throw new Error("Failed to start");
      return data;
    },
    onSuccess: () => void qc.invalidateQueries({ queryKey: STATUS_KEY }),
  });
}

export function useStartArtistArtBackfill() {
  const qc = useQueryClient();
  return useMutation<ArtistArtBackfillStatus, Error, void>({
    mutationFn: async () => {
      const { data, error, response } = await client.POST("/api/artists/art/backfill");
      if (response.status === 403) throw new Error("Enable 'Write artist art to library' first");
      if (response.status === 409) throw new Error("A library operation is in progress");
      if (error || !data) throw new Error("Failed to start backfill");
      return data;
    },
    onSuccess: () => void qc.invalidateQueries({ queryKey: STATUS_KEY }),
  });
}

export function useStopArtistArtBackfill() {
  const qc = useQueryClient();
  return useMutation<ArtistArtBackfillStatus, Error, void>({
    mutationFn: async () =>
      unwrap(
        await client.POST("/api/artists/art/backfill/stop"),
        "Failed to stop",
      ),
    onSuccess: () => void qc.invalidateQueries({ queryKey: STATUS_KEY }),
  });
}
