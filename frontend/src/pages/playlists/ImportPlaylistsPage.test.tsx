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

  test("uploads files, previews, and shows match stats", async () => {
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
                entry({ position: 1, item_id: 7, match: summary({ item_id: 7 }) }),
                entry({
                  position: 2,
                  source: "Ghost - Gone",
                  artist: "Ghost",
                  title: "Gone",
                  album: "",
                  status: "unmatched",
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

    expect(await screen.findByText(/1 matched/)).toBeInTheDocument();
    expect(screen.getByText(/1 unmatched/)).toBeInTheDocument();
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
