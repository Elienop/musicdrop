import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { client } from "@/api/client";
import { detailMessage } from "@/api/lib";
import type { components } from "@/api/schema";

/** One previewed playlist: its entries plus match tallies (generated contract). */
export type PlaylistImportPreview = components["schemas"]["PlaylistImportPreview"];
/** `POST /import/preview` envelope — one preview per source playlist (generated). */
export type PlaylistImportPreviewResponse =
  components["schemas"]["PlaylistImportPreviewResponse"];
/** One source entry with its match verdict (generated contract). */
export type ImportEntryPreview = components["schemas"]["ImportEntryPreview"];
/** A library track offered as a match or suggestion (generated contract). */
export type TrackSummary = components["schemas"]["TrackSummary"];
/** `POST /import` body — the resolved playlists to create (generated contract). */
export type PlaylistImportRequest = components["schemas"]["PlaylistImportRequest"];
/** `POST /import` response — the created playlists (generated contract). */
export type PlaylistImportResponse = components["schemas"]["PlaylistImportResponse"];
/** One uploaded m3u file (name + raw text) for the preview (generated contract). */
export type PlaylistImportFile = components["schemas"]["PlaylistImportFile"];
/** A Plex playlist the import source picker lists (generated contract). */
export type PlexPlaylistInfo = components["schemas"]["PlexPlaylistInfo"];

/** Exactly one source per preview request: uploaded files OR named Plex
 * playlists (the generated `PlaylistImportPreviewRequest` union, narrowed to
 * the two shapes the UI actually sends). */
export type ImportSource =
  | { files: PlaylistImportFile[] }
  | { plex_playlists: string[] };

async function fetchPlexPlaylists(): Promise<PlexPlaylistInfo[]> {
  const { data, error, response } = await client.GET("/api/plex/playlists");
  // Surface the flat `detail` the backend sends on 409 (Plex not configured)
  // and 502 (Plex unreachable) so the source picker shows the real reason.
  if (error || !response.ok || !data) {
    throw new Error(detailMessage(error) ?? "Failed to load Plex playlists");
  }
  return data.playlists;
}

/**
 * The server's audio playlists (`GET /api/plex/playlists`) for the import
 * source picker. `retry: false` so an unconfigured (409) or unreachable (502)
 * Plex fails fast to the surfaced detail instead of hammering the server.
 * `enabled` lets the page defer the call until the Plex source is in view.
 */
export function usePlexImportPlaylists(enabled: boolean) {
  return useQuery({
    queryKey: ["plex", "import-playlists"],
    queryFn: fetchPlexPlaylists,
    enabled,
    retry: false,
  });
}

/**
 * Preview an import (`POST /api/playlists/import/preview`): send EITHER
 * uploaded m3u files OR named Plex playlists and get back one match preview per
 * playlist (each entry classified matched / ambiguous / unmatched). A pure
 * read — it creates nothing, so there is nothing to invalidate.
 */
export function useImportPreview() {
  return useMutation<PlaylistImportPreviewResponse, Error, ImportSource>({
    mutationFn: async (source) => {
      const { data, error, response } = await client.POST(
        "/api/playlists/import/preview",
        { body: source },
      );
      if (error || !response.ok || !data) {
        throw new Error(detailMessage(error) ?? "Failed to preview the import");
      }
      return data;
    },
  });
}

/**
 * Commit a reviewed import (`POST /api/playlists/import`): each entry is either
 * a resolved library track (`{item_id}`) or a remembered pending record
 * (`{pending: {…}}`). Creates the playlists, so it invalidates the list on
 * success.
 */
export function useImportCommit() {
  const queryClient = useQueryClient();
  return useMutation<PlaylistImportResponse, Error, PlaylistImportRequest>({
    mutationFn: async (request) => {
      const { data, error, response } = await client.POST("/api/playlists/import", {
        body: request,
      });
      if (error || !response.ok || !data) {
        throw new Error(detailMessage(error) ?? "Failed to import the playlists");
      }
      return data;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["playlists"] });
    },
  });
}
