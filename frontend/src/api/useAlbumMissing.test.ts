import { describe, expect, it, vi, beforeEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createElement, type ReactNode } from "react";

import { client } from "@/api/client";
import { useAlbumMissing, type AlbumMissingReport } from "@/api/useAlbumMissing";

vi.mock("@/api/client", () => ({ client: { GET: vi.fn() } }));

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return createElement(QueryClientProvider, { client: qc }, children);
}

const report: AlbumMissingReport = {
  status: "ok",
  total: 4,
  present_count: 2,
  missing: [{ index: 3, disc: 1, title: "Track 3", duration_seconds: 183, mb_trackid: "t3" }],
  source: "MusicBrainz",
};

describe("useAlbumMissing", () => {
  beforeEach(() => vi.clearAllMocks());

  it("fetches the report when an mbid is present", async () => {
    (client.GET as ReturnType<typeof vi.fn>).mockResolvedValue({ data: report, error: undefined });
    const { result } = renderHook(() => useAlbumMissing(7, "rel-1"), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual(report);
    expect(client.GET).toHaveBeenCalledWith("/api/albums/{album_id}/missing", {
      params: { path: { album_id: 7 } },
    });
  });

  it("does not fetch when mbid is null (non-MusicBrainz album)", () => {
    (client.GET as ReturnType<typeof vi.fn>).mockResolvedValue({ data: report, error: undefined });
    const { result } = renderHook(() => useAlbumMissing(7, null), { wrapper });
    expect(result.current.fetchStatus).toBe("idle");
    expect(client.GET).not.toHaveBeenCalled();
  });
});
