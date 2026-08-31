import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, test } from "vitest";

import type { TrashedAlbum } from "@/api/useTrash";
import { SettingsTrashPage } from "@/pages/settings/SettingsTrashPage";
import { server } from "@/test/msw-server";

const TRASH_URL = `${window.location.origin}/api/trash`;
const RESTORE_URL = `${window.location.origin}/api/trash/restore`;
const ALL_URL = `${window.location.origin}/api/trash/all`;

// Typed, not a bare object literal: `restore_mode` and friends are REQUIRED on
// the wire contract, so an untyped fixture would let the page render an
// undefined mode and every assertion below would still pass.
const album: TrashedAlbum = {
  folder: "2 Brothers - Dreams",
  album_artist: "2 Brothers",
  album: "Dreams",
  year: 1994,
  track_count: 2,
  format: "FLAC",
  restore_mode: "move_back",
  restore_note: null,
  origin: "/music/2 Brothers/Dreams",
};

/** The backend writes three distinct why-sentences; the page must render
 * whatever arrives, so the tests carry one verbatim rather than a shape. */
const NO_RECORD_NOTE =
  "MusicDrop has no record of where this came from — it was moved to Trash before" +
  " origins were recorded. Restoring re-imports it, so beets files it under your" +
  " current naming rules rather than putting it back.";

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <SettingsTrashPage />
    </QueryClientProvider>,
  );
}

