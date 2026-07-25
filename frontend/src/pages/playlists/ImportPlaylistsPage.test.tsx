import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, test, vi } from "vitest";

// The page toasts the import outcome; no Toaster is mounted in unit tests.
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));
import { toast } from "sonner";

import { ImportPlaylistsPage } from "@/pages/playlists/ImportPlaylistsPage";
import { renderWithProviders } from "@/test/render";
import { server } from "@/test/msw-server";

const origin = window.location.origin;
const PREVIEW_URL = `${origin}/api/playlists/import/preview`;
const COMMIT_URL = `${origin}/api/playlists/import`;
const PLEX_URL = `${origin}/api/plex/playlists`;

/** One library track offered as a match or suggestion. */
function summary(over: Partial<Record<string, unknown>> = {}) {
  return {
    item_id: 1,
    title: "Alpha",
    artist: "Band",
    album: "Record",
    duration_seconds: 200,
    ...over,
  };
}

/** One preview entry with its verdict. */
function entry(over: Partial<Record<string, unknown>> = {}) {
  return {
    position: 1,
    source: "Band - Alpha",
    artist: "Band",
    title: "Alpha",
    album: "Record",
    duration_seconds: 200,
    status: "matched",
    item_id: null,
    match: null,
    suggestions: [],
    ...over,
  };
}

function renderImport() {
  return renderWithProviders(<ImportPlaylistsPage />, {
    route: "/playlists/import",
    path: "/playlists/import",
  });
}

