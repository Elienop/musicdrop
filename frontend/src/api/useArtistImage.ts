import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { client } from "@/api/client";
import { apiUrl, errorDetail, unwrap } from "@/api/lib";
import type { components } from "@/api/schema";

export type ArtistImageSettings = components["schemas"]["ArtistImageSettings"];

/** Query key every ArtistImage + the Settings panel share, so flipping the
 * toggle and invalidating this one key re-evaluates every instance. */
export const ARTIST_IMAGE_SETTINGS_KEY = ["artist-image", "settings"] as const;

/** Fallback when an override request fails without a usable `detail` body. */
const OVERRIDE_ERROR = "Couldn’t update the artist image";

async function fetchSettings(): Promise<ArtistImageSettings> {
  return unwrap(
    await client.GET("/api/artists/image/settings"),
    "Failed to load artist-image settings",
  );
}

export function useArtistImageSettings() {
  return useQuery({ queryKey: ARTIST_IMAGE_SETTINGS_KEY, queryFn: fetchSettings });
}

export function useSetArtistImageSettings() {
  const queryClient = useQueryClient();
  return useMutation<ArtistImageSettings, Error, boolean>({
    mutationFn: async (enabled) =>
      unwrap(
        await client.PUT("/api/artists/image/settings", { body: { enabled } }),
        "Failed to update artist-image settings",
      ),
    onSuccess: () => {
      // Every ArtistImage consults this query, so they all re-evaluate enabled.
      void queryClient.invalidateQueries({ queryKey: ARTIST_IMAGE_SETTINGS_KEY });
    },
  });
}

function overrideUrl(name: string): string {
  return apiUrl(`/api/artists/image/override?name=${encodeURIComponent(name)}`);
}

function fromUrlOverrideUrl(name: string): string {
  return apiUrl(`/api/artists/image/override/from-url?name=${encodeURIComponent(name)}`);
}

/** Upload a custom portrait for `name` (multipart, field "file"). The header
 * image refresh is driven by the caller bumping ArtistImage's `version`. */
export function useUploadArtistImageOverride(name: string) {
  return useMutation<void, Error, Blob>({
    mutationFn: async (image) => {
      const form = new FormData();
      form.append("file", image, "artist-image");
      const res = await fetch(overrideUrl(name), { method: "POST", body: form });
      if (!res.ok) throw new Error(await errorDetail(res, OVERRIDE_ERROR));
    },
  });
}

/** Reset `name` back to the automatic image (clears the override slot). */
export function useResetArtistImageOverride(name: string) {
  return useMutation<void, Error, void>({
    mutationFn: async () => {
      const res = await fetch(overrideUrl(name), { method: "DELETE" });
      if (!res.ok) throw new Error(await errorDetail(res, OVERRIDE_ERROR));
    },
  });
}

/** Set `name`'s portrait from an image URL — the server fetches + stores the bytes. */
export function useSetArtistImageFromUrl(name: string) {
  return useMutation<void, Error, string>({
    mutationFn: async (url) => {
      const res = await fetch(fromUrlOverrideUrl(name), {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ url }),
      });
      if (!res.ok) throw new Error(await errorDetail(res, OVERRIDE_ERROR));
    },
  });
}
