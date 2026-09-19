import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { client } from "@/api/client";
import { DeleteArtistAction } from "@/pages/artists/DeleteArtistAction";

function ok(data: unknown) {
  return { data, error: undefined, response: { ok: true, status: 200 } } as never;
}

function Loc() {
  return <div data-testid="loc">{useLocation().pathname}</div>;
}

function renderAction(albumCount = 3) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/artists/Daft%20Punk"]}>
        <Routes>
          <Route
            path="/artists/:name"
            element={
              <>
                <DeleteArtistAction name="Daft Punk" albumCount={albumCount} />
                <Loc />
              </>
            }
          />
          <Route path="/artists" element={<Loc />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** The confirm body, pinned WHOLE — see the twin in DeleteAlbumAction.test.tsx
 * for why a fragment is not enough. The copy this replaced claimed the artist's
 * FOLDERS move and that everything stays recoverable in Trash; neither is true
 * of what Delete does. THREE clauses inflect on the count, not one, and a
 * plural leaking into the singular arm is the whole reason both are pinned. */
function body(count: number, plural: string, folder: string, noun: string): string {
  return (
    `Tracks, cover art, and lyrics from ${count} album${plural} by Daft Punk ` +
    `move to Trash; other files stay in ${folder}. Restore re-imports only the ` +
    `tracks. Plex shows ${noun} as unavailable until a rescan.`
  );
}

const THREE = [3, "s", "their folders", "the albums"] as const;
// "the folder", not "its folder": the nearest singular noun before a possessive
// here is Trash, and "the Trash folder" is a real phrase in this app.
const ONE = [1, "", "the folder", "the album"] as const;

describe("DeleteArtistAction", () => {
  beforeEach(() => vi.restoreAllMocks());

  it.each([THREE, ONE])(
    "says what Delete actually moves for %i album(s), promising only the tracks back",
    async (count, plural, folder, noun) => {
      renderAction(count);
      await userEvent.click(screen.getByRole("button", { name: /delete artist/i }));
      expect(
        await screen.findByText(body(count, plural, folder, noun)),
      ).toBeInTheDocument();
    },
  );

  it("confirms, deletes by name (query param), then navigates to the roster", async () => {
    const del = vi
      .spyOn(client, "DELETE")
      .mockResolvedValue(ok({ trashed_albums: 3, trash_path: "/trash" }));
    renderAction();

    await userEvent.click(screen.getByRole("button", { name: /delete artist/i }));
    // Count + name surface in the confirm copy.
    expect(await screen.findByText(body(...THREE))).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /move to trash/i }));

    await waitFor(() =>
      expect(del).toHaveBeenCalledWith("/api/artists", {
        params: { query: { name: "Daft Punk" } },
      }),
    );
    await waitFor(() => expect(screen.getByTestId("loc")).toHaveTextContent(/^\/artists$/));
  });

  it("reopens with no alert from a failed delete", async () => {
    vi.spyOn(client, "DELETE").mockResolvedValue({
      data: undefined,
      error: { detail: "A library operation is in progress" },
      response: { ok: false, status: 409 },
    } as never);
    renderAction();

    const btn = screen.getByRole("button", { name: /delete artist/i });
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

  /** The twin of the album dialog's pin — the artist delete moves N albums, so
   * a half-done one is the arm the guard exists for. */
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
    await userEvent.click(screen.getByRole("button", { name: /delete artist/i }));
    await userEvent.click(screen.getByRole("button", { name: /move to trash/i }));
    return screen.findByRole("alert");
  }

  it("shows the server's recovery hint under the failure message", async () => {
    const alert = await failDelete({
      message: "Delete failed: [Errno 28] No space left on device",
      recovery: RECOVERY,
    });
    expect(alert).toHaveTextContent("Delete failed: [Errno 28] No space left on device");
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
});
