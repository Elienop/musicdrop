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

  /** The 500's recovery arm that guards the only copy (delete.py `_recovery`).
   * Its sentence ORDER is the measured part — retry BEFORE emptying Trash — so
   * it has to reach the user whole. */
  const RECOVERY =
    "A delete that stops part-way can leave some or all of the files in Trash. " +
    "Retry before emptying Trash: emptying now can destroy the only copy.";

  async function failDelete(detail: unknown) {
    vi.spyOn(client, "DELETE").mockResolvedValue({
      data: undefined,
      error: { detail },
      response: { ok: false, status: 500 },
    } as never);
    renderAction();
    await userEvent.click(screen.getByRole("button", { name: /delete album/i }));
    await userEvent.click(screen.getByRole("button", { name: /move to trash/i }));
    return screen.findByRole("alert");
  }

  it("shows the server's recovery hint under the failure message", async () => {
    const alert = await failDelete({
      message: "Delete failed: [Errno 28] No space left on device",
      recovery: RECOVERY,
    });
    expect(alert).toHaveTextContent("Delete failed: [Errno 28] No space left on device");
    // One alert, both lines: a screen reader hears the guard with the failure.
    expect(alert).toHaveTextContent(RECOVERY);
  });

  it("keeps an unbroken 150-character path inside the dialog at 320", async () => {
    // The real 500: a permission failure interpolates two absolute paths, and
    // Chromium gives no wrap opportunity at `/` or `_`. The <p> is a GRID item
    // of AlertDialogContent, so `min-width: auto` made it 365px wide inside a
    // 288px dialog at 320 and clipped the buttons. jsdom lays nothing out, so
    // the class pair is the tripwire; the browser pass is the oracle.
    const path = `/mnt/${"z".repeat(150)}/01 Track.flac`;
    const alert = await failDelete({ message: `Delete failed: ${path}` });
    expect(alert).toHaveTextContent(path);
    expect(alert).toHaveClass("min-w-0", "break-words");
  });

  it("adds no second line when the body carries no recovery", async () => {
    const alert = await failDelete({ message: "Delete failed: boom" });
    expect(alert).toHaveTextContent("Delete failed: boom");
    expect(alert.querySelector("span")).toBeNull();
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
