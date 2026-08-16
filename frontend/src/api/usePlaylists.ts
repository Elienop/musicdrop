import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { client } from "@/api/client";
import { apiUrl, errorDetail, unwrap } from "@/api/lib";
import type { components } from "@/api/schema";

export type Playlist = components["schemas"]["Playlist"];
export type PlaylistDetail = components["schemas"]["PlaylistDetail"];
export type PlaylistTrack = components["schemas"]["PlaylistTrack"];
export type PlaylistMergeResult = components["schemas"]["PlaylistMergeResponse"];

async function fetchPlaylists(): Promise<Playlist[]> {
  return unwrap(await client.GET("/api/playlists"), "Failed to load playlists");
}

/** All playlists (summaries).
 *
 * `enabled` lets a caller that is mounted before it is visible - the merge
 * dialog, which exists while closed - hold the request back. Several pages'
 * tests serve no `/api/playlists` handler, so an unwanted list fetch is a
 * failure there, not just waste.
 */
export function usePlaylists({ enabled = true }: { enabled?: boolean } = {}) {
  return useQuery({
    queryKey: ["playlists"],
    queryFn: fetchPlaylists,
    staleTime: 30_000,
    enabled,
  });
}

async function fetchPlaylist(id: string): Promise<PlaylistDetail> {
  return unwrap(
    await client.GET("/api/playlists/{playlist_id}", {
      params: { path: { playlist_id: id } },
    }),
    "Failed to load playlist",
  );
}

/** A single playlist with its resolved tracklist. */
export function usePlaylist(id: string) {
  return useQuery({
    queryKey: ["playlist", id],
    queryFn: () => fetchPlaylist(id),
    enabled: id.length > 0,
  });
}

export function useCreatePlaylist() {
  const queryClient = useQueryClient();
  return useMutation<Playlist, Error, { name: string; description?: string }>({
    mutationFn: async (body) =>
      unwrap(
        await client.POST("/api/playlists", {
          body: { name: body.name, description: body.description ?? "" },
        }),
        "Create failed",
      ),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["playlists"] });
    },
  });
}

export function useRenamePlaylist(id: string) {
  const queryClient = useQueryClient();
  return useMutation<Playlist, Error, { name: string }>({
    mutationFn: async (body) =>
      unwrap(
        await client.PATCH("/api/playlists/{playlist_id}", {
          params: { path: { playlist_id: id } },
          body,
        }),
        "Rename failed",
      ),
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ["playlist", id] });
      void queryClient.invalidateQueries({ queryKey: ["playlists"] });
    },
  });
}

export function useSetTargets(id: string) {
  const queryClient = useQueryClient();
  return useMutation<Playlist, Error, string[]>({
    mutationFn: async (targetPlexUsers) =>
      unwrap(
        await client.PATCH("/api/playlists/{playlist_id}", {
          params: { path: { playlist_id: id } },
          body: { target_plex_users: targetPlexUsers },
        }),
        "Failed to save targets",
      ),
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ["playlist", id] });
      void queryClient.invalidateQueries({ queryKey: ["playlists"] });
    },
  });
}

export function useDeletePlaylist() {
  const queryClient = useQueryClient();
  return useMutation<void, Error, string>({
    mutationFn: async (id) => {
      const { error, response } = await client.DELETE("/api/playlists/{playlist_id}", {
        params: { path: { playlist_id: id } },
      });
      if (error || !response.ok) throw new Error("Delete failed");
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["playlists"] });
    },
  });
}

export function useAddTracks() {
  const queryClient = useQueryClient();
  return useMutation<PlaylistDetail, Error, { playlistId: string; trackIds: number[] }>({
    mutationFn: async ({ playlistId, trackIds }) =>
      unwrap(
        await client.POST("/api/playlists/{playlist_id}/tracks", {
          params: { path: { playlist_id: playlistId } },
          body: { track_ids: trackIds },
        }),
        "Add failed",
      ),
    onSettled: (_data, _err, vars) => {
      void queryClient.invalidateQueries({ queryKey: ["playlist", vars.playlistId] });
      void queryClient.invalidateQueries({ queryKey: ["playlists"] });
    },
  });
}

/** Remove one entry (resolved or pending) by its stable per-slot uid. */
export function useRemoveEntry(id: string) {
  const queryClient = useQueryClient();
  return useMutation<PlaylistDetail, Error, string>({
    mutationFn: async (entryUid) =>
      unwrap(
        await client.DELETE("/api/playlists/{playlist_id}/entries/{entry_uid}", {
          params: { path: { playlist_id: id, entry_uid: entryUid } },
        }),
        "Remove failed",
      ),
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ["playlist", id] });
      void queryClient.invalidateQueries({ queryKey: ["playlists"] });
    },
  });
}

