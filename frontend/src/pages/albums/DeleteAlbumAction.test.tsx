import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { client } from "@/api/client";
import type { AlbumDetail } from "@/api/useAlbum";
import { DeleteAlbumAction } from "@/pages/albums/DeleteAlbumAction";

const album = {
  id: 42,
  album_artist: "Daft Punk",
  title: "Discovery",
  tracks: [],
} as unknown as AlbumDetail;

function ok(data: unknown) {
  return { data, error: undefined, response: { ok: true, status: 200 } } as never;
}

function Loc() {
  return <div data-testid="loc">{useLocation().pathname}</div>;
}

function renderAction() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/albums/42"]}>
        <Routes>
          <Route
            path="/albums/:albumId"
            element={
              <>
                <DeleteAlbumAction album={album} />
                <Loc />
              </>
            }
          />
          <Route path="/artists/:name" element={<Loc />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("DeleteAlbumAction", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("confirms, deletes the album, then navigates to the artist", async () => {
    const del = vi
      .spyOn(client, "DELETE")
      .mockResolvedValue(ok({ trashed_albums: 1, trash_path: "/trash/Discovery" }));
    renderAction();

    await userEvent.click(screen.getByRole("button", { name: /delete album/i }));
    expect(await screen.findByText(/move this album to trash/i)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /move to trash/i }));

    await waitFor(() =>
      expect(del).toHaveBeenCalledWith("/api/albums/{album_id}", {
        params: { path: { album_id: 42 } },
      }),
    );
    await waitFor(() => expect(screen.getByTestId("loc")).toHaveTextContent("/artists/"));
  });

  it("does nothing when cancelled", async () => {
    const del = vi.spyOn(client, "DELETE");
    renderAction();
    await userEvent.click(screen.getByRole("button", { name: /delete album/i }));
    await userEvent.click(screen.getByRole("button", { name: /cancel/i }));
    expect(del).not.toHaveBeenCalled();
    expect(screen.getByTestId("loc")).toHaveTextContent("/albums/42");
  });
});
