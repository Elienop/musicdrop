import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { AlbumDetail } from "@/api/useAlbum";
import type { AlbumMissingReport } from "@/api/useAlbumMissing";
import { AlbumDetailPage } from "@/pages/albums/AlbumDetailPage";

const album: AlbumDetail = {
  id: 7, album_artist: "Radiohead", title: "In Rainbows", year: 2007,
  track_count: 2, genre: "Rock", mb_albumid: "rel-1",
  folder_outside_library: null,
  tracks: [
    { id: 1, title: "15 Step", track: 1, disc: 1, duration_seconds: 100, artist: "Radiohead", mb_trackid: "t1", has_lyrics: false, instrumental: false },
    { id: 2, title: "Bodysnatchers", track: 2, disc: 1, duration_seconds: 100, artist: "Radiohead", mb_trackid: "t2", has_lyrics: false, instrumental: false },
  ],
};

const useAlbumMissingMock = vi.fn();
vi.mock("@/api/useAlbum", async (orig) => {
  const actual = await orig<typeof import("@/api/useAlbum")>();
  return { ...actual, useAlbum: () => ({ data: album, isPending: false, isError: false }) };
});
vi.mock("@/api/useAlbumMissing", async (orig) => {
  const actual = await orig<typeof import("@/api/useAlbumMissing")>();
  return { ...actual, useAlbumMissing: () => useAlbumMissingMock() };
});
vi.mock("@/api/useAlbumLyrics", async (orig) => {
  const actual = await orig<typeof import("@/api/useAlbumLyrics")>();
  return {
    ...actual,
    useStartAlbumLyricsFetch: () => ({ mutate: vi.fn(), isPending: false, isError: false, error: null }),
  };
});
vi.mock("@/api/useLyricsBackfill", () => ({
  useLyricsBackfillStatus: () => ({
    data: {
      phase: "idle", job_id: null, total: 0, processed: 0, found: 0,
      not_found: 0, failed: 0, skipped: 0, current: null,
      writes_enabled: false, error: null, album_id: null, scope_label: "library",
    },
  }),
  useStopLyricsBackfill: () => ({ mutate: vi.fn(), isPending: false }),
}));

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/albums/7"]}>
        <Routes>
          <Route path="/albums/:albumId" element={<AlbumDetailPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function ok(missing: AlbumMissingReport["missing"]): { data: AlbumMissingReport; fetchStatus: string; refetch: () => void } {
  return {
    data: { status: "ok", total: 2 + missing.length, present_count: 2, missing, source: "MusicBrainz" },
    fetchStatus: "idle",
    refetch: vi.fn(),
  };
}

describe("AlbumDetailPage missing tracks", () => {
  beforeEach(() => vi.clearAllMocks());

  it("renders a greyed missing row + summary", async () => {
    useAlbumMissingMock.mockReturnValue(
      ok([{ index: 3, disc: 1, title: "Nude", duration_seconds: 100, mb_trackid: "t3" }]),
    );
    await renderPage();
    expect(await screen.findByText("Nude")).toBeInTheDocument();
    expect(screen.getByText("missing")).toBeInTheDocument();
    expect(screen.getByText(/1 of 3 tracks missing/i)).toBeInTheDocument();
  });

  it("shows no overlay when the release is complete", async () => {
    useAlbumMissingMock.mockReturnValue(ok([]));
    await renderPage();
    expect(screen.queryByText("missing")).not.toBeInTheDocument();
    expect(screen.queryByText(/tracks missing/i)).not.toBeInTheDocument();
  });

  it("surfaces a fetch_failed note with Retry, naming the source", async () => {
    useAlbumMissingMock.mockReturnValue({
      data: { status: "fetch_failed", total: 0, present_count: 0, missing: [], source: "Deezer" },
      fetchStatus: "idle",
      refetch: vi.fn(),
    });
    await renderPage();
    expect(await screen.findByText(/couldn’t reach deezer/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument();
  });

  it("names the source in a release_unavailable note", async () => {
    useAlbumMissingMock.mockReturnValue({
      data: {
        status: "release_unavailable",
        total: 0,
        present_count: 0,
        missing: [],
        source: "Deezer",
      },
      fetchStatus: "idle",
      refetch: vi.fn(),
    });
    await renderPage();
    expect(await screen.findByText(/couldn’t find this release on deezer/i)).toBeInTheDocument();
  });

  it("shows a source-agnostic note while still fetching", async () => {
    useAlbumMissingMock.mockReturnValue({ data: undefined, fetchStatus: "fetching", refetch: vi.fn() });
    await renderPage();
    expect(await screen.findByText(/checking for missing tracks/i)).toBeInTheDocument();
    expect(screen.queryByText(/musicbrainz/i)).not.toBeInTheDocument();
  });
});