/** Resolve (or re-point) one entry to a library track, keeping its position. */
export function useResolveEntry(id: string) {
  const queryClient = useQueryClient();
  return useMutation<PlaylistDetail, Error, { entryUid: string; itemId: number }>({
    mutationFn: async ({ entryUid, itemId }) =>
      unwrap(
        await client.PATCH("/api/playlists/{playlist_id}/entries/{entry_uid}", {
          params: { path: { playlist_id: id, entry_uid: entryUid } },
          body: { item_id: itemId },
        }),
        "Failed to match the track",
      ),
    onSuccess: (detail) => {
      queryClient.setQueryData(["playlist", id], detail);
      void queryClient.invalidateQueries({ queryKey: ["playlists"] });
    },
  });
}

export function useSyncPlaylist(id: string) {
  const queryClient = useQueryClient();
  return useMutation<PlaylistDetail, Error, void>({
    mutationFn: async () => {
      const { data, error, response } = await client.POST(
        "/api/playlists/{playlist_id}/sync",
        { params: { path: { playlist_id: id } } },
      );
      // Tag the error with the HTTP status so the detail page can tell the
      // "Plex not connected" 409 apart from a generic failure.
      if (error || !response.ok || !data) {
        throw Object.assign(new Error("Sync failed"), { status: response.status });
      }
      return data;
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ["playlist", id] });
      void queryClient.invalidateQueries({ queryKey: ["playlists"] });
    },
  });
}

/** Upload raw JPEG/PNG bytes as this playlist's custom cover. The artwork PUT
 * carries a raw image body (no request model), so it bypasses the typed
 * openapi-fetch client and hits `fetch` directly — the same shape the album
 * cover install uses. Surfaces the server's message so the panel can show the
 * real reason (415 unsupported type, 413 too large). Invalidating the playlist
 * + playlists queries re-renders the header cover via Task 5's `?v=<hash>` and
 * refreshes the list thumbnail. */
export function useUploadPlaylistArtwork(id: string) {
  const queryClient = useQueryClient();
  return useMutation<Playlist, Error, Blob>({
    mutationFn: async (image) => {
      const res = await fetch(apiUrl(`/api/playlists/${id}/artwork`), {
        method: "PUT",
        body: image,
      });
      if (!res.ok) throw new Error(await errorDetail(res, "Couldn’t save the artwork."));
      return (await res.json()) as Playlist;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["playlist", id] });
      void queryClient.invalidateQueries({ queryKey: ["playlists"] });
    },
  });
}

/** Remove the playlist's custom cover (204). On success the header falls back
 * to the album-cover collage — same cache invalidation as the upload. */
export function useDeletePlaylistArtwork(id: string) {
  const queryClient = useQueryClient();
  return useMutation<void, Error, void>({
    mutationFn: async () => {
      const res = await fetch(apiUrl(`/api/playlists/${id}/artwork`), { method: "DELETE" });
      if (!res.ok) throw new Error("Couldn’t remove the artwork.");
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["playlist", id] });
      void queryClient.invalidateQueries({ queryKey: ["playlists"] });
    },
  });
}

export function useReorderTracks(id: string) {
  const queryClient = useQueryClient();
  return useMutation<PlaylistDetail, Error, string[]>({
    mutationFn: async (entryUids) =>
      unwrap(
        await client.PUT("/api/playlists/{playlist_id}/tracks", {
          params: { path: { playlist_id: id } },
          body: { entry_uids: entryUids },
        }),
        "Reorder failed",
      ),
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ["playlist", id] });
    },
  });
}

/** Fold another playlist into `id`. The response carries the rewritten detail
 * plus the server's own `added` / `skipped_duplicates` counts, so the caller
 * states the outcome without recounting. Merge rewrites the whole list in one
 * round trip, so there is no optimistic path: the response IS the new state.
 *
 * The cache seed lives in the HOOK-level onSuccess on purpose - TanStack skips
 * a per-call onSuccess when its observer has unmounted but always runs this
 * one, so the detail cache cannot be left stale by a dialog that closed early.
 */
export function useMergePlaylist(id: string) {
  const queryClient = useQueryClient();
  return useMutation<PlaylistMergeResult, Error, { sourceId: string; deleteSource: boolean }>({
    mutationFn: async ({ sourceId, deleteSource }) =>
      unwrap(
        await client.POST("/api/playlists/{playlist_id}/merge", {
          params: { path: { playlist_id: id } },
          body: { source_id: sourceId, delete_source: deleteSource },
        }),
        "Merge failed",
      ),
    onSuccess: (result, vars) => {
      queryClient.setQueryData(["playlist", id], result.playlist);
      // The source is either gone or unchanged; either way its cached detail
      // and the list's counts are now wrong.
      void queryClient.invalidateQueries({ queryKey: ["playlist", vars.sourceId] });
      void queryClient.invalidateQueries({ queryKey: ["playlists"] });
    },
  });
}
