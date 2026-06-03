import { screen } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { describe, expect, test } from "vitest";

import type { components } from "@/api/schema";
import { SearchPage } from "@/pages/search/SearchPage";
import { renderWithProviders } from "@/test/render";
import { server } from "@/test/msw-server";

type SearchResults = components["schemas"]["SearchResults"];

const SEARCH_URL = `${window.location.origin}/api/search`;

function makeResults(overrides: Partial<SearchResults> = {}): SearchResults {
  return {
    artists: [{ name: "Radiohead", album_count: 9 }],
    albums: [
      {
        id: 1,
        album_artist: "Radiohead",
        title: "OK Computer",
        year: 1997,
        track_count: 12,
        genre: "Alternative Rock",
        mb_albumid: null,
      },
    ],
    tracks: [
      {
        id: 10,
        title: "Karma Police",
        artist: "Radiohead",
        album: "OK Computer",
        album_id: 1,
        duration_seconds: 261,
      },
    ],
    artist_total: 1,
    album_total: 1,
    track_total: 1,
    ...overrides,
  };
}

/** Render SearchPage at `/search?q=...` so `useSearchParams` resolves. */
function renderAt(route: string) {
  return renderWithProviders(<SearchPage />, { route, path: "/search" });
}

describe("SearchPage", () => {
  test("renders the three sections from a matched query", async () => {
    server.use(http.get(SEARCH_URL, () => HttpResponse.json(makeResults())));

    renderAt("/search?q=radio");

    // All three section headings render.
    expect(
      await screen.findByRole("heading", { name: /artists/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: /albums/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: /tracks/i }),
    ).toBeInTheDocument();
    // The artist card links to the artist page; the album + track hits surface.
    const artistLink = screen
      .getAllByRole("link")
      .find((a) => a.getAttribute("href") === "/artists/Radiohead");
    expect(artistLink).toBeDefined();
    expect(screen.getByText("OK Computer")).toBeInTheDocument();
    expect(screen.getByText("Karma Police")).toBeInTheDocument();
  });

  test("sends the query term to the API", async () => {
    let seenQ: string | null = null;
    server.use(
      http.get(SEARCH_URL, ({ request }) => {
        seenQ = new URL(request.url).searchParams.get("q");
        return HttpResponse.json(makeResults());
      }),
    );

    renderAt("/search?q=radio");

    await screen.findByText("Karma Police");
    expect(seenQ).toBe("radio");
  });

  test("shows '{shown} of {total}' per section when capped", async () => {
    server.use(
      http.get(SEARCH_URL, () =>
        HttpResponse.json(
          makeResults({ track_total: 87, album_total: 3, artist_total: 1 }),
        ),
      ),
    );

    renderAt("/search?q=radio");

    // One track shown of 87.
    expect(await screen.findByText(/1 of 87/)).toBeInTheDocument();
    expect(screen.getByText(/1 of 3/)).toBeInTheDocument();
  });

  test("a track's title links to its album page (and only the title)", async () => {
    server.use(http.get(SEARCH_URL, () => HttpResponse.json(makeResults())));

    renderAt("/search?q=radio");

    // The link's accessible name is exactly the title — not a run-on of
    // title/artist/album/duration.
    const link = await screen.findByRole("link", { name: "Karma Police" });
    expect(link).toHaveAttribute("href", "/albums/1");
    // The duration is NOT inside the track link.
    expect(link).not.toHaveTextContent("4:21");
  });

  test("a singleton track (no album_id) is not a link", async () => {
    server.use(
      http.get(SEARCH_URL, () =>
        HttpResponse.json(
          makeResults({
            artists: [],
            albums: [],
            tracks: [
              {
                id: 11,
                title: "Lonely Single",
                artist: "Someone",
                album: "",
                album_id: null,
                duration_seconds: 100,
              },
            ],
            artist_total: 0,
            album_total: 0,
            track_total: 1,
          }),
        ),
      ),
    );

    renderAt("/search?q=lonely");

    expect(await screen.findByText("Lonely Single")).toBeInTheDocument();
    // No link wraps it (singleton has no album page).
    expect(
      screen.queryByRole("link", { name: /Lonely Single/i }),
    ).not.toBeInTheDocument();
  });

  test("hides a section that has no hits", async () => {
    server.use(
      http.get(SEARCH_URL, () =>
        HttpResponse.json(
          makeResults({
            artists: [],
            albums: [],
            artist_total: 0,
            album_total: 0,
          }),
        ),
      ),
    );

    renderAt("/search?q=radio");

    await screen.findByText("Karma Police");
    // No Artists / Albums section headings when those lists are empty.
    expect(
      screen.queryByRole("heading", { name: /artists/i }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: /albums/i }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: /tracks/i }),
    ).toBeInTheDocument();
  });

  test("renders a 'Results for {q}' page heading at h1 level", async () => {
    server.use(http.get(SEARCH_URL, () => HttpResponse.json(makeResults())));

    renderAt("/search?q=radio");

    const h1 = await screen.findByRole("heading", { level: 1 });
    expect(h1).toHaveTextContent(/results for/i);
    expect(h1).toHaveTextContent("radio");
    // Section headings stay at h2.
    expect(
      screen.getByRole("heading", { level: 2, name: /artists/i }),
    ).toBeInTheDocument();
  });

  test("announces the result count in a single polite live region", async () => {
    server.use(
      http.get(SEARCH_URL, () =>
        HttpResponse.json(
          makeResults({ artist_total: 1, album_total: 1, track_total: 1 }),
        ),
      ),
    );

    const { container } = renderAt("/search?q=radio");

    await screen.findByText("Karma Police");
    const live = container.querySelectorAll('[aria-live="polite"]');
    // Exactly one polite live region (not one per section).
    expect(live).toHaveLength(1);
    // 1 artist + 1 album + 1 track shown = 3 results.
    expect(live[0]).toHaveTextContent(/3 results/i);
  });

  test("derives empty from the rendered arrays, not the server totals", async () => {
    // Pathological: totals claim hits but the arrays are empty. Must show the
    // no-results state, never a blank results area.
    server.use(
      http.get(SEARCH_URL, () =>
        HttpResponse.json(
          makeResults({
            artists: [],
            albums: [],
            tracks: [],
            artist_total: 5,
            album_total: 5,
            track_total: 5,
          }),
        ),
      ),
    );

    renderAt("/search?q=ghost");

    expect(await screen.findByText(/no results for/i)).toBeInTheDocument();
  });

  test("shows the idle prompt when q is blank", () => {
    // No handler needed — the query is disabled while q is empty.
    renderAt("/search?q=");

    expect(screen.getByText(/search your library/i)).toBeInTheDocument();
  });

  test("shows 'No results' when a non-blank query matches nothing", async () => {
    server.use(
      http.get(SEARCH_URL, () =>
        HttpResponse.json(
          makeResults({
            artists: [],
            albums: [],
            tracks: [],
            artist_total: 0,
            album_total: 0,
            track_total: 0,
          }),
        ),
      ),
    );

    renderAt("/search?q=zzz");

    expect(await screen.findByText(/no results for/i)).toBeInTheDocument();
    expect(screen.getByText(/zzz/)).toBeInTheDocument();
  });

  test("shows a loading state while the query is in flight", () => {
    server.use(
      http.get(
        SEARCH_URL,
        () => new Promise<HttpResponse<SearchResults>>(() => {}),
      ),
    );

    renderAt("/search?q=radio");

    expect(screen.getByRole("status")).toHaveTextContent(/searching/i);
  });

  test("shows an error state with a retry on a 500", async () => {
    server.use(
      http.get(SEARCH_URL, () => new HttpResponse(null, { status: 500 })),
    );

    renderAt("/search?q=radio");

    expect(
      await screen.findByText(/couldn.t run the search/i),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument();
  });
});
