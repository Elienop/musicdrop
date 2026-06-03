import { describe, expect, it, vi, beforeEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createElement, type ReactNode } from "react";

import { client } from "@/api/client";
import { useStartAlbumLyricsFetch, type LyricsBackfillStatus } from "@/api/useAlbumLyrics";

vi.mock("@/api/client", () => ({ client: { POST: vi.fn() } }));

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return createElement(QueryClientProvider, { client: qc }, children);
}

const status: LyricsBackfillStatus = {
  phase: "running", job_id: "j1", total: 12, processed: 0, found: 0, not_found: 0,
  failed: 0, skipped: 0, current: null, writes_enabled: true, error: null,
  album_id: 7, scope_label: "*NSYNC — *NSYNC",
};

describe("useStartAlbumLyricsFetch", () => {
  beforeEach(() => vi.clearAllMocks());

  it("POSTs the album fetch and returns the job status", async () => {
    (client.POST as ReturnType<typeof vi.fn>).mockResolvedValue({ data: status, response: { ok: true } });
    const { result } = renderHook(() => useStartAlbumLyricsFetch(7), { wrapper });
    result.current.mutate();
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual(status);
    expect(client.POST).toHaveBeenCalledWith("/api/albums/{album_id}/lyrics/fetch", {
      params: { path: { album_id: 7 } },
    });
  });
});
