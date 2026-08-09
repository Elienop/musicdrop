import { useMutation, useQueryClient } from "@tanstack/react-query";
import { apiUrl, errorDetail } from "@/api/lib";
import type { components } from "@/api/schema";

type CoverInstallResult = components["schemas"]["CoverInstallResult"];

export type FetchedCover =
  | { found: false }
  | { found: true; blob: Blob; source: string | null };

/** Fetch a candidate cover for one album. Preview only — the server stores
 * nothing, and installing re-posts THESE bytes through `useInstallAlbumCover`.
 *
 * Hands back the BLOB, never an object URL — the same contract as
 * `useFetchArtistImage`, and for the same reason: two sibling hooks that differ
 * here are how the next person copies the wrong one. TanStack skips a per-call
 * `onSuccess` once the observer has unmounted (`mutationObserver.js` gates
 * `#mutateOptions` on `hasListeners()`), so a URL minted here would be handed
 * to nobody when the caller unmounts mid-fetch: created, unrevokable, leaked.
 * Minting it in the caller's own `onSuccess` means an abandoned fetch creates
 * nothing at all. Revoking from a hook-level `onSuccess` is NOT the fix —
 * `mutation.js` awaits `this.options` callbacks unconditionally, before
 * notifying observers, so that would destroy every preview on the normal path. */
export function useFetchAlbumCover(albumId: number) {
  return useMutation<FetchedCover, Error, void>({
    mutationFn: async () => {
      const res = await fetch(apiUrl(`/api/albums/${albumId}/cover/fetch`), { method: "POST" });
      if (res.status === 404) return { found: false };
      if (!res.ok) throw new Error("Fetch failed");
      const blob = await res.blob();
      return {
        found: true,
        blob,
        source: res.headers.get("X-Art-Source"),
      };
    },
  });
}

export function useInstallAlbumCover(albumId: number) {
  const queryClient = useQueryClient();
  return useMutation<CoverInstallResult, Error, Blob>({
    mutationFn: async (image) => {
      const form = new FormData();
      form.append("file", image, "cover");
      const res = await fetch(apiUrl(`/api/albums/${albumId}/cover`), { method: "POST", body: form });
      if (res.status === 409) throw new Error("An import is running; try again when it finishes.");
      if (!res.ok) throw new Error(await errorDetail(res, "Cover install failed"));
      return (await res.json()) as CoverInstallResult;
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ["album", albumId] });
    },
  });
}
