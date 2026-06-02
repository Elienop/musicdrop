import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AlbumEditPanel } from "@/pages/albums/AlbumEditPanel";
import { client } from "@/api/client";
import type { AlbumDetail } from "@/api/useAlbum";

const album = {
  id: 7,
  album_artist: "Radiohead",
  title: "In Rainbows",
  year: 2007,
  genre: "Alternative Rock",
  track_count: 1,
  tracks: [{ id: 1, title: "15 Step", track: 1, disc: 1, duration_seconds: 230, artist: "Radiohead" }],
} as unknown as AlbumDetail;

function renderPanel() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <AlbumEditPanel album={album} onClose={() => {}} />
    </QueryClientProvider>,
  );
}

describe("AlbumEditPanel", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("previews a title change then shows the diff", async () => {
    vi.spyOn(client, "POST").mockResolvedValue({
      data: {
        changed_fields: ["title"],
        album_before: { title: "In Rainbows" },
        album_after: { title: "In Rainbows (R)" },
        tracks: [],
        move_enabled: false,
        move_plan: [],
      },
    } as never);

    renderPanel();
    const titleInput = screen.getByLabelText(/album title/i);
    fireEvent.change(titleInput, { target: { value: "In Rainbows (R)" } });
    fireEvent.click(screen.getByRole("button", { name: /preview/i }));

    await waitFor(() => expect(screen.getByText("In Rainbows (R)")).toBeInTheDocument());
    expect(screen.getByText(/will change/i)).toBeInTheDocument();
  });

  it("shows the move notice when move_enabled and move_plan present", async () => {
    vi.spyOn(client, "POST").mockResolvedValue({
      data: {
        changed_fields: ["album_artist"],
        album_before: { album_artist: "Radiohead" },
        album_after: { album_artist: "Radiohead (Live)" },
        tracks: [],
        move_enabled: true,
        move_plan: [{ item_id: 1, track: 1, old_path: "/a/x.flac", new_path: "/b/x.flac" }],
      },
    } as never);

    renderPanel();
    fireEvent.change(screen.getByLabelText(/album artist/i), {
      target: { value: "Radiohead (Live)" },
    });
    fireEvent.click(screen.getByRole("button", { name: /preview/i }));
    await waitFor(() => expect(screen.getByText(/will move 1 file/i)).toBeInTheDocument());
  });
});
