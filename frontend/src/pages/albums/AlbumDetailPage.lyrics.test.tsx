import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { AlbumDetail } from "@/api/useAlbum";
import type { LyricsBackfillStatus } from "@/api/useLyricsBackfill";

const album: AlbumDetail = {
  id: 7, album_artist: "Radiohead", title: "In Rainbows", year: 2007,
  track_count: 2, genre: "Rock", mb_albumid: "rel-1",
  tracks: [
    { id: 1, title: "15 Step", track: 1, disc: 1, duration_seconds: 100, artist: "Radiohead", mb_trackid: "t1", has_lyrics: true },
    { id: 2, title: "Bodysnatchers", track: 2, disc: 1, duration_seconds: 100, artist: "Radiohead", mb_trackid: "t2", has_lyrics: false },
  ],
};

const idleStatus: LyricsBackfillStatus = {
  phase: "idle", job_id: null, total: 0, processed: 0, found: 0,
  not_found: 0, failed: 0, skipped: 0, current: null,
  writes_enabled: false, error: null, album_id: null, scope_label: "library",
};
let statusData: LyricsBackfillStatus = idleStatus;

const mutateMock = vi.fn();
let startState: {
  mutate: typeof mutateMock;
  isPending: boolean;
  isError: boolean;
  error: Error | null;
} = { mutate: mutateMock, isPending: false, isError: false, error: null };

vi.mock("@/api/useAlbum", async (orig) => {
  const actual = await orig<typeof import("@/api/useAlbum")>();
  return { ...actual, useAlbum: () => ({ data: album, isPending: false, isError: false }) };
});
vi.mock("@/api/useAlbumMissing", async (orig) => {
  const actual = await orig<typeof import("@/api/useAlbumMissing")>();
  return { ...actual, useAlbumMissing: () => ({ data: undefined, fetchStatus: "idle" }) };
});
vi.mock("@/api/useAlbumLyrics", async (orig) => {
  const actual = await orig<typeof import("@/api/useAlbumLyrics")>();
  return {
    ...actual,
    useStartAlbumLyricsFetch: () => startState,
  };
});
vi.mock("@/api/useLyricsBackfill", () => ({
  useLyricsBackfillStatus: () => ({ data: statusData }),
  useStopLyricsBackfill: () => ({ mutate: vi.fn(), isPending: false }),
}));

async function renderPage() {
  const { AlbumDetailPage } = await import("@/pages/albums/AlbumDetailPage");
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

describe("AlbumDetailPage lyrics", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    statusData = idleStatus;
    startState = { mutate: mutateMock, isPending: false, isError: false, error: null };
  });

  it("shows a lyrics indicator per track and a coverage summary", async () => {
    await renderPage();
    // one track has lyrics, one doesn't -> "1 of 2"
    expect(await screen.findByText(/1 of 2 tracks have lyrics/i)).toBeInTheDocument();
    expect(screen.getByLabelText("Has lyrics")).toBeInTheDocument();
    expect(screen.getByLabelText("No lyrics")).toBeInTheDocument();
  });

  it("fires the per-album fetch when the button is clicked", async () => {
    const user = (await import("@testing-library/user-event")).default.setup();
    await renderPage();
    await user.click(screen.getByRole("button", { name: /fetch missing lyrics/i }));
    expect(mutateMock).toHaveBeenCalledTimes(1);
  });

  it("surfaces a per-album fetch error", async () => {
    startState = {
      mutate: mutateMock, isPending: false, isError: true,
      error: new Error("A library operation is in progress"),
    };
    await renderPage();
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/library operation is in progress/i);
  });

  it("marches with live progress while THIS album's job runs", async () => {
    statusData = {
      ...idleStatus, phase: "running", album_id: 7, scope_label: "Radiohead — In Rainbows",
      processed: 3, total: 12, found: 2,
    };
    await renderPage();
    expect(await screen.findByText(/Fetching lyrics… 3 \/ 12 · found 2/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /stop/i })).toBeInTheDocument();
  });
});