describe("ImportPlaylistsPage", () => {
  // The page lists Plex playlists as one of the two sources; default to an
  // empty list so the file-upload tests don't hit an unhandled request.
  beforeEach(() => {
    server.use(http.get(PLEX_URL, () => HttpResponse.json({ playlists: [] })));
    vi.mocked(toast.error).mockClear();
    vi.mocked(toast.success).mockClear();
  });

  test("surfaces a partial-import failure via a toast instead of dropping it silently", async () => {
    server.use(
      http.post(PREVIEW_URL, () =>
        HttpResponse.json({
          playlists: [
            { name: "Road", matched_count: 1, ambiguous_count: 0, unmatched_count: 0, entries: [entry()] },
          ],
        }),
      ),
      http.post(COMMIT_URL, () =>
        HttpResponse.json({ created: [], failed: [{ name: "Road", error: "store write failed" }] }),
      ),
    );
    renderImport();

    await userEvent.upload(
      screen.getByLabelText(/upload playlist files/i),
      new File(["#EXTM3U\nBand - Alpha\n"], "Road.m3u8"),
    );
    await userEvent.click(await screen.findByRole("button", { name: /import 1 playlist/i }));

    await waitFor(() => expect(vi.mocked(toast.error)).toHaveBeenCalled());
    expect(vi.mocked(toast.error).mock.calls[0][0]).toContain("Road");
    expect(vi.mocked(toast.success)).not.toHaveBeenCalled();
  });

  test("shows live match stats that follow resolutions", async () => {
    server.use(
      http.post(PREVIEW_URL, () =>
        HttpResponse.json({
          playlists: [
            {
              name: "Road",
              matched_count: 1,
              ambiguous_count: 1,
              unmatched_count: 0,
              entries: [
                entry({ position: 1, item_id: 7, match: summary({ item_id: 7 }) }),
                entry({
                  position: 2,
                  source: "Ghost - Gone",
                  artist: "Ghost",
                  title: "Gone",
                  album: "",
                  status: "ambiguous",
                  suggestions: [summary({ item_id: 9, title: "Gone (Remaster)" })],
                }),
              ],
            },
          ],
        }),
      ),
    );
    renderImport();

    const input = screen.getByLabelText(/upload playlist files/i);
    await userEvent.upload(
      input,
      new File(["#EXTM3U\nBand - Alpha\nGhost - Gone\n"], "Road.m3u8", {
        type: "audio/x-mpegurl",
      }),
    );

    // Seeded: entry 1 is matched, entry 2 is unresolved. The matched tally is a
    // muted span in the header; the unmatched count rides the "Needs attention"
    // filter label that now sits in the summary row beside it.
    expect(await screen.findByText(/1 matched/)).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Needs attention (1)" }),
    ).toBeInTheDocument();

    // Resolving entry 2's suggestion moves the tallies live (they no longer sit
    // frozen at the preview's counts).
    await userEvent.click(screen.getByRole("button", { name: /gone \(remaster\)/i }));
    expect(await screen.findByText(/2 matched/)).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Needs attention (0)" }),
    ).toBeInTheDocument();
  });

  test("an entry leads with its own track identity, not just the origin string", async () => {
    server.use(
      http.post(PREVIEW_URL, () =>
        HttpResponse.json({
          playlists: [
            {
              name: "UK Pop Fever",
              matched_count: 0,
              ambiguous_count: 0,
              unmatched_count: 2,
              entries: [
                // A Plex pull: every entry's source is just "plex:<playlist>",
                // so without the title/artist line the user can't tell WHAT
                // track failed to match (or what to search for).
                entry({
                  position: 1,
                  source: "plex:UK Pop Fever",
                  artist: "Erasure",
                  title: "Chains of Love",
                  album: "The Innocents",
                  duration_seconds: 170,
                  status: "unmatched",
                }),
                // A bare-path m3u line (no EXTINF): the source IS the identity.
                entry({
                  position: 2,
                  source: "Music/one.mp3",
                  artist: null,
                  title: null,
                  album: null,
                  duration_seconds: null,
                  status: "unmatched",
                }),
              ],
            },
          ],
        }),
      ),
    );
    renderImport();

    await userEvent.upload(
      screen.getByLabelText(/upload playlist files/i),
      new File(["#EXTM3U\n"], "UK Pop Fever.m3u8"),
    );

    // Titled entry: title · artist · duration visible; origin demoted to tooltip.
    expect(await screen.findByText("Chains of Love")).toBeInTheDocument();
    expect(screen.getByText(/Erasure/)).toBeInTheDocument();
    expect(screen.getByText(/2:50/)).toBeInTheDocument();
    expect(screen.getByTitle("plex:UK Pop Fever")).toBeInTheDocument();
    expect(screen.queryByText("plex:UK Pop Fever")).not.toBeInTheDocument();
    // Untitled entry still falls back to its raw source line.
    expect(screen.getByText("Music/one.mp3")).toBeInTheDocument();
  });

  test("accepting a suggestion upgrades the entry before commit", async () => {
    server.use(
      http.post(PREVIEW_URL, () =>
        HttpResponse.json({
          playlists: [
            {
              name: "Road",
              matched_count: 0,
              ambiguous_count: 1,
              unmatched_count: 0,
              entries: [
                entry({
                  status: "ambiguous",
                  suggestions: [
                    summary({ item_id: 11, title: "Alpha (Live)" }),
                    summary({ item_id: 22, title: "Alpha (Remix)" }),
                  ],
                }),
              ],
            },
          ],
        }),
      ),
    );
    let committed: unknown = null;
    server.use(
      http.post(COMMIT_URL, async ({ request }) => {
        committed = await request.json();
        return HttpResponse.json({ created: [] });
      }),
    );
    renderImport();

    await userEvent.upload(
      screen.getByLabelText(/upload playlist files/i),
      new File(["#EXTM3U\nBand - Alpha\n"], "Road.m3u8"),
    );

    await userEvent.click(await screen.findByRole("button", { name: /alpha \(remix\)/i }));
    await userEvent.click(screen.getByRole("button", { name: /import 1 playlist/i }));

    await waitFor(() => expect(committed).not.toBeNull());
    expect(committed).toMatchObject({
      playlists: [{ entries: [{ item_id: 22 }] }],
    });
  });

  test("commits unmatched entries as pending with their source", async () => {
    server.use(
      http.post(PREVIEW_URL, () =>
        HttpResponse.json({
          playlists: [
            {
              name: "Road",
              matched_count: 0,
              ambiguous_count: 0,
              unmatched_count: 1,
              entries: [
                entry({
                  source: "Ghost - Track",
                  artist: "Ghost",
                  title: "Track",
                  album: "Album X",
                  duration_seconds: 222,
                  status: "unmatched",
                }),
              ],
            },
          ],
        }),
      ),
    );
    let committed: unknown = null;
    server.use(
      http.post(COMMIT_URL, async ({ request }) => {
        committed = await request.json();
        return HttpResponse.json({ created: [] });
      }),
    );
    renderImport();

    await userEvent.upload(
      screen.getByLabelText(/upload playlist files/i),
      new File(["#EXTM3U\nGhost - Track\n"], "Road.m3u8"),
    );

    await userEvent.click(await screen.findByRole("button", { name: /import 1 playlist/i }));

    await waitFor(() => expect(committed).not.toBeNull());
    expect(committed).toMatchObject({
      playlists: [
        {
          entries: [
            {
              pending: {
                artist: "Ghost",
                title: "Track",
                album: "Album X",
                duration_seconds: 222,
                source: "Ghost - Track",
              },
            },
          ],
        },
      ],
    });
  });

  test("keeps same-named playlists' resolutions apart by position", async () => {
    // Two playlists share the name "Road" in ONE preview (e.g. two Road.m3u8
    // files from different folders). Name-keyed state collapses them in a Map:
    // resolving the first playlist's entry would leak onto the second. Index
    // keying keeps them apart.
    server.use(
      http.post(PREVIEW_URL, () =>
        HttpResponse.json({
          playlists: [
            {
              name: "Road",
              matched_count: 0,
              ambiguous_count: 1,
              unmatched_count: 0,
              entries: [
                entry({
                  position: 1,
                  source: "Band - Alpha",
                  status: "ambiguous",
                  suggestions: [
                    summary({ item_id: 11, title: "Alpha (Live)" }),
                    summary({ item_id: 22, title: "Alpha (Remix)" }),
                  ],
                }),
              ],
            },
            {
              name: "Road",
              matched_count: 0,
              ambiguous_count: 0,
              unmatched_count: 1,
              entries: [
                entry({
                  position: 1,
                  source: "Ghost - Gone",
                  artist: "Ghost",
                  title: "Gone",
                  album: "",
                  duration_seconds: 200,
                  status: "unmatched",
                }),
              ],
            },
          ],
        }),
      ),
    );
    let committed: unknown = null;
    server.use(
      http.post(COMMIT_URL, async ({ request }) => {
        committed = await request.json();
        return HttpResponse.json({ created: [] });
      }),
    );
    renderImport();

    await userEvent.upload(
      screen.getByLabelText(/upload playlist files/i),
      new File(["#EXTM3U\nBand - Alpha\n"], "Road.m3u8"),
    );

    // Resolve ONLY the first playlist's entry (its "Alpha (Remix)" suggestion).
    await userEvent.click(await screen.findByRole("button", { name: /alpha \(remix\)/i }));
    await userEvent.click(screen.getByRole("button", { name: /import 2 playlists/i }));

    await waitFor(() => expect(committed).not.toBeNull());
    // First playlist resolves to the picked item; the second — a different
    // playlist that only happens to share the name — commits as pending.
    expect(committed).toMatchObject({
      playlists: [
        { entries: [{ item_id: 22 }] },
        { entries: [{ pending: { title: "Gone", artist: "Ghost" } }] },
      ],
    });
    // And the leak the bug produced must be gone: the second entry has no item_id.
    expect((committed as { playlists: { entries: unknown[] }[] }).playlists[1].entries[0]).not.toHaveProperty(
      "item_id",
    );
  });

  test("surfaces a file-read failure through the page error path", async () => {
    // A File whose .text() rejects (e.g. a revoked/unreadable handle) must not
    // vanish silently — the page shows a recoverable message.
    const originalText = File.prototype.text;
    File.prototype.text = () => Promise.reject(new Error("unreadable"));
    try {
      renderImport();
      await userEvent.upload(
        screen.getByLabelText(/upload playlist files/i),
        new File(["#EXTM3U\nBand - Alpha\n"], "Road.m3u8"),
      );
      expect(
        await screen.findByText(/couldn't read the selected files/i),
      ).toBeInTheDocument();
    } finally {
      File.prototype.text = originalText;
    }
  });

  test("surfaces a failed preview from the Plex source path", async () => {
    server.use(
      http.get(PLEX_URL, () =>
        HttpResponse.json({
          playlists: [{ name: "Road", track_count: 3, rating_key: "11" }],
        }),
      ),
    );
    server.use(
      http.post(PREVIEW_URL, () =>
        HttpResponse.json({ detail: "Plex went away" }, { status: 502 }),
      ),
    );
    renderImport();

    await userEvent.click(await screen.findByRole("checkbox", { name: /road/i }));
    await userEvent.click(screen.getByRole("button", { name: /preview .*plex/i }));

    expect(await screen.findByText(/plex went away/i)).toBeInTheDocument();
  });

  test("imports from Plex via the playlist picker", async () => {
    server.use(
      http.get(PLEX_URL, () =>
        HttpResponse.json({
          playlists: [
            { name: "Road", track_count: 12, rating_key: "11" },
            { name: "Chill", track_count: 5, rating_key: "22" },
          ],
        }),
      ),
    );
    let previewBody: unknown = null;
    server.use(
      http.post(PREVIEW_URL, async ({ request }) => {
        previewBody = await request.json();
        return HttpResponse.json({
          playlists: [
            {
              name: "Road",
              matched_count: 1,
              ambiguous_count: 0,
              unmatched_count: 0,
              entries: [entry({ item_id: 7, match: summary({ item_id: 7 }) })],
            },
          ],
        });
      }),
    );
    renderImport();

    await userEvent.click(await screen.findByRole("checkbox", { name: /road/i }));
    await userEvent.click(screen.getByRole("button", { name: /preview .*plex/i }));

    await waitFor(() => expect(previewBody).not.toBeNull());
    // Selection travels by Plex IDENTITY (ratingKey), never by title.
    expect(previewBody).toEqual({ plex_rating_keys: ["11"] });
  });

  test("two same-titled Plex playlists stay independently selectable", async () => {
    // Plex allows duplicate titles. Keyed by name, one of these two could never
    // be picked (and picking either pulled the same shadowed playlist).
    server.use(
      http.get(PLEX_URL, () =>
        HttpResponse.json({
          playlists: [
            { name: "Road", track_count: 12, rating_key: "11" },
            { name: "Road", track_count: 5, rating_key: "22" },
          ],
        }),
      ),
    );
    let previewBody: unknown = null;
    server.use(
      http.post(PREVIEW_URL, async ({ request }) => {
        previewBody = await request.json();
        return HttpResponse.json({ playlists: [] });
      }),
    );
    renderImport();

    // Duplicate titles get a disambiguated label so both rows are addressable.
    const first = await screen.findByRole("checkbox", { name: /road.*12 tracks/i });
    const second = screen.getByRole("checkbox", { name: /road.*5 tracks/i });
    await userEvent.click(first);
    await userEvent.click(second);

    // Both are checked at once — a name-keyed selection would collapse them.
    expect(first).toBeChecked();
    expect(second).toBeChecked();

    await userEvent.click(screen.getByRole("button", { name: /preview 2 from plex/i }));
    await waitFor(() => expect(previewBody).not.toBeNull());
    expect(previewBody).toEqual({ plex_rating_keys: ["11", "22"] });
  });

  test("picking the SECOND of two same-titled Plex playlists sends only its key", async () => {
    // The shadowing bug's sharpest edge: the later duplicate used to be the only
    // one reachable by title — now each is reachable on its own.
    server.use(
      http.get(PLEX_URL, () =>
        HttpResponse.json({
          playlists: [
            { name: "Road", track_count: 12, rating_key: "11" },
            { name: "Road", track_count: 5, rating_key: "22" },
          ],
        }),
      ),
    );
    let previewBody: unknown = null;
    let committed: unknown = null;
    server.use(
      http.post(PREVIEW_URL, async ({ request }) => {
        previewBody = await request.json();
        return HttpResponse.json({
          playlists: [
            {
              name: "Road",
              matched_count: 1,
              ambiguous_count: 0,
              unmatched_count: 0,
              entries: [entry({ item_id: 7, match: summary({ item_id: 7 }) })],
            },
          ],
        });
      }),
      http.post(COMMIT_URL, async ({ request }) => {
        committed = await request.json();
        return HttpResponse.json({ created: [] });
      }),
    );
    renderImport();

    await userEvent.click(await screen.findByRole("checkbox", { name: /road.*5 tracks/i }));
    await userEvent.click(screen.getByRole("button", { name: /preview 1 from plex/i }));

    await waitFor(() => expect(previewBody).not.toBeNull());
    expect(previewBody).toEqual({ plex_rating_keys: ["22"] });

    await userEvent.click(await screen.findByRole("button", { name: /import 1 playlist/i }));
    await waitFor(() => expect(committed).not.toBeNull());
    // The commit stamps the SAME identity the preview was pulled with.
    expect(committed).toMatchObject({
      playlists: [{ name: "Road", plex_source: "Road", plex_rating_key: "22" }],
    });
  });

  test("a multi-playlist Plex commit stamps each playlist's OWN rating key", async () => {
    // plexKeys is an ARRAY aligned to the preview list by index — that alignment
    // is the whole reason it isn't a single value, and a commit is the only place
    // it is observable. If it ever slipped, each playlist would be stamped with
    // its neighbour's Plex identity and later seed the WRONG cover.
    server.use(
      http.get(PLEX_URL, () =>
        HttpResponse.json({
          playlists: [
            { name: "Alpha", track_count: 1, rating_key: "11" },
            { name: "Beta", track_count: 1, rating_key: "22" },
          ],
        }),
      ),
    );
    let previewBody: unknown = null;
    let committed: unknown = null;
    server.use(
      http.post(PREVIEW_URL, async ({ request }) => {
        previewBody = await request.json();
        return HttpResponse.json({
          playlists: ["Alpha", "Beta"].map((name) => ({
            name,
            matched_count: 1,
            ambiguous_count: 0,
            unmatched_count: 0,
            entries: [entry({ item_id: 7, match: summary({ item_id: 7 }) })],
          })),
        });
      }),
      http.post(COMMIT_URL, async ({ request }) => {
        committed = await request.json();
        return HttpResponse.json({ created: [] });
      }),
    );
    renderImport();

    await userEvent.click(await screen.findByRole("checkbox", { name: "Alpha" }));
    await userEvent.click(screen.getByRole("checkbox", { name: "Beta" }));
    await userEvent.click(screen.getByRole("button", { name: /preview 2 from plex/i }));
    await waitFor(() => expect(previewBody).not.toBeNull());
    expect(previewBody).toEqual({ plex_rating_keys: ["11", "22"] });

    await userEvent.click(await screen.findByRole("button", { name: /import 2 playlists/i }));
    await waitFor(() => expect(committed).not.toBeNull());
    expect(committed).toMatchObject({
      playlists: [
        { name: "Alpha", plex_source: "Alpha", plex_rating_key: "11" },
        { name: "Beta", plex_source: "Beta", plex_rating_key: "22" },
      ],
    });
  });

  test("same-titled Plex playlists with EQUAL track counts still read distinctly", async () => {
    // The track-count qualifier ties in the commonest duplicate shape (a playlist
    // copied verbatim), so an ordinal breaks the tie — otherwise both checkboxes
    // carry a byte-identical accessible name and neither can be addressed.
    server.use(
      http.get(PLEX_URL, () =>
        HttpResponse.json({
          playlists: [
            { name: "Road", track_count: 5, rating_key: "11" },
            { name: "Road", track_count: 5, rating_key: "22" },
          ],
        }),
      ),
    );
    let previewBody: unknown = null;
    server.use(
      http.post(PREVIEW_URL, async ({ request }) => {
        previewBody = await request.json();
        return HttpResponse.json({ playlists: [] });
      }),
    );
    renderImport();

    const second = await screen.findByRole("checkbox", { name: /road.*5 tracks.*#2/i });
    expect(screen.getByRole("checkbox", { name: /road.*5 tracks.*#1/i })).not.toBe(second);
    await userEvent.click(second);
    await userEvent.click(screen.getByRole("button", { name: /preview 1 from plex/i }));

    await waitFor(() => expect(previewBody).not.toBeNull());
    expect(previewBody).toEqual({ plex_rating_keys: ["22"] });  // the one actually clicked
  });

  test("positions render 1-based even though the backend sends 0-based", async () => {
    server.use(
      http.post(PREVIEW_URL, () =>
        HttpResponse.json({
          playlists: [
            {
              name: "Road",
              matched_count: 0,
              ambiguous_count: 0,
              unmatched_count: 1,
              entries: [entry({ position: 0, status: "unmatched" })],
            },
          ],
        }),
      ),
    );
    renderImport();
    await userEvent.upload(
      screen.getByLabelText(/upload playlist files/i),
      new File(["#EXTM3U\nBand - Alpha\n"], "Road.m3u8"),
    );

    const row = (await screen.findByText("Alpha")).closest("li");
    expect(row).not.toBeNull();
    expect(within(row as HTMLElement).getByText("1")).toBeInTheDocument();
    expect(within(row as HTMLElement).queryByText("0")).not.toBeInTheDocument();
  });

  test("the badge follows the live resolution, not the frozen preview status", async () => {
    server.use(
      http.post(PREVIEW_URL, () =>
        HttpResponse.json({
          playlists: [
            {
              name: "Road",
              matched_count: 0,
              ambiguous_count: 1,
              unmatched_count: 0,
              entries: [
                entry({
                  status: "ambiguous",
                  suggestions: [summary({ item_id: 11, title: "Alpha (Live)" })],
                }),
              ],
            },
          ],
        }),
      ),
    );
    renderImport();
    await userEvent.upload(
      screen.getByLabelText(/upload playlist files/i),
      new File(["#EXTM3U\nBand - Alpha\n"], "Road.m3u8"),
    );

    const row = (await screen.findByText("Alpha")).closest("li") as HTMLElement;
    expect(within(row).getByText("Ambiguous")).toBeInTheDocument();

    // Accepting the chip flips the badge to Matched and shows the inline
    // → confirmation (frozen entry.status would still say ambiguous — the whole
    // point of the live derivation).
    await userEvent.click(within(row).getByRole("button", { name: /alpha \(live\)/i }));
    // The resolved row left the needs-attention default view — flip to All and
    // re-grab it (the pre-click element is detached once the filter drops it).
    await userEvent.click(screen.getByRole("button", { name: "All (1)" }));
    const rowAfter = (await screen.findByText("Alpha")).closest("li") as HTMLElement;
    expect(within(rowAfter).getByText("Matched")).toBeInTheDocument();
    expect(within(rowAfter).queryByText("Ambiguous")).not.toBeInTheDocument();
    expect(within(rowAfter).getByText("Alpha (Live)", { selector: "span" })).toBeInTheDocument();
  });

  test("a suggestion-less row is a single line with Search inline; chips get a second line", async () => {
    server.use(
      http.post(PREVIEW_URL, () =>
        HttpResponse.json({
          playlists: [
            {
              name: "Road",
              matched_count: 0,
              ambiguous_count: 1,
              unmatched_count: 1,
              entries: [
                entry({ position: 0, status: "unmatched" }),
                entry({
                  position: 1,
                  source: "Ghost - Gone",
                  artist: "Ghost",
                  title: "Gone",
                  status: "ambiguous",
                  suggestions: [summary({ item_id: 9, title: "Gone (Remaster)" })],
                }),
              ],
            },
          ],
        }),
      ),
    );
    renderImport();
    await userEvent.upload(
      screen.getByLabelText(/upload playlist files/i),
      new File(["#EXTM3U\nBand - Alpha\nGhost - Gone\n"], "Road.m3u8"),
    );

    // The bare unmatched row: no "No library match" filler, no ↳ chips line —
    // just its identity, an inline Search escape, and the badge.
    const bare = (await screen.findByText("Alpha")).closest("li") as HTMLElement;
    expect(within(bare).queryByText(/no library match/i)).not.toBeInTheDocument();
    expect(within(bare).queryByText("↳")).not.toBeInTheDocument();
    expect(within(bare).getByRole("button", { name: /search/i })).toBeInTheDocument();

    // The chip row keeps its second line, marked by the ↳ affordance.
    const chips = (await screen.findByText("Gone")).closest("li") as HTMLElement;
    expect(within(chips).getByText("↳")).toBeInTheDocument();
    expect(within(chips).getByRole("button", { name: /gone \(remaster\)/i })).toBeInTheDocument();
  });

  test("defaults to needs-attention: resolved rows hide until All is selected", async () => {
    server.use(
      http.post(PREVIEW_URL, () =>
        HttpResponse.json({
          playlists: [
            {
              name: "Road",
              matched_count: 1,
              ambiguous_count: 0,
              unmatched_count: 1,
              entries: [
                entry({ position: 0, item_id: 7, match: summary({ item_id: 7 }) }),
                entry({
                  position: 1,
                  source: "Ghost - Gone",
                  artist: "Ghost",
                  title: "Gone",
                  status: "unmatched",
                }),
              ],
            },
          ],
        }),
      ),
    );
    renderImport();
    await userEvent.upload(
      screen.getByLabelText(/upload playlist files/i),
      new File(["#EXTM3U\nBand - Alpha\nGhost - Gone\n"], "Road.m3u8"),
    );

    // Default view: only the unresolved row; the seeded match is one click away.
    expect(await screen.findByText("Gone")).toBeInTheDocument();
    expect(screen.queryByText("Alpha")).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Needs attention (1)", pressed: true }),
    ).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "All (2)" }));
    // The seeded match reappears under All. Its title "Alpha" renders twice —
    // the row identity plus the "→ Alpha · Band" confirmation (the seeded
    // match's own title) — so match all rather than a bare (throwing) findByText.
    expect(await screen.findAllByText("Alpha")).not.toHaveLength(0);
    expect(screen.getByText("Gone")).toBeInTheDocument();
  });

  test("resolving a row removes it from the needs-attention view, counts follow", async () => {
    server.use(
      http.post(PREVIEW_URL, () =>
        HttpResponse.json({
          playlists: [
            {
              name: "Road",
              matched_count: 0,
              ambiguous_count: 1,
              unmatched_count: 0,
              entries: [
                entry({
                  status: "ambiguous",
                  suggestions: [summary({ item_id: 11, title: "Alpha (Live)" })],
                }),
              ],
            },
          ],
        }),
      ),
    );
    renderImport();
    await userEvent.upload(
      screen.getByLabelText(/upload playlist files/i),
      new File(["#EXTM3U\nBand - Alpha\n"], "Road.m3u8"),
    );

    await userEvent.click(await screen.findByRole("button", { name: /alpha \(live\)/i }));

    // The row left the default view and the empty state took its place; the
    // toggle labels carry the live counts.
    expect(screen.queryByText("Alpha", { selector: ".font-medium" })).not.toBeInTheDocument();
    expect(screen.getByText(/switch to all to see the entries/i)).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Needs attention (0)", pressed: true }),
    ).toBeInTheDocument();

    // The resolved row is still reachable — and re-pickable — under All.
    await userEvent.click(screen.getByRole("button", { name: "All (1)" }));
    expect(await screen.findByText("Alpha")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /alpha \(live\)/i })).toBeInTheDocument();
  });

  // ——— Editable playlist name (Task 7) ———

  test("an edited playlist name lands in the commit body", async () => {
    server.use(
      http.post(PREVIEW_URL, () =>
        HttpResponse.json({
          playlists: [
            {
              name: "Road",
              matched_count: 1,
              ambiguous_count: 0,
              unmatched_count: 0,
              entries: [entry({ item_id: 7, match: summary({ item_id: 7 }) })],
            },
          ],
        }),
      ),
    );
    let committed: unknown = null;
    server.use(
      http.post(COMMIT_URL, async ({ request }) => {
        committed = await request.json();
        return HttpResponse.json({ created: [] });
      }),
    );
    renderImport();

    await userEvent.upload(
      screen.getByLabelText(/upload playlist files/i),
      new File(["#EXTM3U\nBand - Alpha\n"], "Road.m3u8"),
    );

    // Pencil → input → save, the detail-page rename idiom.
    await userEvent.click(await screen.findByRole("button", { name: /rename road/i }));
    const input = screen.getByRole("textbox", { name: /playlist name/i });
    await userEvent.clear(input);
    await userEvent.type(input, "Highway");
    await userEvent.click(screen.getByRole("button", { name: /save name/i }));

    // The renamed title shows on the card…
    expect(await screen.findByText("Highway")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: /import 1 playlist/i }));
    await waitFor(() => expect(committed).not.toBeNull());
    // …and the edit is what commits.
    expect(committed).toMatchObject({ playlists: [{ name: "Highway" }] });
  });

  test("an empty name edit falls back to the original name", async () => {
    server.use(
      http.post(PREVIEW_URL, () =>
        HttpResponse.json({
          playlists: [
            {
              name: "Road",
              matched_count: 1,
              ambiguous_count: 0,
              unmatched_count: 0,
              entries: [entry({ item_id: 7, match: summary({ item_id: 7 }) })],
            },
          ],
        }),
      ),
    );
    let committed: unknown = null;
    server.use(
      http.post(COMMIT_URL, async ({ request }) => {
        committed = await request.json();
        return HttpResponse.json({ created: [] });
      }),
    );
    renderImport();

    await userEvent.upload(
      screen.getByLabelText(/upload playlist files/i),
      new File(["#EXTM3U\nBand - Alpha\n"], "Road.m3u8"),
    );

    await userEvent.click(await screen.findByRole("button", { name: /rename road/i }));
    await userEvent.clear(screen.getByRole("textbox", { name: /playlist name/i }));
    await userEvent.click(screen.getByRole("button", { name: /save name/i }));

    // The blank edit is rejected — the card keeps the original title.
    expect(await screen.findByText("Road")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: /import 1 playlist/i }));
    await waitFor(() => expect(committed).not.toBeNull());
    expect(committed).toMatchObject({ playlists: [{ name: "Road" }] });
  });

  test("a Plex import carries the ORIGINAL bare title as plex_source, even after a rename", async () => {
    server.use(
      http.get(PLEX_URL, () =>
        HttpResponse.json({
          playlists: [{ name: "UK Pop Fever", track_count: 1, rating_key: "77" }],
        }),
      ),
      http.post(PREVIEW_URL, () =>
        HttpResponse.json({
          playlists: [
            {
              name: "UK Pop Fever",
              matched_count: 1,
              ambiguous_count: 0,
              unmatched_count: 0,
              entries: [entry({ item_id: 7, match: summary({ item_id: 7 }) })],
            },
          ],
        }),
      ),
    );
    let committed: unknown = null;
    server.use(
      http.post(COMMIT_URL, async ({ request }) => {
        committed = await request.json();
        return HttpResponse.json({ created: [] });
      }),
    );
    renderImport();

    await userEvent.click(await screen.findByRole("checkbox", { name: /uk pop fever/i }));
    await userEvent.click(screen.getByRole("button", { name: /preview .*plex/i }));

    // Rename it to something the user prefers…
    await userEvent.click(await screen.findByRole("button", { name: /rename uk pop fever/i }));
    const input = screen.getByRole("textbox", { name: /playlist name/i });
    await userEvent.clear(input);
    await userEvent.type(input, "My Mix");
    await userEvent.click(screen.getByRole("button", { name: /save name/i }));

    await userEvent.click(screen.getByRole("button", { name: /import 1 playlist/i }));
    await waitFor(() => expect(committed).not.toBeNull());
    // The new name commits, but plex_source stays the exact original Plex title
    // (a decorated "plex:<name>" would silently import artless).
    expect(committed).toMatchObject({
      playlists: [
        // plex_rating_key is the identity the backend resolves the poster by;
        // plex_source stays the display/back-compat title.
        { name: "My Mix", plex_source: "UK Pop Fever", plex_rating_key: "77" },
      ],
    });
  });

  test("a file import sets no plex_source", async () => {
    server.use(
      http.post(PREVIEW_URL, () =>
        HttpResponse.json({
          playlists: [
            {
              name: "Road",
              matched_count: 1,
              ambiguous_count: 0,
              unmatched_count: 0,
              entries: [entry({ item_id: 7, match: summary({ item_id: 7 }) })],
            },
          ],
        }),
      ),
    );
    let committed: unknown = null;
    server.use(
      http.post(COMMIT_URL, async ({ request }) => {
        committed = await request.json();
        return HttpResponse.json({ created: [] });
      }),
    );
    renderImport();

    await userEvent.upload(
      screen.getByLabelText(/upload playlist files/i),
      new File(["#EXTM3U\nBand - Alpha\n"], "Road.m3u8"),
    );
    await userEvent.click(await screen.findByRole("button", { name: /import 1 playlist/i }));
    await waitFor(() => expect(committed).not.toBeNull());
    const playlist = (committed as { playlists: Record<string, unknown>[] }).playlists[0];
    expect(playlist).not.toHaveProperty("plex_source");
    expect(playlist).not.toHaveProperty("plex_rating_key");
  });

  // ——— Filter toggle in the header row (Task 7) ———

  test("the filter toggle sits in the summary and switches views without collapsing the card", async () => {
    server.use(
      http.post(PREVIEW_URL, () =>
        HttpResponse.json({
          playlists: [
            {
              name: "Road",
              matched_count: 1,
              ambiguous_count: 0,
              unmatched_count: 1,
              entries: [
                entry({ position: 0, title: "Kept", artist: "A", item_id: 7, match: summary({ item_id: 7, title: "Kept" }) }),
                entry({ position: 1, source: "Ghost - Gone", title: "Gone", artist: "B", status: "unmatched" }),
              ],
            },
          ],
        }),
      ),
    );
    const { container } = renderImport();

    await userEvent.upload(
      screen.getByLabelText(/upload playlist files/i),
      new File(["#EXTM3U\nBand - Alpha\nGhost - Gone\n"], "Road.m3u8"),
    );

    const details = (await screen.findByText("Gone")).closest("details") as HTMLDetailsElement;
    const summaryEl = details.querySelector("summary") as HTMLElement;
    // The filter now lives inside the summary row, next to the matched tally.
    expect(within(summaryEl).getByRole("group", { name: /filter entries/i })).toBeInTheDocument();
    expect(within(summaryEl).getByText(/1 matched/)).toBeInTheDocument();

    // Open the card, then switch to All from the header toggle: the matched row
    // appears and the card stays open (the preventDefault keeps the click from
    // toggling the <details> collapse).
    await userEvent.click(within(summaryEl).getByText("Road"));
    expect(details.open).toBe(true);
    expect(screen.queryByText("Kept")).not.toBeInTheDocument();

    await userEvent.click(within(summaryEl).getByRole("button", { name: "All (2)" }));
    // "Kept" renders twice under All — the row identity plus its "→ Kept"
    // matched confirmation — so match all rather than a bare (throwing) query.
    expect(await screen.findAllByText("Kept")).not.toHaveLength(0);
    expect(details.open).toBe(true);
    // The old toolbar band between the summary and the list is gone.
    expect(container.querySelectorAll('[data-slot="segmented-control"]')).toHaveLength(1);
  });
});
