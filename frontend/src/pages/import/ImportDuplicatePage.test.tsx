import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router";
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
      tracks: [],
    },
  ],
};

function renderDuplicateAt(route = "/import/albums/0/duplicate?job=job-1") {
  return renderWithProviders(<ImportDuplicatePage />, {
    route,
    path: "/import/albums/:index/duplicate",
  });
}

/** Mounts the duplicate page with seeded router state plus a /review probe. */
function renderWithOrigin(state?: unknown) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter
        initialEntries={[
          { pathname: "/import/albums/0/duplicate", search: "?job=job-1", state },
        ]}
      >
        <Routes>
          <Route
            path="/import/albums/:index/duplicate"
            element={<ImportDuplicatePage />}
          />
          <Route path="/review" element={<p>Review page probe</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
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
          tracks: [],
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

  test("entered from Review: back link reads Review and post-skip returns there", async () => {
    server.use(
      http.get(DUPLICATE_URL, () => HttpResponse.json(PROMPT)),
      http.post(DUPLICATE_URL, () => new HttpResponse(null, { status: 204 })),
    );
    const user = userEvent.setup();
    renderWithOrigin({ from: { label: "Review", to: "/review" } });

    const back = await screen.findByRole("link", { name: "Review" });
    expect(back).toHaveAttribute("href", "/review");

    await user.click(await screen.findByRole("button", { name: /skip new/i }));
    expect(await screen.findByText("Review page probe")).toBeInTheDocument();
  });

  test("without an origin the back link falls back to the job's import feed", async () => {
    server.use(http.get(DUPLICATE_URL, () => HttpResponse.json(PROMPT)));
    renderWithOrigin(undefined);

    const back = await screen.findByRole("link", { name: "Import" });
    expect(back).toHaveAttribute("href", "/import?job=job-1");
  });

  test("decision buttons carry no title tooltips — one visible footnote describes all three", async () => {
    server.use(http.get(DUPLICATE_URL, () => HttpResponse.json(PROMPT)));
    renderDuplicateAt();

    const keepBoth = await screen.findByRole("button", { name: /keep both/i });
    const replace = screen.getByRole("button", { name: /replace old/i });
    const merge = screen.getByRole("button", { name: /^merge/i });
    for (const button of [keepBoth, replace, merge]) {
      expect(button).not.toHaveAttribute("title");
      expect(button).toHaveAttribute("aria-describedby", "duplicate-footnote");
    }
    expect(keepBoth).toHaveAccessibleDescription(
      /keep both imports alongside the existing copy/i,
    );
    // The footnote is visible text, not a hover-only tooltip.
    expect(
      screen.getByText(/replace moves the old copy to trash/i),
    ).toBeVisible();
  });
});
