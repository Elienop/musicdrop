import { describe, expect, it, vi, beforeEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createElement, type ReactNode } from "react";

import { client } from "@/api/client";
import { useAlbumLyricsFetch, type AlbumLyricsResult } from "@/api/useAlbumLyrics";

vi.mock("@/api/client", () => ({ client: { POST: vi.fn() } }));

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return createElement(QueryClientProvider, { client: qc }, children);
}

const result: AlbumLyricsResult = {
  album_id: 7, fetched: 1, not_found: 1, failed: 0, skipped: 0,
  items: [
    { item_id: 1, status: "found", source: "lrclib", written: true },
    { item_id: 2, status: "not_found", source: null, written: false },
  ],
  writes_enabled: true,
};

describe("useAlbumLyricsFetch", () => {
  beforeEach(() => vi.clearAllMocks());

  it("POSTs the album fetch and returns the result", async () => {
    (client.POST as ReturnType<typeof vi.fn>).mockResolvedValue({
      data: result, error: undefined, response: { ok: true },
    });
    const { result: hook } = renderHook(() => useAlbumLyricsFetch(7), { wrapper });
    hook.current.mutate();
    await waitFor(() => expect(hook.current.isSuccess).toBe(true));
    expect(hook.current.data).toEqual(result);
    expect(client.POST).toHaveBeenCalledWith("/api/albums/{album_id}/lyrics/fetch", {
      params: { path: { album_id: 7 } },
    });
  });
});
