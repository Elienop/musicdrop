// frontend/src/api/useArtistImage.ts
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { client } from "@/api/client";
import type { components } from "@/api/schema";

export type ArtistImageSettings = components["schemas"]["ArtistImageSettings"];

/** Query key every ArtistImage + the Settings panel share, so flipping the
 * toggle and invalidating this one key re-evaluates every instance. */
export const ARTIST_IMAGE_SETTINGS_KEY = ["artist-image", "settings"] as const;

function apiUrl(path: string): string {
  const origin = typeof window === "undefined" ? "" : window.location.origin;
  return `${origin}${path}`;
}

async function errorDetail(res: Response): Promise<string> {
  try {
    const body: unknown = await res.json();
    if (
      body !== null &&
      typeof body === "object" &&
      "detail" in body &&
      typeof (body as { detail: unknown }).detail === "string"
    ) {
      return (body as { detail: string }).detail;
    }
  } catch {
    // Non-JSON body — fall through to the generic message.
  }
  return "Couldn’t update the artist image";
}

async function fetchSettings(): Promise<ArtistImageSettings> {
  const { data, error } = await client.GET("/api/artists/image/settings");
  if (error || !data) throw new Error("Failed to load artist-image settings");
  return data;
}

export function useArtistImageSettings() {
  return useQuery({ queryKey: ARTIST_IMAGE_SETTINGS_KEY, queryFn: fetchSettings });
}

export function useSetArtistImageSettings() {
  const queryClient = useQueryClient();
  return useMutation<ArtistImageSettings, Error, boolean>({
    mutationFn: async (enabled) => {
      const { data, error } = await client.PUT("/api/artists/image/settings", {
        body: { enabled },
      });
      if (error || !data) throw new Error("Failed to update artist-image settings");
      return data;
    },
    onSuccess: () => {
      // Every ArtistImage consults this query, so they all re-evaluate enabled.
      void queryClient.invalidateQueries({ queryKey: ARTIST_IMAGE_SETTINGS_KEY });
    },
  });
}

function overrideUrl(name: string): string {
  return apiUrl(`/api/artists/image/override?name=${encodeURIComponent(name)}`);
}

/** Upload a custom portrait for `name` (multipart, field "file"). The header
 * image refresh is driven by the caller bumping ArtistImage's `version`. */
export function useUploadArtistImageOverride(name: string) {
  return useMutation<void, Error, Blob>({
    mutationFn: async (image) => {
      const form = new FormData();
      form.append("file", image, "artist-image");
      const res = await fetch(overrideUrl(name), { method: "POST", body: form });
      if (!res.ok) throw new Error(await errorDetail(res));
    },
  });
}

/** Reset `name` back to the automatic image (clears the override slot). */
export function useResetArtistImageOverride(name: string) {
  return useMutation<void, Error, void>({
    mutationFn: async () => {
      const res = await fetch(overrideUrl(name), { method: "DELETE" });
      if (!res.ok) throw new Error(await errorDetail(res));
    },
  });
}
