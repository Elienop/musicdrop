import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { client } from "@/api/client";
import { apiUrl, errorDetail, unwrap } from "@/api/lib";
import type { components } from "@/api/schema";

export type ArtistImageSettings = components["schemas"]["ArtistImageSettings"];
export type ArtistImageSourceList = components["schemas"]["ArtistImageSourceList"];
export type ArtistImageSourceOption = components["schemas"]["ArtistImageSourceOption"];
export type ArtistImageSourceId = ArtistImageSourceOption["id"];
export type ArtistImageResetResult = components["schemas"]["ArtistImageResetResult"];

/** Query key every ArtistImage + the Settings panel share, so flipping the
 * toggle and invalidating this one key re-evaluates every instance. */
export const ARTIST_IMAGE_SETTINGS_KEY = ["artist-image", "settings"] as const;

/** Fallback when an override request fails without a usable `detail` body. */
const OVERRIDE_ERROR = "Couldn’t update the artist image";

/** Fallback for the preview fetch, which stores nothing — so "update" would be
 * the wrong word for what just failed. */
const FETCH_ERROR = "Couldn’t fetch an image from that source";

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

/** Which sources can be fetched for THIS artist, in the chain's own fallback
 * order. Per-artist, not per-install: fanart.tv is MusicBrainz-keyed, so it can
 * be configured yet come back `available: false` with a `reason` here. Sources
 * with no credentials are omitted entirely.
 *
 * This endpoint is gated on NOTHING server-side, so it answers even when artist
 * images are switched off — see `useFetchArtistImage` for why that matters.
 *
 * `name` carries no guard here and the server declares `min_length=1`, so pass
 * `enabled={false}` rather than an empty name; an empty one 422s. */
export function useArtistImageSources(name: string, enabled = true) {
  return useQuery({
    queryKey: ["artist-image", "sources", name],
    enabled,
    queryFn: async (): Promise<ArtistImageSourceList> =>
      unwrap(
        await client.GET("/api/artists/image/sources", { params: { query: { name } } }),
        "Couldn’t load the image sources",
      ),
  });
}

export type FetchedArtistImage =
  | { found: false; reason: string }
  | { found: true; blob: Blob; objectUrl: string; source: string | null };

/** Fetch a candidate portrait from ONE source. Preview only — the server stores
 * nothing, and installing re-posts THESE bytes through the upload hook, so the
 * image the user approved is the image that lands. Mirrors useFetchAlbumCover.
 *
 * The caller owns `objectUrl` and must revoke it.
 *
 * Two things this hook cannot learn from the generated types:
 * - "Artist images are on" is not one question. `useArtistImageSettings`
 *   reports the IMAGE toggle alone, while this route accepts when EITHER that
 *   or the write-to-library toggle is on (main.py composes the predicate). So
 *   gate a fetch affordance on `imagesEnabled || writeEnabled` — the same pair
 *   `ArtistImage` composes to decide whether to attempt a portrait at all, and
 *   the pair `ArtistAlbumsPage` already holds. `settings.enabled` alone hides a
 *   path that works. No endpoint reports the composed answer, so this stays a
 *   client-side mirror of the backend's `or`; a toggle flipped in another tab
 *   mid-session still lands on the 403 below.
 * - The 403's cause is invisible in the schema: an `Origin`-guard dependency
 *   emits no OpenAPI security scheme. Only the OpenAPI *description* names both
 *   causes — at runtime each raises its own one-cause sentence ("Turn on artist
 *   images first" / "cross-origin request rejected"). Whichever fired, the
 *   server's sentence names it, so show that rather than guessing.
 *
 * `source` rides in the query string as the generated Literal, so an id outside
 * the three the backend knows cannot be sent. */
export function useFetchArtistImage(name: string) {
  return useMutation<FetchedArtistImage, Error, ArtistImageSourceId>({
    mutationFn: async (source) => {
      const res = await fetch(
        apiUrl(
          `/api/artists/image/fetch?name=${encodeURIComponent(name)}&source=${encodeURIComponent(source)}`,
        ),
        { method: "POST" },
      );
      // 404 is a real answer ("that source has nothing for this artist"); every
      // other failure — notably the 502 a source outage produces — is an error,
      // so an outage never reads as "no image exists".
      if (res.status === 404) {
        return { found: false, reason: await errorDetail(res, "That source had no portrait.") };
      }
      if (!res.ok) throw new Error(await errorDetail(res, FETCH_ERROR));
      const blob = await res.blob();
      return {
        found: true,
        blob,
        objectUrl: URL.createObjectURL(blob),
        // Nullable for real: the backend sets no CORS `expose_headers`, so this
        // reads only while the request is same-origin — true in prod, and true
        // in dev ONLY because Vite proxies /api. Point the app at the backend's
        // own origin and the provenance silently goes null.
        source: res.headers.get("X-Art-Source"),
      };
    },
  });
}

/** Forget every stored portrait for `name` — the upload AND the cached
 * automatic one — so the next serve looks it up again. Clearing only the
 * override drops the user back onto the automatic image they just rejected.
 *
 * The result reports each slot, but the two booleans are NOT a success signal:
 * a cache dir that refuses the unlink still drops the in-memory entry, so
 * `false, false` can mean "nothing was stored" or "the files stayed but what is
 * served changed". Never branch on them to say nothing happened. */
export function useResetArtistImage(name: string) {
  return useMutation<ArtistImageResetResult, Error, void>({
    mutationFn: async () => {
      const res = await fetch(apiUrl(`/api/artists/image/reset?name=${encodeURIComponent(name)}`), {
        method: "POST",
      });
      if (!res.ok) throw new Error(await errorDetail(res, OVERRIDE_ERROR));
      return (await res.json()) as ArtistImageResetResult;
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
