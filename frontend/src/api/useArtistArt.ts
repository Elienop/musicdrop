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

/** Bound on the per-artist start request. The endpoint only queues the job —
 * the writing happens after it answers — so a reply that takes longer than
 * this says the transport is stuck, not that the work is slow.
 *
 * It exists because the confirm dialog disables Cancel and swallows Escape
 * while the start is pending (`ArtistArtStatus` in
 * pages/artists/ArtistAlbumsPage.tsx): with no bound, a request that never
 * answers leaves a dialog no key or click can close. */
const START_TIMEOUT_MS = 10_000;

/** Run `send` with a signal that aborts after `START_TIMEOUT_MS`, reporting a
 * timed-out request as a short message instead of the transport's own.
 *
 * An explicit controller rather than `AbortSignal.timeout`, for two measured
 * reasons (both checked under this project's jsdom + vitest 4.1.11 setup):
 * `AbortSignal.timeout` is armed off a timer vitest's fake clock does not
 * replace, so a test could only prove the bound by waiting out the real 10s;
 * and its abort reason arrives from Node's realm, where `instanceof
 * DOMException` is false against jsdom's global. Reading `signal.aborted`
 * holds in both realms and under either clock. */
async function withStartTimeout<T>(send: (signal: AbortSignal) => Promise<T>): Promise<T> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), START_TIMEOUT_MS);
  try {
    return await send(controller.signal);
  } catch (error) {
    if (controller.signal.aborted) throw new Error("The request timed out.");
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

export function useStartArtistArtApply(name: string) {
  const qc = useQueryClient();
  return useMutation<ArtistArtBackfillStatus, Error, void>({
    mutationFn: async () =>
      withStartTimeout(async (signal) => {
        const { data, error, response } = await client.POST("/api/artists/art/apply", {
          params: { query: { name } },
          signal,
        });
        if (response.status === 403) throw new Error("Enable 'Write artist art to library' first");
        if (response.status === 409) throw new Error("A library operation is in progress");
        if (error || !data) throw new Error("Failed to start");
        return data;
      }),
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
