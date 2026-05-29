import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, test } from "vitest";

import type { DuplicatePrompt } from "@/api/useImport";
import { ImportDuplicatePage } from "@/pages/import/ImportDuplicatePage";
import { renderWithProviders } from "@/test/render";
import { server } from "@/test/msw-server";

const DUPLICATE_URL = `${window.location.origin}/api/import/job-1/albums/0/duplicate`;

const PROMPT: DuplicatePrompt = {
  album_index: 0,
  incoming: {
    album_artist: "Radiohead",
    album: "In Rainbows",
    year: 2007,
    track_count: 10,
    format: "FLAC",
    bitrate_kbps: 900,
    folder: "/incoming/Radiohead - In Rainbows",
    has_current_art: false,
  },
  existing: [
    {
      album_id: 1,
      album_artist: "Radiohead",
      album: "In Rainbows",
      year: 2007,
      track_count: 9,
      format: "MP3",
      bitrate_kbps: 320,
      folder: "/music/Radiohead/In Rainbows",
    },
  ],
};

function renderDuplicateAt(route = "/import/albums/0/duplicate?job=job-1") {
  return renderWithProviders(<ImportDuplicatePage />, {
    route,
    path: "/import/albums/:index/duplicate",
  });
}

describe("ImportDuplicatePage", () => {
  test("renders new-vs-existing and posts the chosen action", async () => {
    let body: unknown = null;
    server.use(
      http.get(DUPLICATE_URL, () => HttpResponse.json(PROMPT)),
      http.post(DUPLICATE_URL, async ({ request }) => {
        body = await request.json();
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const user = userEvent.setup();
    renderDuplicateAt();

    // The new-vs-existing framing + both panels render.
    expect(
      await screen.findByText(/already in your library/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/Importing \(new\)/i)).toBeInTheDocument();
    // The existing copy's slimmer tracklist is surfaced.
    expect(screen.getByText(/9 tracks/i)).toBeInTheDocument();

    // Choosing "Replace old" posts { action: "replace" }.
    await user.click(screen.getByRole("button", { name: /replace old/i }));
    await waitFor(() => expect(body).toEqual({ action: "replace" }));
  });

  test("Merge posts { action: 'merge' }", async () => {
    let body: unknown = null;
    server.use(
      http.get(DUPLICATE_URL, () => HttpResponse.json(PROMPT)),
      http.post(DUPLICATE_URL, async ({ request }) => {
        body = await request.json();
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const user = userEvent.setup();
    renderDuplicateAt();

    await user.click(await screen.findByRole("button", { name: /merge/i }));
    await waitFor(() => expect(body).toEqual({ action: "merge" }));
  });

  test("on a successful decision it navigates back to the feed", async () => {
    server.use(
      http.get(DUPLICATE_URL, () => HttpResponse.json(PROMPT)),
      http.post(DUPLICATE_URL, () => new HttpResponse(null, { status: 204 })),
    );
    const user = userEvent.setup();
    renderDuplicateAt();

    // On screen first.
    expect(
      await screen.findByText(/already in your library/i),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /skip new/i }));

    // Navigating to /import?job=job-1 leaves the duplicate route (the harness
    // only mounts the duplicate page + a "/" landing), so the decision screen
    // unmounts — the heading disappears once the router has advanced.
    await waitFor(() =>
      expect(
        screen.queryByText(/already in your library/i),
      ).not.toBeInTheDocument(),
    );
  });

  test("renders one 'Already in library' panel per existing copy", async () => {
    const TWO_EXISTING: DuplicatePrompt = {
      ...PROMPT,
      existing: [
        PROMPT.existing[0],
        {
          album_id: 2,
          album_artist: "Radiohead",
          album: "In Rainbows (Disc 2)",
          year: 2007,
          track_count: 8,
          format: "FLAC",
          bitrate_kbps: 1000,
          folder: "/music/Radiohead/In Rainbows [2]",
        },
      ],
    };
    server.use(http.get(DUPLICATE_URL, () => HttpResponse.json(TWO_EXISTING)));
    renderDuplicateAt();

    await screen.findByText(/already in your library/i);
    expect(screen.getAllByText(/already in library/i)).toHaveLength(2);
  });

  test("shows a not-waiting notice on 404", async () => {
    server.use(
      http.get(DUPLICATE_URL, () =>
        HttpResponse.json({ detail: "Import album not found" }, { status: 404 }),
      ),
    );
    renderDuplicateAt();

    expect(await screen.findByText(/isn.?t waiting/i)).toBeInTheDocument();
  });
});
