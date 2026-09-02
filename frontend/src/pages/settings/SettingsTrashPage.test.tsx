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

/** The backend writes three distinct why-sentences for an IMPORT row
 * (`_NO_RECORD_NOTE` / `_SHARED_FOLDER_NOTE` / `_OUTSIDE_LIBRARY_NOTE`) and a
 * fourth for the `refused` mode, which is `REFUSED_NOTE` below — four
 * sentences out of `trash_manage._restore_fields`, of which this file's
 * fixtures carry two. The page must render whatever arrives, so the tests
 * carry one VERBATIM rather than a shape — a component that ignored
 * `restore_note` and printed its own copy would pass a shape assertion and
 * fail this one.
 *
 * It is NOT a contract — it is a hand-kept COPY of a runtime string that never
 * crosses the OpenAPI schema, so nothing across the boundary can check it. This
 * copy has gone stale twice (the backend widened the sentence to admit a failed
 * write, then widened it again to admit an unusable record, and nothing here
 * noticed, because the fixture defines its own input). Refresh it by hand
 * whenever `trash_manage._NO_RECORD_NOTE` changes. */
const NO_RECORD_NOTE =
  "MusicDrop has no usable record of where this came from: it may predate origin" +
  " records, its record may have failed to write, or that record may be unusable now" +
  " (the server log says which). Restoring re-imports it, so beets files it under your" +
  " current naming rules rather than putting it back.";

/** The other arm. Shared, because several tests need to assert that the two
 * arms differ — an assertion one fixture cannot make on its own. */
