import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, test } from "vitest";

import type { components } from "@/api/schema";
import { DuplicatesPage } from "@/pages/duplicates/DuplicatesPage";
import { renderWithProviders } from "@/test/render";
import { server } from "@/test/msw-server";

type DuplicatesReport = components["schemas"]["DuplicatesReport"];
type DuplicateAlbum = components["schemas"]["DuplicateAlbum"];

const DUP_URL = `${window.location.origin}/api/duplicates`;
const RESOLVE_URL = `${window.location.origin}/api/duplicates/resolve`;

/** The `p-N` token on an element. THROWS when there is none: a lookup that
 * answers 0 or undefined turns the size assertion that uses it into a
 * tautology, which is how a vacuous class pin gets written. */
function paddingToken(el: Element | null | undefined): string {
  const found = /(?:^|\s)p-([\d.]+)(?:\s|$)/.exec(el?.className ?? "");
  if (found === null) throw new Error(`no p-* class on: ${el?.className ?? "null"}`);
  return found[1];
}

/** …in CSS px. Tailwind's spacing step is 0.25rem = 4px. */
function padding(el: Element | null | undefined): number {
  return Number(paddingToken(el)) * 4;
}

function album(overrides: Partial<DuplicateAlbum> = {}): DuplicateAlbum {
  return {
    id: 1,
    album_artist: "Radiohead",
    title: "In Rainbows",
    year: 2007,
    track_count: 10,
    genre: null,
    mb_albumid: null,
    format: "FLAC",
    bitrate_kbps: 900,
    folder: "/music/Radiohead/In Rainbows",
    is_suggested_keeper: true,
    ...overrides,
  };
}

function reportWithOneGroup(): DuplicatesReport {
  return {
    mode: "strict",
    group_count: 1,
    album_count: 2,
    groups: [
      {
        match_reason: "MusicBrainz album id",
        suggested_keeper_id: 1,
        members: [
          album({ id: 1, track_count: 10, format: "FLAC", is_suggested_keeper: true }),
          album({
            id: 2,
            track_count: 9,
            format: "MP3",
            bitrate_kbps: 320,
            folder: "/music/Radiohead/In Rainbows (1)",
            is_suggested_keeper: false,
          }),
        ],
      },
    ],
  };
}

const RESOLVE_ALL_URL = `${window.location.origin}/api/duplicates/resolve-all`;

function reportWithTwoGroups(): DuplicatesReport {
  return {
    mode: "strict",
    group_count: 2,
    album_count: 4,
    groups: [
      reportWithOneGroup().groups[0], // Radiohead / In Rainbows (ids 1,2)
      {
        match_reason: "MusicBrainz album id",
        suggested_keeper_id: 3,
        members: [
          album({ id: 3, album_artist: "Daft Punk", title: "Discovery", track_count: 14, is_suggested_keeper: true, folder: "/music/Daft Punk/Discovery" }),
          album({ id: 4, album_artist: "Daft Punk", title: "Discovery", track_count: 12, is_suggested_keeper: false, format: "MP3", bitrate_kbps: 320, folder: "/music/Daft Punk/Discovery (1)" }),
        ],
      },
    ],
  };
}

function renderPage() {
  return renderWithProviders(<DuplicatesPage />, { route: "/duplicates", path: "/duplicates" });
}

