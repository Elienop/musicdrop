import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, test } from "vitest";

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
});
