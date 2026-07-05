import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { client } from "@/api/client";
import type { components } from "@/api/schema";

export type Playlist = components["schemas"]["Playlist"];
export type PlaylistDetail = components["schemas"]["PlaylistDetail"];
export type PlaylistTrack = components["schemas"]["PlaylistTrack"];

async function fetchPlaylists(): Promise<Playlist[]> {
  const { data, error, response } = await client.GET("/api/playlists");
  if (error || !response.ok || !data) throw new Error("Failed to load playlists");
  return data;
}

/** All playlists (summaries). */
export function usePlaylists() {
  return useQuery({
    queryKey: ["playlists"],
    queryFn: fetchPlaylists,
    staleTime: 30_000,
  });
}

async function fetchPlaylist(id: string): Promise<PlaylistDetail> {
  const { data, error, response } = await client.GET("/api/playlists/{playlist_id}", {
    params: { path: { playlist_id: id } },
  });
  if (error || !response.ok || !data) throw new Error("Failed to load playlist");
  return data;
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
    mutationFn: async (body) => {
      const { data, error, response } = await client.POST("/api/playlists", {
        body: { name: body.name, description: body.description ?? "" },
      });
      if (error || !response.ok || !data) throw new Error("Create failed");
      return data;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["playlists"] });
    },
  });
}

export function useRenamePlaylist(id: string) {
  const queryClient = useQueryClient();
  return useMutation<Playlist, Error, { name: string }>({
    mutationFn: async (body) => {
      const { data, error, response } = await client.PATCH("/api/playlists/{playlist_id}", {
        params: { path: { playlist_id: id } },
        body,
      });
      if (error || !response.ok || !data) throw new Error("Rename failed");
      return data;
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ["playlist", id] });
      void queryClient.invalidateQueries({ queryKey: ["playlists"] });
    },
  });
}

export function useSetTargets(id: string) {
  const queryClient = useQueryClient();
  return useMutation<Playlist, Error, string[]>({
    mutationFn: async (targetPlexUsers) => {
      const { data, error, response } = await client.PATCH("/api/playlists/{playlist_id}", {
        params: { path: { playlist_id: id } },
        body: { target_plex_users: targetPlexUsers },
      });
      if (error || !response.ok || !data) throw new Error("Failed to save targets");
      return data;
    },
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
    mutationFn: async ({ playlistId, trackIds }) => {
      const { data, error, response } = await client.POST("/api/playlists/{playlist_id}/tracks", {
        params: { path: { playlist_id: playlistId } },
        body: { track_ids: trackIds },
      });
      if (error || !response.ok || !data) throw new Error("Add failed");
      return data;
    },
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
    mutationFn: async (entryUid) => {
      const { data, error, response } = await client.DELETE(
        "/api/playlists/{playlist_id}/entries/{entry_uid}",
        { params: { path: { playlist_id: id, entry_uid: entryUid } } },
      );
      if (error || !response.ok || !data) throw new Error("Remove failed");
      return data;
    },
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
    mutationFn: async ({ entryUid, itemId }) => {
      const { data, error, response } = await client.PATCH(
        "/api/playlists/{playlist_id}/entries/{entry_uid}",
        {
          params: { path: { playlist_id: id, entry_uid: entryUid } },
          body: { item_id: itemId },
        },
      );
      if (error || !response.ok || !data) throw new Error("Failed to match the track");
      return data;
    },
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

export function useReorderTracks(id: string) {
  const queryClient = useQueryClient();
  return useMutation<PlaylistDetail, Error, string[]>({
    mutationFn: async (entryUids) => {
      const { data, error, response } = await client.PUT("/api/playlists/{playlist_id}/tracks", {
        params: { path: { playlist_id: id } },
        body: { entry_uids: entryUids },
      });
      if (error || !response.ok || !data) throw new Error("Reorder failed");
      return data;
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ["playlist", id] });
    },
  });
}
