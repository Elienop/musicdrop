import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";
import { describe, expect, test } from "vitest";

import type { components } from "@/api/schema";
import { SearchPage } from "@/pages/search/SearchPage";
import { renderWithProviders } from "@/test/render";
import { server } from "@/test/msw-server";

type SearchResults = components["schemas"]["SearchResults"];
type TypedSearchPage = components["schemas"]["TypedSearchPage"];

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

/** Probe mounted at /albums/:albumId — prints the router-state origin, so
 * origin threading out of search links is assertable end-to-end. */
function ProbeAlbumPage() {
  const state = useLocation().state as
    | { from?: { label: string; to: string } }
    | null;
  return (
    <p>{state?.from ? `${state.from.label} → ${state.from.to}` : "no origin"}</p>
  );
}

function renderWithAlbumProbe(route: string) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[route]}>
        <Routes>
          <Route path="/search" element={<SearchPage />} />
          <Route path="/albums/:albumId" element={<ProbeAlbumPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

let lastQuery: URLSearchParams = new URLSearchParams();

/** Typed-mode handler (Task 1 contract): a `TypedSearchPage` with only
 * `tracks` populated with one per-offset row; `total` carries the full match
 * count; the other sections stay empty. */
function typedTracksHandler(total: number) {
  return http.get(SEARCH_URL, ({ request }) => {
    lastQuery = new URL(request.url).searchParams;
    const offset = Number(lastQuery.get("offset") ?? "0");
    const page: TypedSearchPage = {
      type: "tracks",
      artists: [],
      albums: [],
      tracks: [
        {
          id: 100 + offset,
          title: `Track ${offset + 1}`,
          artist: "Radiohead",
          album: "OK Computer",
          album_id: 1,
          duration_seconds: 200,
        },
      ],
      total,
      limit: 48,
      offset,
    };
    return HttpResponse.json(page);
  });
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
    // Section headings stay at h2. (find*: the h1 now exists from the loading
    // state on, so wait for the sections to land before asserting.)
    expect(
      await screen.findByRole("heading", { level: 2, name: /artists/i }),
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

  test("shows the idle prompt with the Search h1 when q is blank", () => {
    // No handler needed — the query is disabled while q is empty.
    renderAt("/search?q=");

    expect(
      screen.getByRole("heading", { level: 1, name: "Search" }),
    ).toBeInTheDocument();
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

    // The query echoes in the no-results message itself (the h1 "Results for
    // “zzz”" also contains it now, so scope the assertion to the message).
    const message = await screen.findByText(/no results for/i);
    expect(message).toBeInTheDocument();
    expect(message).toHaveTextContent("zzz");
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

  test("a capped section offers a 'View all' link into the typed view", async () => {
    server.use(
      http.get(SEARCH_URL, () =>
        HttpResponse.json(makeResults({ track_total: 87 })),
      ),
    );

    renderAt("/search?q=radio");

    const viewAll = await screen.findByRole("link", {
      name: /view all 87 tracks/i,
    });
    expect(viewAll).toHaveAttribute("href", "/search?q=radio&type=tracks");
    // Uncapped sections (1 of 1) offer no View-all.
    expect(
      screen.queryByRole("link", { name: /view all 1\b/i }),
    ).not.toBeInTheDocument();
  });
});

describe("SearchPage typed view (?type=)", () => {
  test("requests the typed page (type/limit/offset) and renders only that section", async () => {
    server.use(typedTracksHandler(87));

    renderAt("/search?q=radio&type=tracks");

    expect(await screen.findByText("Track 1")).toBeInTheDocument();
    expect(lastQuery.get("type")).toBe("tracks");
    expect(lastQuery.get("limit")).toBe("48");
    expect(lastQuery.get("offset")).toBe("0");
    // The count line names the typed total; no Artists/Albums sections.
    expect(screen.getByText("87 tracks")).toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: /artists/i }),
    ).not.toBeInTheDocument();
  });

  test("offers an 'All results' link back to the sectioned view", async () => {
    server.use(typedTracksHandler(87));

    renderAt("/search?q=radio&type=tracks");

    await screen.findByText("Track 1");
    const back = screen.getByRole("link", { name: /all results/i });
    expect(back).toHaveAttribute("href", "/search?q=radio");
  });

  test("paginates at 48, focusing the count line with a plain scroll", async () => {
    server.use(typedTracksHandler(87));

    renderAt("/search?q=radio&type=tracks");

    await screen.findByText("Track 1");
    await userEvent.click(screen.getByRole("button", { name: /next/i }));

    await waitFor(() => expect(lastQuery.get("offset")).toBe("48"));
    expect(await screen.findByText("Track 49")).toBeInTheDocument();
    expect(screen.getByText("87 tracks")).toHaveFocus();
    expect(window.scrollTo).toHaveBeenCalledWith({ top: 0 });
  });

  test("an out-of-range typed page offers a way back to the first page", async () => {
    server.use(
      http.get(SEARCH_URL, () => {
        const page: TypedSearchPage = {
          type: "tracks",
          artists: [],
          albums: [],
          tracks: [],
          total: 87,
          limit: 48,
          offset: 480,
        };
        return HttpResponse.json(page);
      }),
    );

    renderAt("/search?q=radio&type=tracks&offset=480");

    expect(
      await screen.findByText(/nothing on this page/i),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /first page/i }),
    ).toBeInTheDocument();
  });
});

describe("SearchPage origin threading", () => {
  test("a sectioned track link carries the Search origin in router state", async () => {
    server.use(http.get(SEARCH_URL, () => HttpResponse.json(makeResults())));

    renderWithAlbumProbe("/search?q=radio");

    await userEvent.click(
      await screen.findByRole("link", { name: "Karma Police" }),
    );
    expect(
      await screen.findByText("Search → /search?q=radio"),
    ).toBeInTheDocument();
  });

  test("a typed track link carries the typed URL in its origin", async () => {
    server.use(typedTracksHandler(87));

    renderWithAlbumProbe("/search?q=radio&type=tracks");

    await userEvent.click(
      await screen.findByRole("link", { name: "Track 1" }),
    );
    expect(
      await screen.findByText("Search → /search?q=radio&type=tracks"),
    ).toBeInTheDocument();
  });
});
