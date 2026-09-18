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

/** The confirm body, pinned WHOLE. Every clause is a claim about what Delete
 * does, and the copy this replaced was false on two of them — it promised the
 * whole album folder and a recoverable copy in Trash, where the move takes the
 * tracks, the cover and MusicDrop's lyric files only, leaves anything else in
 * the folder alone, and Restore re-IMPORTS the tracks. A fragment match would
 * pass again on the next such promise; this only passes on the sentence that
 * was checked against the behaviour. */
const BODY =
  "Tracks, cover art, and lyrics move to Trash; other files stay in the folder. " +
  "Restore re-imports only the tracks. Plex shows the album as unavailable " +
  "until a rescan.";

describe("DeleteAlbumAction", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("says what Delete actually moves, and promises no more recovery than that", async () => {
    renderAction();
    await userEvent.click(screen.getByRole("button", { name: /delete album/i }));
    expect(await screen.findByText(BODY)).toBeInTheDocument();
  });

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

  it("reopens with no alert from a failed delete", async () => {
    vi.spyOn(client, "DELETE").mockResolvedValue({
      data: undefined,
      error: { detail: "A library operation is in progress" },
      response: { ok: false, status: 409 },
    } as never);
    renderAction();

    const btn = screen.getByRole("button", { name: /delete album/i });
    await userEvent.click(btn);
    await userEvent.click(screen.getByRole("button", { name: /move to trash/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/library operation is in progress/i);

    // The mutation outlives the dialog, so the next open must not inherit it.
    await userEvent.click(screen.getByRole("button", { name: /cancel/i }));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
    await userEvent.click(btn);
    await screen.findByRole("alertdialog");
    expect(screen.queryByRole("alert")).toBeNull();
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