describe("SettingsTrashPage", () => {
  test("lists trashed albums with their meta", async () => {
    server.use(
      http.get(TRASH_URL, () =>
        HttpResponse.json({ albums: [album], trash_path: "/data/beets/trash" }),
      ),
    );
    renderPage();
    expect(await screen.findByText(/2 Brothers - Dreams/)).toBeInTheDocument();
    expect(screen.getByText("2 tracks · FLAC · 1994")).toBeInTheDocument();
    expect(screen.getByText("/data/beets/trash")).toBeInTheDocument();
  });

  test("shows an empty state when Trash is empty", async () => {
    server.use(
      http.get(TRASH_URL, () =>
        HttpResponse.json({ albums: [], trash_path: "/t" }),
      ),
    );
    renderPage();
    expect(await screen.findByText(/Trash is empty/i)).toBeInTheDocument();
  });

  test("Empty confirms then DELETEs that folder", async () => {
    let deleted: string | null = null;
    server.use(
      http.get(TRASH_URL, () =>
        HttpResponse.json({ albums: [album], trash_path: "/t" }),
      ),
      http.delete(TRASH_URL, ({ request }) => {
        deleted = new URL(request.url).searchParams.get("folder");
        return HttpResponse.json({ removed: 1 });
      }),
    );
    const user = userEvent.setup();
    renderPage();

    await user.click(
      await screen.findByRole("button", { name: /Empty Dreams/i }),
    );
    const dialog = await screen.findByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: "Delete" }));

    await waitFor(() => expect(deleted).toBe("2 Brothers - Dreams"));
  });

  test("Empty all DELETEs /api/trash/all", async () => {
    let called = false;
    server.use(
      http.get(TRASH_URL, () =>
        HttpResponse.json({ albums: [album], trash_path: "/t" }),
      ),
      http.delete(ALL_URL, () => {
        called = true;
        return HttpResponse.json({ removed: 1 });
      }),
    );
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Empty all" }));
    const dialog = await screen.findByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: "Empty all" }));

    await waitFor(() => expect(called).toBe(true));
  });

  test("Restore posts the folder and shows the restored result", async () => {
    let posted: unknown = null;
    server.use(
      http.get(TRASH_URL, () =>
        HttpResponse.json({ albums: [album], trash_path: "/t" }),
      ),
      http.post(RESTORE_URL, async ({ request }) => {
        posted = await request.json();
        return HttpResponse.json({
          restored: true,
          reason: "restored",
          album_id: 7,
        });
      }),
    );
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: /^Restore$/ }));
    await waitFor(() =>
      expect(posted).toEqual({ folder: "2 Brothers - Dreams" }),
    );
    expect(
      await screen.findByText(/Restored to your library/i),
    ).toBeInTheDocument();
  });

  test("a zero-track row keeps Restore enabled and explains the uncertainty", async () => {
    // track_count === 0 means "nothing here produced a readable media Item",
    // NOT "no audio" (see the comment in trash_manage.py) — beets' importer
    // reads more than Item.from_path does, so Restore may genuinely work. The
    // row must say so without taking the button away.
    const emptyAlbum: TrashedAlbum = {
      folder: "No Audio - Ghost",
      album_artist: null,
      album: null,
      year: null,
      track_count: 0,
      format: null,
      restore_mode: "import",
      restore_note: NO_RECORD_NOTE,
      origin: null,
    };
    server.use(
      http.get(TRASH_URL, () =>
        HttpResponse.json({ albums: [emptyAlbum], trash_path: "/t" }),
      ),
    );
    renderPage();

    const restore = await screen.findByRole("button", { name: /^Restore$/ });
    expect(restore).toBeEnabled();
    expect(
      await screen.findByText(/couldn.t read audio tags here/i),
    ).toBeInTheDocument();
    expect(restore).toHaveAccessibleDescription(/restore may still work/i);
  });

  test("a move-back row names the folder Restore returns it to", async () => {
    server.use(
      http.get(TRASH_URL, () =>
        HttpResponse.json({ albums: [album], trash_path: "/t" }),
      ),
    );
    renderPage();

    const restore = await screen.findByRole("button", { name: /^Restore$/ });
    expect(screen.getByText("Exact restore.")).toBeInTheDocument();
    expect(screen.getByText("/music/2 Brothers/Dreams")).toBeInTheDocument();
    // The promise has to reach the button itself: a sighted user reads the line
    // beside it, a screen-reader user only gets what it is described by.
    expect(restore).toHaveAccessibleDescription(
      /Goes back to\s+\/music\/2 Brothers\/Dreams/,
    );
  });

  test("an import row shows the backend's note verbatim and keeps Restore enabled", async () => {
    // Owner's call: an approximate restore is still a recovery path, so it is
    // warned about, never disabled.
    const reimported: TrashedAlbum = {
      folder: "Old Band - Demos",
      album_artist: "Old Band",
      album: "Demos",
      year: 1999,
      track_count: 4,
      format: "MP3",
      restore_mode: "import",
      restore_note: NO_RECORD_NOTE,
      origin: "/old-library/Old Band/Demos",
    };
    server.use(
      http.get(TRASH_URL, () =>
        HttpResponse.json({ albums: [reimported], trash_path: "/t" }),
      ),
    );
    renderPage();

    const restore = await screen.findByRole("button", { name: /^Restore$/ });
    expect(restore).toBeEnabled();
    expect(screen.getByText("Approximate restore.")).toBeInTheDocument();
    expect(screen.getByText(NO_RECORD_NOTE)).toBeInTheDocument();
    // origin survives an import row so the user can put it back by hand.
    expect(screen.getByText("/old-library/Old Band/Demos")).toBeInTheDocument();
    expect(restore).toHaveAccessibleDescription(
      /no record of where this came from/i,
    );
    expect(restore).toHaveAccessibleDescription(
      /Was at\s+\/old-library\/Old Band\/Demos/,
    );
    expect(screen.queryByText("Exact restore.")).not.toBeInTheDocument();
  });

  test("a zero-track move-back row drops the hedge that would contradict it", async () => {
    // The row this feature exists for: an audio-free husk WITH a recorded
    // origin is moved back whole. "Restore may still work" next to an exact
    // promise would be two different answers to the same question, so the
    // zero-track line explains the empty meta instead.
    const husk: TrashedAlbum = {
      folder: "Scans (LP)",
      album_artist: null,
      album: null,
      year: null,
      track_count: 0,
      format: null,
      restore_mode: "move_back",
      restore_note: null,
      origin: "/music/Some Artist/Some Album/Scans (LP)",
    };
    server.use(
      http.get(TRASH_URL, () =>
        HttpResponse.json({ albums: [husk], trash_path: "/t" }),
      ),
    );
    renderPage();

    const restore = await screen.findByRole("button", { name: /^Restore$/ });
    expect(restore).toBeEnabled();
    expect(await screen.findByText(/no track details/i)).toBeInTheDocument();
    expect(screen.queryByText(/may still work/i)).not.toBeInTheDocument();
    expect(restore).toHaveAccessibleDescription(/Goes back to/);
    expect(restore).toHaveAccessibleDescription(
      /not a sign Restore won’t work/,
    );
  });

  test("Restore explains an occupied origin instead of failing generically", async () => {
    server.use(
      http.get(TRASH_URL, () =>
        HttpResponse.json({ albums: [album], trash_path: "/t" }),
      ),
      http.post(RESTORE_URL, () =>
        HttpResponse.json({
          restored: false,
          reason: "origin_occupied",
          album_id: null,
        }),
      ),
    );
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: /^Restore$/ }));
    const message = await screen.findByText(/original folder exists again/i);
    expect(message).toHaveTextContent(/still in Trash/i);
    expect(screen.queryByText(/^Couldn’t restore$/)).not.toBeInTheDocument();
    // A refusal must not be painted like the success it replaces.
    expect(message).toHaveClass("text-warning");
  });

  test("a row lets its actions wrap below the text rather than squeezing it", async () => {
    // Both halves or neither: `flex-wrap` with a 0 basis never wraps, and a
    // 16rem basis without `flex-wrap` squeezes the actions off a narrow row
    // instead. jsdom computes no layout, so this pins the pair that makes the
    // 390px reflow possible — the reflow itself is a browser check.
    server.use(
      http.get(TRASH_URL, () =>
        HttpResponse.json({ albums: [album], trash_path: "/t" }),
      ),
    );
    renderPage();

    const row = (await screen.findByText(/2 Brothers - Dreams/)).closest("li");
    expect(row).toHaveClass("flex-wrap");
    expect(row?.firstElementChild).toHaveClass("basis-64", "grow");
  });

  test("an approximate row can shrink below its longest unbreakable path segment", async () => {
    // The warning arm nests the note in a flex item, which defaults to
    // `min-width: auto` — a floor set by the longest token inside it.
    // `overflow-wrap` (the `break-words` on the path) picks where LINES break
    // and does NOT lower that floor, so without `min-w-0` an origin with no
    // separator to break at pushes the whole page sideways. Measured at 320px
    // with an 85-character space-free path: scrollWidth 633 vs clientWidth 305,
    // and 305 once this class is present. jsdom computes no layout, so this
    // pins the class; the overflow itself is a browser check.
    const unbreakable = {
      ...album,
      restore_mode: "import" as const,
      restore_note: "No record of where this came from.",
      origin:
        "/music/Godspeed_You_Black_Emperor_Lift_Your_Skinny_Fists_Like_Antennas/Disc_One",
    };
    server.use(
      http.get(TRASH_URL, () =>
        HttpResponse.json({ albums: [unbreakable], trash_path: "/t" }),
      ),
    );
    renderPage();

    const path = await screen.findByText(unbreakable.origin);
    expect(path).toHaveClass("break-words");
    // The flex item between the warning icon and the path must have a floor of
    // zero, or the path's min-content width becomes the row's.
    expect(path.closest("p")?.querySelector(":scope > span")).toHaveClass(
      "min-w-0",
    );
  });

  test("the header points at the per-row promise instead of making one", async () => {
    server.use(
      http.get(TRASH_URL, () =>
        HttpResponse.json({ albums: [album], trash_path: "/t" }),
      ),
    );
    renderPage();

    expect(
      await screen.findByText(/Each row says where Restore will put it/i),
    ).toBeInTheDocument();
    expect(screen.queryByText(/puts one back as-is/i)).not.toBeInTheDocument();
  });

  test("Restore surfaces the already-in-library result", async () => {
    server.use(
      http.get(TRASH_URL, () =>
        HttpResponse.json({ albums: [album], trash_path: "/t" }),
      ),
      http.post(RESTORE_URL, () =>
        HttpResponse.json({
          restored: false,
          reason: "already_in_library",
          album_id: null,
        }),
      ),
    );
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: /^Restore$/ }));
    expect(
      await screen.findByText(/Already in your library/i),
    ).toBeInTheDocument();
  });
});
