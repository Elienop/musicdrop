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

    // Seeded: entry 1 is matched, entry 2 is unresolved.
    expect(await screen.findByText(/1 matched/)).toBeInTheDocument();
    expect(screen.getByText(/1 unmatched/)).toBeInTheDocument();

    // Resolving entry 2's suggestion moves the tallies live (they no longer sit
    // frozen at the preview's counts).
    await userEvent.click(screen.getByRole("button", { name: /gone \(remaster\)/i }));
    expect(await screen.findByText(/2 matched/)).toBeInTheDocument();
    expect(screen.getByText(/0 unmatched/)).toBeInTheDocument();
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
        HttpResponse.json({ playlists: [{ name: "Road", track_count: 3 }] }),
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
            { name: "Road", track_count: 12 },
            { name: "Chill", track_count: 5 },
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
    expect(previewBody).toEqual({ plex_playlists: ["Road"] });
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
});
