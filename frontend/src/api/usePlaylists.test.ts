import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { createElement, type ReactNode } from "react";
import { describe, expect, test } from "vitest";

import { useMergePlaylist } from "@/api/usePlaylists";
import { server } from "@/test/msw-server";

const TARGET = "a".repeat(32);
const SOURCE = "b".repeat(32);

function wrapperFor(queryClient: QueryClient) {
  return ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client: queryClient }, children);
}

function detail(name: string) {
  return {
    id: TARGET,
    name,
    description: "",
    track_count: 2,
    pending_count: 0,
    target_plex_users: [],
    plex: {},
    created_at: "2026-08-16T00:00:00+00:00",
    updated_at: "2026-08-16T01:00:00+00:00",
    artwork_hash: null,
    cover_album_ids: [],
    tracks: [],
  };
}

describe("useMergePlaylist", () => {
  test("posts the source id and delete flag, and seeds the detail cache", async () => {
    let body: unknown = null;
    server.use(
      http.post(`${window.location.origin}/api/playlists/${TARGET}/merge`, async ({ request }) => {
        body = await request.json();
        return HttpResponse.json({
          playlist: detail("Keep"),
          added: 3,
          skipped_duplicates: 1,
          source_deleted: true,
        });
      }),
    );
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { result } = renderHook(() => useMergePlaylist(TARGET), {
      wrapper: wrapperFor(queryClient),
    });

    result.current.mutate({ sourceId: SOURCE, deleteSource: true });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(body).toEqual({ source_id: SOURCE, delete_source: true });
    expect(result.current.data?.added).toBe(3);
    expect(result.current.data?.skipped_duplicates).toBe(1);
    // The whole list is rewritten server-side, so the response IS the new
    // detail - seed it rather than round-tripping a refetch.
    expect(queryClient.getQueryData(["playlist", TARGET])).toEqual(detail("Keep"));
  });

  test("invalidates the source's cached detail and the playlist list", async () => {
    server.use(
      http.post(`${window.location.origin}/api/playlists/${TARGET}/merge`, () =>
        HttpResponse.json({
          playlist: detail("Keep"),
          added: 3,
          skipped_duplicates: 1,
          source_deleted: true,
        }),
      ),
    );
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    // Both caches are stale the moment the merge lands: the source is either
    // gone or emptied of the rows that moved, and every summary in the list
    // carries a track_count. Seeded so there is something real to invalidate -
    // invalidateQueries on a key the cache has never seen marks nothing.
    queryClient.setQueryData(["playlist", SOURCE], detail("Road trip"));
    queryClient.setQueryData(["playlists"], []);
    const { result } = renderHook(() => useMergePlaylist(TARGET), {
      wrapper: wrapperFor(queryClient),
    });

    result.current.mutate({ sourceId: SOURCE, deleteSource: true });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(queryClient.getQueryState(["playlist", SOURCE])?.isInvalidated).toBe(true);
    expect(queryClient.getQueryState(["playlists"])?.isInvalidated).toBe(true);
  });
});
