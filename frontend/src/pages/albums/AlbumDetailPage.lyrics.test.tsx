import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import type { AlbumDetail } from "@/api/useAlbum";

const album: AlbumDetail = {
  id: 7, album_artist: "Radiohead", title: "In Rainbows", year: 2007,
  track_count: 2, genre: "Rock", mb_albumid: "rel-1",
  tracks: [
    { id: 1, title: "15 Step", track: 1, disc: 1, duration_seconds: 100, artist: "Radiohead", mb_trackid: "t1", has_lyrics: true },
    { id: 2, title: "Bodysnatchers", track: 2, disc: 1, duration_seconds: 100, artist: "Radiohead", mb_trackid: "t2", has_lyrics: false },
  ],
};

const mutateMock = vi.fn();
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
    useAlbumLyricsFetch: () => ({ mutate: mutateMock, isPending: false, data: undefined }),
  };
});

async function renderPage() {
  const { AlbumDetailPage } = await import("@/pages/albums/AlbumDetailPage");
  return render(
    <MemoryRouter initialEntries={["/albums/7"]}>
      <Routes>
        <Route path="/albums/:albumId" element={<AlbumDetailPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("AlbumDetailPage lyrics", () => {
  beforeEach(() => vi.clearAllMocks());

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
});