const importedAlbum: TrashedAlbum = {
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

/** The third arm, and the only one that takes a control away. Verbatim from
 * `trash_manage._SYMLINKED_ENTRY_NOTE` for the same reason the import note is:
 * a page that ignored `restore_note` and printed its own copy would satisfy a
 * shape assertion. Same caveat too: this is a hand-kept COPY, not a contract.
 * The assertions around it are regexes over fragments (`/will not restore it/`,
 * `/own Empty both refuse it/`, `/never moved/`), so they keep passing while
 * this drifts — and they did: this fixture sat a rewording behind the backend
 * for a whole slice with nothing red. Refresh it by hand whenever
 * `trash_manage._SYMLINKED_ENTRY_NOTE` changes; the comment beside that string
 * asks for the same thing from the other side.
 *
 * The rest of the shape is not decoration. Every symlinked entry MusicDrop
 * itself creates is an album's own FOLDER, which reaches the listing through
 * `_audio_free_entries` (os.walk does not follow the link, so it produces no
 * audio group) and therefore arrives with track_count 0 and null
 * artist/album/format/origin. This fixture is that row.
 *
 * It is not the only refused row the backend CAN send — a top-level link to a
 * media file is walked as a file and lists refused with real tags and a track
 * count (measured: ('linked.flac', 'refused', 1)). MusicDrop's own trashing
 * moves a FOLDER — `trash._album_root`, or the container it makes for a shared
 * one — so it does not produce that row. The two take the same HEDGE arm —
 * `noTracks` is false for a refused row whatever its track count — but they do
 * not render alike: the META line above it reads `track_count`, so the same
 * fixture with track_count 1 / format FLAC renders "1 track · FLAC" where this
 * one falls back to its folder name (measured, on the same "Can't be restored."
 * arm and with no tags hint either way). So the fixture stays the shape the app
 * produces, and what that costs is one line — a meta line with tags on a
 * REFUSED row; `album` and `importedAlbum` render that line with tags here. */
const REFUSED_NOTE =
  "This Trash entry is a link to a folder or file elsewhere, so MusicDrop will not" +
  " restore it — following the link would import files that were never in Trash. The" +
  " album's own files were never moved: they are still where the link points, and" +
  " adding that folder through Import is what puts the album back in the library." +
  " Restore and this row's own Empty both refuse it; Empty all removes the link, and" +
  " only the link.";

const refusedAlbum: TrashedAlbum = {
  folder: "Symlinked Album",
  album_artist: null,
  album: null,
  year: null,
  track_count: 0,
  format: null,
  restore_mode: "refused",
  restore_note: REFUSED_NOTE,
  origin: null,
};

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

  test("Restore posts the folder and shows the restored result in the calm tone", async () => {
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
    const message = await screen.findByText(/Restored to your library/i);
    expect(message).toBeInTheDocument();
    // The other half of "a refusal must not read like a success": the refusal
    // is asserted amber elsewhere, so a tone that is ALWAYS amber would pass
    // that test alone. A success has to be positively calm.
    expect(message).toHaveClass("text-muted-foreground");
    expect(message).not.toHaveClass("text-warning");
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
    server.use(
      http.get(TRASH_URL, () =>
        HttpResponse.json({ albums: [importedAlbum], trash_path: "/t" }),
      ),
    );
    renderPage();

    const restore = await screen.findByRole("button", { name: /^Restore$/ });
    expect(restore).toBeEnabled();
    expect(screen.getByText("Approximate restore.")).toBeInTheDocument();
    expect(screen.getByText(NO_RECORD_NOTE)).toBeInTheDocument();
    // origin survives an import row so the user can put it back by hand.
    expect(screen.getByText("/old-library/Old Band/Demos")).toBeInTheDocument();
    // A distinctive FRAGMENT, not the whole sentence and not a phrase the
    // backend is likely to reword: this assertion pinned "no record of where
    // this came from" and silently stopped matching the day the backend widened
    // it to "no usable record of…". What it needs to prove is that the note
    // reaches the button's accessible description at all.
    expect(restore).toHaveAccessibleDescription(/where this came from/i);
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

  test("a row that has tracks is not told its tags were unreadable", async () => {
    // The other direction of the same guard, and the direction nothing pinned:
    // dropping `album.track_count === 0` from it left the whole frontend suite
    // green (measured on this branch: 1338 passed) while this row rendered
    // "2 tracks · FLAC · 1994" directly above "MusicDrop couldn't read audio
    // tags here — that's why there are no track details". The hint explains an
    // EMPTY meta line; on a row that has one it contradicts what it sits under.
    server.use(
      http.get(TRASH_URL, () =>
        HttpResponse.json({ albums: [album], trash_path: "/t" }),
      ),
    );
    renderPage();

    const restore = await screen.findByRole("button", { name: /^Restore$/ });
    expect(screen.getByText("2 tracks · FLAC · 1994")).toBeInTheDocument();
    expect(
      screen.queryByText(/couldn.t read audio tags here/i),
    ).not.toBeInTheDocument();
    expect(restore).not.toHaveAccessibleDescription(/no track details/i);
    // Positive control for the matcher: this row IS described — by its outlook
    // line — so the assertion above cannot be passing on an empty description.
    expect(restore).toHaveAccessibleDescription(/Goes back to/);
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
    // The qualifier is the difference between this message and a wrong one: an
    // EMPTY folder at the origin is replaced and the restore goes ahead
    // (`trash_manage._occupied`), so "exists again" on its own would send the
    // user to clear something that was never the blocker.
    expect(message).toHaveTextContent(/with anything in it/i);
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

  test("a restore that never landed says why instead of going quiet", async () => {
    // The backend refuses a restore with a 503 when the music share is not
    // mounted, and words the reason itself. Nothing read `restore.isError`, so
    // the button just stopped spinning and the row was byte-identical to
    // before the click — the sentence existed and could not reach anyone.
    server.use(
      http.get(TRASH_URL, () =>
        HttpResponse.json({ albums: [album], trash_path: "/t" }),
      ),
      http.post(RESTORE_URL, () =>
        HttpResponse.json(
          { detail: "The music library isn’t readable. Is the share mounted?" },
          { status: 503 },
        ),
      ),
    );
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: /^Restore$/ }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/Is the share mounted\?/i);
    // Destructive, not the amber a refusal gets: the request failed outright.
    expect(alert).toHaveClass("text-destructive");
    // And the row is still usable — a failure must not strand the control.
    expect(screen.getByRole("button", { name: /^Restore$/ })).toBeEnabled();
  });

  test("retrying after a refusal drops the stale outcome", async () => {
    // Two answers to one click is worse than one: the amber "already there"
    // from the first attempt reads as current beside the new red failure.
    let call = 0;
    server.use(
      http.get(TRASH_URL, () =>
        HttpResponse.json({ albums: [album], trash_path: "/t" }),
      ),
      http.post(RESTORE_URL, () => {
        call += 1;
        return call === 1
          ? HttpResponse.json({
              restored: false,
              reason: "origin_occupied",
              album_id: null,
            })
          : HttpResponse.json({ detail: "Server fell over" }, { status: 500 });
      }),
    );
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: /^Restore$/ }));
    expect(
      await screen.findByText(/original folder exists again/i),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /^Restore$/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      /Server fell over/,
    );
    expect(
      screen.queryByText(/original folder exists again/i),
    ).not.toBeInTheDocument();
  });

  test("the approximate row is marked by an icon AND colour, the exact row by neither", async () => {
    // A note that communicates by colour alone is not acceptable, and one that
    // communicates by words alone is not glanceable. Both arms are asserted
    // because dropping the icon leaves the words behind and looks fine.
    const outlookOf = (button: HTMLElement) =>
      document.getElementById(
        (button.getAttribute("aria-describedby") ?? "").split(" ")[0],
      );

    server.use(
      http.get(TRASH_URL, () =>
        HttpResponse.json({ albums: [importedAlbum], trash_path: "/t" }),
      ),
    );
    const { unmount } = renderPage();
    const importOutlook = outlookOf(
      await screen.findByRole("button", { name: /^Restore$/ }),
    );
    const icon = importOutlook?.querySelector("svg");
    expect(icon).toBeInTheDocument();
    expect(icon).toHaveAttribute("aria-hidden", "true");
    expect(screen.getByText("Approximate restore.")).toHaveClass(
      "text-warning",
    );
    unmount();

    server.use(
      http.get(TRASH_URL, () =>
        HttpResponse.json({ albums: [album], trash_path: "/t" }),
      ),
    );
    renderPage();
    const exactOutlook = outlookOf(
      await screen.findByRole("button", { name: /^Restore$/ }),
    );
    expect(exactOutlook?.querySelector("svg")).toBeNull();
    expect(screen.getByText("Exact restore.")).toHaveClass("text-foreground");
  });

  test("both arms' origin paths can break, so a long one cannot pan the row", async () => {
    // jsdom computes no layout, so this pins the classes the 320px browser
    // measurement depends on — one per arm, since they are separate elements
    // and a mutant can drop either alone.
    server.use(
      http.get(TRASH_URL, () =>
        HttpResponse.json({
          albums: [album, importedAlbum],
          trash_path: "/t",
        }),
      ),
    );
    renderPage();

    expect(await screen.findByText("/music/2 Brothers/Dreams")).toHaveClass(
      "break-words",
    );
    expect(screen.getByText("/old-library/Old Band/Demos")).toHaveClass(
      "break-words",
    );
  });

  test("the header points at the per-row promise instead of making one", async () => {
    // The second assertion is a BLACKLIST of one phrasing: a paraphrase that
    // makes the same false promise ("Restore puts one back exactly as it was")
    // renders and passes here untouched. What it pins is the pair — that the
    // header defers to the row, and that the one wording which actually
    // shipped, and was wrong for every `import` row, cannot come back.
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
  test("a refused row offers neither of its own controls and says why", async () => {
    // Both per-row routes refuse the symlink LEXICALLY — on the child being a
    // link, before resolving it and without ever following it — and answer 404
    // before doing any work, so a live Restore and a live Empty here can only
    // produce an error. Disabled — not hidden: the row still has to read as one
    // that HAS these controls, or "cannot be restored" looks like a missing UI.
    server.use(
      http.get(TRASH_URL, () =>
        HttpResponse.json({ albums: [refusedAlbum], trash_path: "/t" }),
      ),
    );
    renderPage();

    const restore = await screen.findByRole("button", { name: /^Restore$/ });
    const empty = screen.getByRole("button", { name: /Empty Symlinked Album/i });
    expect(restore).toBeDisabled();
    expect(empty).toBeDisabled();
    // The reason is visible, in the same place the import note is shown...
    expect(screen.getByText(REFUSED_NOTE)).toBeInTheDocument();
    // ...and reaches BOTH controls, not only the one it renders beside.
    expect(restore).toHaveAccessibleDescription(/will not restore it/i);
    expect(empty).toHaveAccessibleDescription(/own Empty both refuse it/i);
    // The heading must not keep promising a restore that cannot happen.
    expect(screen.getByText("Can’t be restored.")).toBeInTheDocument();
    expect(screen.queryByText("Approximate restore.")).not.toBeInTheDocument();
    // Empty all is the ONE route that clears this entry: never disabled here.
    expect(screen.getByRole("button", { name: "Empty all" })).toBeEnabled();
  });

  test("a refused row disables its own controls and nothing else's", async () => {
    // The other half of the claim above. Asserted in ONE listing, per row, so a
    // mutant that disables every row (or none) fails here rather than passing
    // three single-row tests that each only ever look at their own arm.
    server.use(
      http.get(TRASH_URL, () =>
        HttpResponse.json({
          albums: [album, importedAlbum, refusedAlbum],
          trash_path: "/t",
        }),
      ),
    );
    renderPage();
    await screen.findByText(/2 Brothers - Dreams/);

    const rowOf = (title: RegExp) => {
      const row = screen.getByText(title).closest("li");
      if (!row) throw new Error(`no row for ${title.source}`);
      return row;
    };
    const controls = (title: RegExp) => ({
      restore: within(rowOf(title)).getByRole("button", { name: /^Restore$/ }),
      empty: within(rowOf(title)).getByRole("button", { name: /^Empty / }),
    });

    const exact = controls(/2 Brothers - Dreams/);
    const imported = controls(/Old Band - Demos/);
    const refused = controls(/Unknown artist - Symlinked Album/);

    expect(exact.restore).toBeEnabled();
    expect(exact.empty).toBeEnabled();
    expect(imported.restore).toBeEnabled();
    expect(imported.empty).toBeEnabled();
    expect(refused.restore).toBeDisabled();
    expect(refused.empty).toBeDisabled();
  });

  test("a refused row drops the zero-track hedge both its buttons would make false", async () => {
    // The hedge goes on `refused` alone, not on the track count: "Restore may
    // still work; Empty removes it permanently" is two promises under two
    // disabled buttons, and it names the wrong cause — nothing under the link
    // was unreadable, nothing under it was read — so the backend's note is left
    // to explain the row. The fixture is zero-track because the rows MusicDrop
    // itself creates are (see `REFUSED_NOTE`), which makes this the rendered
    // default rather than a corner; a refused row that did arrive with tracks
    // takes the same arm, by `album.track_count === 0` rather than by `!refused`.
    server.use(
      http.get(TRASH_URL, () =>
        HttpResponse.json({ albums: [refusedAlbum], trash_path: "/t" }),
      ),
    );
    renderPage();

    const restore = await screen.findByRole("button", { name: /^Restore$/ });
    expect(
      screen.queryByText(/couldn.t read audio tags here/i),
    ).not.toBeInTheDocument();
    expect(restore).not.toHaveAccessibleDescription(/may still work/i);
    // Positive control for the matcher itself: without it the assertion above
    // also passes on a button that is described by nothing at all.
    expect(restore).toHaveAccessibleDescription(/never moved/i);
  });
});