describe("DuplicatesPage", () => {
  test("renders a duplicate group with its members", async () => {
    server.use(http.get(DUP_URL, () => HttpResponse.json(reportWithOneGroup())));
    renderPage();
    expect(await screen.findByText(/Matched on/i)).toBeInTheDocument();
    expect(screen.getByText("most complete")).toBeInTheDocument();
    expect(screen.getAllByText("In Rainbows")).toHaveLength(2);
  });

  test("empty report shows the clean-library state", async () => {
    server.use(
      http.get(DUP_URL, () => HttpResponse.json({ mode: "strict", group_count: 0, album_count: 0, groups: [] })),
    );
    renderPage();
    expect(await screen.findByText(/No duplicate albums found/i)).toBeInTheDocument();
  });

  test("toggling to fuzzy refetches with mode=fuzzy", async () => {
    const seen: string[] = [];
    server.use(
      http.get(DUP_URL, ({ request }) => {
        seen.push(new URL(request.url).searchParams.get("mode") ?? "");
        return HttpResponse.json({ mode: "fuzzy", group_count: 0, album_count: 0, groups: [] });
      }),
    );
    const user = userEvent.setup();
    renderPage();
    await screen.findByText(/No duplicate albums found/i);
    await user.click(screen.getByRole("button", { name: /fuzzy/i }));
    await waitFor(() => expect(seen).toContain("fuzzy"));
  });

  test("resolve confirm posts keep + remove ids and clears the group", async () => {
    let body: unknown = null;
    let getCalls = 0;
    server.use(
      http.get(DUP_URL, () => {
        getCalls += 1;
        return HttpResponse.json(
          getCalls === 1
            ? reportWithOneGroup()
            : { mode: "strict", group_count: 0, album_count: 0, groups: [] },
        );
      }),
      http.post(RESOLVE_URL, async ({ request }) => {
        body = await request.json();
        return HttpResponse.json({ kept_album_id: 1, moved: [{ id: 2, album_artist: "Radiohead", title: "In Rainbows", trash_path: "/t" }] });
      }),
    );
    const user = userEvent.setup();
    renderPage();
    await screen.findByText(/Matched on/i);

    await user.click(screen.getByRole("button", { name: /move 1 to trash/i }));
    const dialog = await screen.findByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: /move to trash/i }));

    await waitFor(() => expect(body).toEqual({ mode: "strict", keep_album_id: 1, remove_album_ids: [2] }));
    await waitFor(() => expect(screen.queryByText(/Matched on/i)).not.toBeInTheDocument());
  });

  test("an import-active 409 surfaces the 'import running' message", async () => {
    server.use(
      http.get(DUP_URL, () => HttpResponse.json(reportWithOneGroup())),
      http.post(RESOLVE_URL, () => HttpResponse.json({ detail: "Import in progress" }, { status: 409 })),
    );
    const user = userEvent.setup();
    renderPage();
    await screen.findByText(/Matched on/i);
    await user.click(screen.getByRole("button", { name: /move 1 to trash/i }));
    const dialog = await screen.findByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: /move to trash/i }));
    expect(
      await screen.findByText(/can't resolve while an import is running/i),
    ).toBeInTheDocument();
  });

  test("a stale-group 409 shows the 'group changed' message and refetches the report", async () => {
    let getCalls = 0;
    server.use(
      http.get(DUP_URL, () => {
        getCalls += 1;
        return HttpResponse.json(reportWithOneGroup());
      }),
      http.post(RESOLVE_URL, () =>
        HttpResponse.json({ detail: "duplicate group membership changed" }, { status: 409 }),
      ),
    );
    const user = userEvent.setup();
    renderPage();
    await screen.findByText(/Matched on/i);
    const callsBeforeResolve = getCalls;
    await user.click(screen.getByRole("button", { name: /move 1 to trash/i }));
    const dialog = await screen.findByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: /move to trash/i }));
    expect(await screen.findByText(/this group changed/i)).toBeInTheDocument();
    // 409 invalidates ["duplicates"] so the report self-heals: at least one
    // extra GET fires after the failed resolve.
    await waitFor(() => expect(getCalls).toBeGreaterThan(callsBeforeResolve));
  });

  test("the Resolve-all button shows the total copy count (only with 2+ groups)", async () => {
    server.use(http.get(DUP_URL, () => HttpResponse.json(reportWithTwoGroups())));
    renderPage();
    expect(
      await screen.findByRole("button", { name: /resolve all · 2 copies/i }),
    ).toBeInTheDocument();
  });

  test("no Resolve-all button with a single group", async () => {
    server.use(http.get(DUP_URL, () => HttpResponse.json(reportWithOneGroup())));
    renderPage();
    await screen.findByText(/Matched on/i);
    expect(screen.queryByRole("button", { name: /resolve all/i })).not.toBeInTheDocument();
  });

  test("Resolve all posts every group's decision (honoring an override) and clears them", async () => {
    let body: unknown = null;
    let getCalls = 0;
    server.use(
      http.get(DUP_URL, () => {
        getCalls += 1;
        return HttpResponse.json(
          getCalls === 1
            ? reportWithTwoGroups()
            : { mode: "strict", group_count: 0, album_count: 0, groups: [] },
        );
      }),
      http.post(RESOLVE_ALL_URL, async ({ request }) => {
        body = await request.json();
        return HttpResponse.json({
          resolved: [
            { kept_album_id: 1, moved: [{ id: 2, album_artist: "Radiohead", title: "In Rainbows", trash_path: "/t" }] },
            { kept_album_id: 4, moved: [{ id: 3, album_artist: "Daft Punk", title: "Discovery", trash_path: "/t" }] },
          ],
          skipped_stale: [],
          group_count: 2,
          moved_count: 2,
        });
      }),
    );
    const user = userEvent.setup();
    renderPage();
    // Two groups → two "Matched on" rows, so wait on findAllByText.
    await screen.findAllByText(/Matched on/i);

    // Override group 2's keeper: pick the 12-track Daft Punk copy (id 4).
    await user.click(screen.getByRole("radio", { name: /Keep Discovery \(12 tracks, MP3 · 320k\)/i }));
    await user.click(screen.getByRole("button", { name: /resolve all · 2 copies/i }));
    const dialog = await screen.findByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: /move all to trash/i }));

    await waitFor(() =>
      expect(body).toEqual({
        mode: "strict",
        groups: [
          { keep_album_id: 1, remove_album_ids: [2] },
          { keep_album_id: 4, remove_album_ids: [3] },
        ],
      }),
    );
    await waitFor(() => expect(screen.queryByText(/Matched on/i)).not.toBeInTheDocument());
  });

  test("the summary flags groups the server skipped", async () => {
    server.use(
      http.get(DUP_URL, () => HttpResponse.json(reportWithTwoGroups())),
      http.post(RESOLVE_ALL_URL, () =>
        HttpResponse.json({
          resolved: [{ kept_album_id: 1, moved: [{ id: 2, album_artist: "Radiohead", title: "In Rainbows", trash_path: "/t" }] }],
          skipped_stale: [{ keep_album_id: 3, reason: "duplicate group membership changed" }],
          group_count: 1,
          moved_count: 1,
        }),
      ),
    );
    const user = userEvent.setup();
    renderPage();
    // Two groups → two "Matched on" rows, so wait on findAllByText.
    await screen.findAllByText(/Matched on/i);
    await user.click(screen.getByRole("button", { name: /resolve all/i }));
    const dialog = await screen.findByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: /move all to trash/i }));
    expect(await screen.findByText(/1 group changed and was skipped/i)).toBeInTheDocument();
  });

  test("the summary reports the playlists the bulk resolve re-exported", async () => {
    server.use(
      http.get(DUP_URL, () => HttpResponse.json(reportWithTwoGroups())),
      http.post(RESOLVE_ALL_URL, () =>
        HttpResponse.json({
          resolved: [{ kept_album_id: 1, moved: [{ id: 2, album_artist: "Radiohead", title: "In Rainbows", trash_path: "/t" }] }],
          skipped_stale: [],
          group_count: 2,
          moved_count: 2,
          playlists_reexported: 3,
        }),
      ),
    );
    const user = userEvent.setup();
    renderPage();
    await screen.findAllByText(/Matched on/i);
    await user.click(screen.getByRole("button", { name: /resolve all/i }));
    const dialog = await screen.findByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: /move all to trash/i }));

    // Its own sentence, trailing the move outcome it followed from.
    const note = await screen.findByText(/Moved 2 copies across 2 groups to Trash\./);
    expect(note).toHaveTextContent(
      "Moved 2 copies across 2 groups to Trash. Re-exported 3 playlists.",
    );
  });

  test("the re-export sentence sits between the move outcome and the skip caveat", async () => {
    // Both trailing clauses at once, so their ORDER is pinned — the code
    // comment in BulkResolveNote claims it, this proves it.
    server.use(
      http.get(DUP_URL, () => HttpResponse.json(reportWithTwoGroups())),
      http.post(RESOLVE_ALL_URL, () =>
        HttpResponse.json({
          resolved: [{ kept_album_id: 1, moved: [{ id: 2, album_artist: "Radiohead", title: "In Rainbows", trash_path: "/t" }] }],
          skipped_stale: [{ keep_album_id: 3, reason: "duplicate group membership changed" }],
          group_count: 1,
          moved_count: 1,
          playlists_reexported: 1,
        }),
      ),
    );
    const user = userEvent.setup();
    renderPage();
    await screen.findAllByText(/Matched on/i);
    await user.click(screen.getByRole("button", { name: /resolve all/i }));
    const dialog = await screen.findByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: /move all to trash/i }));

    const note = await screen.findByText(/Moved 1 copy across 1 group to Trash\./);
    expect(note).toHaveTextContent(
      "Moved 1 copy across 1 group to Trash. Re-exported 1 playlist. " +
        "1 group changed and was skipped; refreshed; re-check it.",
    );
  });

  test("the summary says nothing about re-exports when no playlist changed", async () => {
    server.use(
      http.get(DUP_URL, () => HttpResponse.json(reportWithTwoGroups())),
      http.post(RESOLVE_ALL_URL, () =>
        HttpResponse.json({
          resolved: [{ kept_album_id: 1, moved: [{ id: 2, album_artist: "Radiohead", title: "In Rainbows", trash_path: "/t" }] }],
          skipped_stale: [],
          group_count: 2,
          moved_count: 2,
          // Explicitly zero: the clause is dropped, never rendered as
          // "Re-exported 0 playlists."
          playlists_reexported: 0,
        }),
      ),
    );
    const user = userEvent.setup();
    renderPage();
    await screen.findAllByText(/Matched on/i);
    await user.click(screen.getByRole("button", { name: /resolve all/i }));
    const dialog = await screen.findByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: /move all to trash/i }));

    const note = await screen.findByText(/Moved 2 copies across 2 groups to Trash\./);
    expect(note).toHaveTextContent(/^Moved 2 copies across 2 groups to Trash\.$/);
    expect(note).not.toHaveTextContent(/re-exported/i);
  });

  test("an all-skipped batch leads with the skip notice, not 'moved 0'", async () => {
    // Every group drifted since the scan: a normal 200 with nothing moved. The
    // summary must surface the actionable skip, not read as a no-op success.
    server.use(
      http.get(DUP_URL, () => HttpResponse.json(reportWithTwoGroups())),
      http.post(RESOLVE_ALL_URL, () =>
        HttpResponse.json({
          resolved: [],
          skipped_stale: [
            { keep_album_id: 1, reason: "duplicate group membership changed" },
            { keep_album_id: 3, reason: "duplicate group membership changed" },
          ],
          group_count: 0,
          moved_count: 0,
        }),
      ),
    );
    const user = userEvent.setup();
    renderPage();
    await screen.findAllByText(/Matched on/i);
    await user.click(screen.getByRole("button", { name: /resolve all/i }));
    const dialog = await screen.findByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: /move all to trash/i }));
    expect(await screen.findByText(/nothing moved/i)).toBeInTheDocument();
    expect(screen.queryByText(/moved 0/i)).not.toBeInTheDocument();
  });

  test("renders the page h1 + live count meta via PageHeader", async () => {
    server.use(http.get(DUP_URL, () => HttpResponse.json(reportWithOneGroup())));
    renderPage();

    const h1 = await screen.findByRole("heading", { level: 1, name: "Duplicates" });
    expect(h1).toHaveAttribute("tabindex", "-1");
    expect(await screen.findByText("1 group · 2 albums")).toBeInTheDocument();
  });

  test("the mode control is the shared SegmentedControl (accent active, not inverted)", async () => {
    server.use(http.get(DUP_URL, () => HttpResponse.json(reportWithOneGroup())));
    renderPage();
    await screen.findByText(/Matched on/i);

    const strict = screen.getByRole("button", { name: /strict/i });
    expect(strict).toHaveAttribute("aria-pressed", "true");
    // The old ModeToggle's inverted bg-foreground active style is gone.
    expect(strict).not.toHaveClass("bg-foreground");
    expect(strict.closest('[data-slot="segmented-control"]')).not.toBeNull();
  });

  test("member rows render on AlbumRow with the cover thumb", async () => {
    server.use(http.get(DUP_URL, () => HttpResponse.json(reportWithOneGroup())));
    renderPage();
    await screen.findByText(/Matched on/i);

    // One cover per member, served from the album cover endpoint.
    const covers = document.querySelectorAll('img[data-slot="cover-art"]');
    expect(covers).toHaveLength(2);
    // ?size=thumb: AlbumRow renders these at size-10 (40 CSS px), so the
    // 320px WebP derivation is already 4x what the box needs at 2x DPI.
    expect(covers[0].getAttribute("src")).toBe("/api/albums/1/cover?size=thumb");
    // The quality/meta line folded into the row.
    expect(screen.getByText(/2007 · 10 tracks · FLAC · 900k/)).toBeInTheDocument();
    // The keeper radio and the row share grid row 1; the folder path is row 2
    // of the SAME column track (`col-start-2`), which is what keeps the radio
    // centred on the row it selects instead of on row+path — measured 17px of
    // drift when they shared one `items-center` box. jsdom computes no layout,
    // so the two classes are what a test can hold.
    const radio = screen.getByRole("radio", { name: /Keep In Rainbows \(10 tracks, FLAC · 900k\)/i });
    // `closest("li")`, not `parentElement`: the radio's parent is the <label>
    // that carries its tap target (below).
    const row = radio.closest("li");
    expect(row).toHaveClass("grid");
    expect(screen.getByTitle("/music/Radiohead/In Rainbows")).toHaveClass(
      "col-start-2",
    );
  });

  test("the keeper radio's tap target is a wrapping label of at least 24px", async () => {
    server.use(http.get(DUP_URL, () => HttpResponse.json(reportWithOneGroup())));
    renderPage();
    await screen.findByText(/Matched on/i);

    const radio = screen.getByRole("radio", { name: /Keep In Rainbows \(10 tracks, FLAC · 900k\)/i });
    const label = radio.parentElement;
    expect(label?.tagName).toBe("LABEL");
    // DERIVED, not a string match: the padding is what makes the target, and
    // the negative margin of the SAME size is what keeps the grid column where
    // it was. A native radio is 13px in Chromium and 16px is the widest UA box
    // we know of, so the floor is checked against 16.
    const pad = padding(label);
    expect(16 + 2 * pad).toBeGreaterThanOrEqual(24);
    // Token equality, not a substring: `className.toContain("-m-3")` is also
    // satisfied by `-m-3.5`, so the pull-back could stop matching the padding
    // and this would still pass.
    expect(label?.className.split(/\s+/)).toContain(`-m-${paddingToken(label)}`);
    // `relative`: without it AlbumRow, the later in-flow sibling, paints over
    // the part of the target that reaches past the margin box.
    expect(label).toHaveClass("relative");
  });

  test("the keeper radios of one group have DIFFERENT accessible names", async () => {
    // The realistic group: members of a duplicate group are copies of one
    // album, so the title and usually the track count are the same on every
    // option. Named "Keep <title> (<n> tracks)" they were indistinguishable to
    // a screen reader — two identical options, one destructive outcome.
    server.use(
      http.get(DUP_URL, () =>
        HttpResponse.json({
          mode: "strict",
          group_count: 1,
          album_count: 2,
          groups: [
            {
              match_reason: "MusicBrainz album id",
              suggested_keeper_id: 1,
              members: [
                album({ id: 1, track_count: 10, format: "FLAC", bitrate_kbps: 900 }),
                album({
                  id: 2,
                  track_count: 10,
                  format: "MP3",
                  bitrate_kbps: 320,
                  folder: "/music/Radiohead/In Rainbows (1)",
                  is_suggested_keeper: false,
                }),
              ],
            },
          ],
        }),
      ),
    );
    renderPage();
    await screen.findByText(/Matched on/i);

    const names = screen
      .getAllByRole("radio")
      .map((r) => r.getAttribute("aria-label") ?? "");
    expect(names).toHaveLength(2);
    expect(new Set(names).size).toBe(2);
    // What makes them different is the quality the row already shows — the
    // same string, not a second spelling of it.
    expect(names[0]).toContain("FLAC · 900k");
    expect(names[1]).toContain("MP3 · 320k");
  });

  test("a keeper radio's name has no quality clause rather than a placeholder", async () => {
    // The contract types both `format` and `bitrate_kbps` nullable. With both
    // null the name read "Keep In Rainbows (10 tracks, -)" and with only the
    // bitrate "(10 tracks, - · 320k)" — a placeholder voiced as "dash", read
    // out of Chromium's AX tree. The VISIBLE meta keeps its `-`.
    server.use(
      http.get(DUP_URL, () =>
        HttpResponse.json({
          mode: "strict",
          group_count: 1,
          album_count: 2,
          groups: [
            {
              match_reason: "MusicBrainz album id",
              suggested_keeper_id: 1,
              members: [
                album({ id: 1, format: null, bitrate_kbps: null }),
                album({
                  id: 2,
                  format: null,
                  bitrate_kbps: 320,
                  folder: "/music/Radiohead/In Rainbows (1)",
                  is_suggested_keeper: false,
                }),
              ],
            },
          ],
        }),
      ),
    );
    renderPage();
    await screen.findByText(/Matched on/i);

    const names = screen
      .getAllByRole("radio")
      .map((r) => r.getAttribute("aria-label") ?? "");
    // Nothing to say → the clause is absent. Bitrate only → the bitrate, with
    // no leading separator.
    expect(names).toEqual([
      "Keep In Rainbows (10 tracks)",
      "Keep In Rainbows (10 tracks, 320k)",
    ]);
    // The rows' own meta lines are unchanged: `-` is a visual convention, and
    // the quality that exists is spelled once, the same way in both places.
    expect(screen.getByText("2007 · 10 tracks · -")).toBeInTheDocument();
    expect(screen.getByText("2007 · 10 tracks · - · 320k")).toBeInTheDocument();
  });

  test("a failed scan renders the shared inline ErrorState", async () => {
    server.use(http.get(DUP_URL, () => new HttpResponse(null, { status: 500 })));
    renderPage();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveAttribute("data-slot", "error-state");
    expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument();
  });

  test("the clean-library state renders the shared EmptyState", async () => {
    server.use(
      http.get(DUP_URL, () =>
        HttpResponse.json({ mode: "strict", group_count: 0, album_count: 0, groups: [] }),
      ),
    );
    renderPage();

    await screen.findByText(/No duplicate albums found/i);
    expect(document.querySelector('[data-slot="empty-state"]')).not.toBeNull();
  });
});
