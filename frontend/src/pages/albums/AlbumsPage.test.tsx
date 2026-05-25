import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, useLocation, useNavigate } from "react-router";
import { describe, expect, test } from "vitest";

import type { components } from "@/api/schema";
import { AlbumsPage } from "@/pages/albums/AlbumsPage";
import { renderWithProviders } from "@/test/render";
import { server } from "@/test/msw-server";

/** Mirrors the current URL search string into the DOM so tests can assert that
 * grid state (offset) round-trips through the URL. */
function LocationProbe() {
  const { search } = useLocation();
  return <div data-testid="location-search">{search}</div>;
}

/** Render AlbumsPage under a MemoryRouter started at `route`, with a probe that
 * exposes the live URL search string. */
function renderAtUrl(route: string, props: Parameters<typeof AlbumsPage>[0] = {}) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[route]}>
        <AlbumsPage {...props} />
        <LocationProbe />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

type AlbumPage = components["schemas"]["AlbumPage"];

const ALBUMS_URL = `${window.location.origin}/api/albums`;

function makePage(overrides: Partial<AlbumPage> = {}): AlbumPage {
  return {
    items: [
      {
        id: 1,
        album_artist: "Radiohead",
        title: "OK Computer",
        year: 1997,
        track_count: 12,
        genre: "Alternative Rock",
      },
      {
        id: 2,
        album_artist: "Aphex Twin",
        title: "Selected Ambient Works 85-92",
        year: 1992,
        track_count: 13,
        genre: "Electronic",
      },
    ],
    total: 2,
    limit: 50,
    offset: 0,
    ...overrides,
  };
}

describe("AlbumsPage", () => {
  test("renders album cards from the API", async () => {
    server.use(http.get(ALBUMS_URL, () => HttpResponse.json(makePage())));

    renderWithProviders(<AlbumsPage />);

    expect(await screen.findByText("OK Computer")).toBeInTheDocument();
    expect(screen.getByText("Radiohead")).toBeInTheDocument();
    expect(screen.getByText("Selected Ambient Works 85-92")).toBeInTheDocument();
    expect(screen.getByText("Aphex Twin")).toBeInTheDocument();
    // Track count + year surfaced.
    expect(screen.getByText("12 tracks")).toBeInTheDocument();
    expect(screen.getByText("1997")).toBeInTheDocument();
    // Total count line.
    expect(screen.getByText(/2 albums/i)).toBeInTheDocument();
  });

  test("wraps each card in a link to its detail page", async () => {
    server.use(http.get(ALBUMS_URL, () => HttpResponse.json(makePage())));

    renderWithProviders(<AlbumsPage />);

    await screen.findByText("OK Computer");

    // Whole card is a single accessible link, named by the album, pointing at
    // the detail route.
    const ok = screen.getByRole("link", { name: /OK Computer/i });
    expect(ok).toHaveAttribute("href", "/albums/1");

    const ambient = screen.getByRole("link", {
      name: /Selected Ambient Works 85-92/i,
    });
    expect(ambient).toHaveAttribute("href", "/albums/2");
  });

  test("renders a cover image per album with the right /cover src", async () => {
    server.use(http.get(ALBUMS_URL, () => HttpResponse.json(makePage())));

    renderWithProviders(<AlbumsPage />);

    const ok = await screen.findByAltText("OK Computer cover");
    expect(ok).toHaveAttribute("src", "/api/albums/1/cover");
    expect(ok).toHaveAttribute("loading", "lazy");

    const ambient = screen.getByAltText(
      "Selected Ambient Works 85-92 cover",
    );
    expect(ambient).toHaveAttribute("src", "/api/albums/2/cover");
  });

  test("falls back to a placeholder when the cover image errors", async () => {
    server.use(http.get(ALBUMS_URL, () => HttpResponse.json(makePage())));

    renderWithProviders(<AlbumsPage />);

    const ok = await screen.findByAltText("OK Computer cover");
    // Simulate the 404 / broken-image path.
    fireEvent.error(ok);

    // The <img> is gone; a labelled placeholder takes its place.
    expect(
      screen.queryByAltText("OK Computer cover"),
    ).not.toBeInTheDocument();
    expect(
      screen.getByLabelText("OK Computer cover unavailable"),
    ).toBeInTheDocument();
  });

  test("shows the empty state when total is 0", async () => {
    server.use(
      http.get(ALBUMS_URL, () =>
        HttpResponse.json(makePage({ items: [], total: 0 })),
      ),
    );

    renderWithProviders(<AlbumsPage />);

    expect(await screen.findByText(/no albums/i)).toBeInTheDocument();
  });

  test("shows an error state with a retry on a 500", async () => {
    server.use(
      http.get(ALBUMS_URL, () => new HttpResponse(null, { status: 500 })),
    );

    renderWithProviders(<AlbumsPage />);

    expect(
      await screen.findByText(/couldn.t load albums/i),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /retry/i }),
    ).toBeInTheDocument();
  });

  test("retry refetches after a transient error", async () => {
    let calls = 0;
    server.use(
      http.get(ALBUMS_URL, () => {
        calls += 1;
        if (calls === 1) {
          return new HttpResponse(null, { status: 500 });
        }
        return HttpResponse.json(makePage());
      }),
    );

    renderWithProviders(<AlbumsPage />);

    const retry = await screen.findByRole("button", { name: /retry/i });
    await userEvent.click(retry);

    expect(await screen.findByText("OK Computer")).toBeInTheDocument();
  });

  test("paginates with Next/Prev when total exceeds the page size", async () => {
    server.use(
      http.get(ALBUMS_URL, ({ request }) => {
        const url = new URL(request.url);
        const offset = Number(url.searchParams.get("offset") ?? "0");
        const item = {
          id: offset === 0 ? 1 : 2,
          album_artist: offset === 0 ? "First Artist" : "Second Artist",
          title: offset === 0 ? "First Album" : "Second Album",
          year: 2000,
          track_count: 10,
          genre: null,
        };
        return HttpResponse.json({
          items: [item],
          total: 2,
          limit: 1,
          offset,
        });
      }),
    );

    renderWithProviders(<AlbumsPage initialLimit={1} />);

    expect(await screen.findByText("First Album")).toBeInTheDocument();
    const prev = screen.getByRole("button", { name: /previous/i });
    expect(prev).toBeDisabled();

    await userEvent.click(screen.getByRole("button", { name: /next/i }));

    expect(await screen.findByText("Second Album")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /previous/i })).toBeEnabled();
    expect(screen.getByRole("button", { name: /next/i })).toBeDisabled();
  });

  test("writes the offset to the URL when paging Next", async () => {
    server.use(
      http.get(ALBUMS_URL, ({ request }) => {
        const offset = Number(
          new URL(request.url).searchParams.get("offset") ?? "0",
        );
        return HttpResponse.json({
          items: [
            {
              id: offset === 0 ? 1 : 2,
              album_artist: "A",
              title: offset === 0 ? "First Album" : "Second Album",
              year: 2000,
              track_count: 10,
              genre: null,
            },
          ],
          total: 2,
          limit: 1,
          offset,
        });
      }),
    );

    renderAtUrl("/", { initialLimit: 1 });

    await screen.findByText("First Album");
    expect(screen.getByTestId("location-search")).toHaveTextContent("");

    await userEvent.click(screen.getByRole("button", { name: /next/i }));

    await screen.findByText("Second Album");
    expect(screen.getByTestId("location-search")).toHaveTextContent("offset=1");
  });

  test("restores the grid page from the URL offset on mount", async () => {
    server.use(
      http.get(ALBUMS_URL, ({ request }) => {
        const offset = Number(
          new URL(request.url).searchParams.get("offset") ?? "0",
        );
        return HttpResponse.json({
          items: [
            {
              id: offset === 0 ? 1 : 2,
              album_artist: "A",
              title: offset === 0 ? "First Album" : "Second Album",
              year: 2000,
              track_count: 10,
              genre: null,
            },
          ],
          total: 2,
          limit: 1,
          offset,
        });
      }),
    );

    // Deep-linking to ?offset=1 should land directly on the second page.
    renderAtUrl("/?offset=1", { initialLimit: 1 });

    expect(await screen.findByText("Second Album")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /previous/i })).toBeEnabled();
  });

  test("threads ?artist= into the API query and shows a filter banner", async () => {
    let seenArtist: string | null = null;
    server.use(
      http.get(ALBUMS_URL, ({ request }) => {
        seenArtist = new URL(request.url).searchParams.get("artist");
        return HttpResponse.json(
          makePage({
            items: [
              {
                id: 1,
                album_artist: "Radiohead",
                title: "OK Computer",
                year: 1997,
                track_count: 12,
                genre: "Alternative Rock",
              },
            ],
            total: 1,
          }),
        );
      }),
    );

    renderAtUrl("/?artist=Radiohead");

    expect(await screen.findByText("OK Computer")).toBeInTheDocument();
    // The artist filter reached the backend.
    expect(seenArtist).toBe("Radiohead");
    // Banner announcing the active filter.
    expect(screen.getByText(/albums by radiohead/i)).toBeInTheDocument();
  });

  test("the clear link removes the artist filter from the URL", async () => {
    server.use(http.get(ALBUMS_URL, () => HttpResponse.json(makePage())));

    renderAtUrl("/?artist=Radiohead");

    await screen.findByText("OK Computer");
    expect(screen.getByTestId("location-search")).toHaveTextContent(
      "artist=Radiohead",
    );

    // "All albums" link clears the filter.
    const clear = screen.getByRole("link", { name: /all albums/i });
    await userEvent.click(clear);

    expect(screen.getByTestId("location-search")).not.toHaveTextContent(
      "artist",
    );
    expect(screen.queryByText(/albums by radiohead/i)).not.toBeInTheDocument();
  });

  test("shows a filter-aware empty state when no albums match the artist", async () => {
    server.use(
      http.get(ALBUMS_URL, () =>
        HttpResponse.json(makePage({ items: [], total: 0 })),
      ),
    );

    renderAtUrl("/?artist=Foo");

    // Filter-aware copy naming the artist, plus an escape link. Two such links
    // exist when filtered (the header clear link + the empty-state link) — both
    // valid escapes — so assert at least one is present.
    expect(await screen.findByText(/no albums by foo/i)).toBeInTheDocument();
    expect(
      screen.getAllByRole("link", { name: /all albums/i }).length,
    ).toBeGreaterThan(0);
    // The generic "library is empty" copy must NOT appear for a zero-match.
    expect(screen.queryByText(/library is empty/i)).not.toBeInTheDocument();
  });

  test("the count's polite live region carries the artist context", async () => {
    server.use(http.get(ALBUMS_URL, () => HttpResponse.json(makePage())));

    renderAtUrl("/?artist=Radiohead");

    const count = await screen.findByText(/2 albums by radiohead/i);
    const live = count.closest("[aria-live]");
    expect(live).not.toBeNull();
    expect(live).toHaveAttribute("aria-live", "polite");
  });

  test("resets the offset to 0 when the artist filter changes", async () => {
    server.use(
      http.get(ALBUMS_URL, ({ request }) => {
        const url = new URL(request.url);
        const offset = Number(url.searchParams.get("offset") ?? "0");
        const artist = url.searchParams.get("artist");
        return HttpResponse.json({
          items: [
            {
              id: 1,
              album_artist: artist ?? "A",
              title: artist ? `Album by ${artist}` : `Offset ${offset}`,
              year: 2000,
              track_count: 10,
              genre: null,
            },
          ],
          total: 5,
          limit: 1,
          offset,
        });
      }),
    );

    // A button that navigates to an artist filter, simulating a click from the
    // Artists page while a stale offset is carried in the URL.
    function Harness() {
      const navigate = useNavigate();
      return (
        <>
          <button type="button" onClick={() => navigate("/?artist=Radiohead")}>
            go to radiohead
          </button>
          <AlbumsPage initialLimit={1} />
          <LocationProbe />
        </>
      );
    }

    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={["/?offset=3"]}>
          <Harness />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    await screen.findByText("Offset 3");
    expect(screen.getByTestId("location-search")).toHaveTextContent("offset=3");

    await userEvent.click(
      screen.getByRole("button", { name: /go to radiohead/i }),
    );

    // The artist filter is applied AND the stale offset is dropped.
    await screen.findByText("Album by Radiohead");
    const search = screen.getByTestId("location-search");
    expect(search).toHaveTextContent("artist=Radiohead");
    expect(search).not.toHaveTextContent("offset");
  });

  test("announces the page range in a polite live region", async () => {
    server.use(
      http.get(ALBUMS_URL, () =>
        HttpResponse.json({
          items: [
            {
              id: 1,
              album_artist: "A",
              title: "Album A",
              year: 2000,
              track_count: 5,
              genre: null,
            },
          ],
          total: 4,
          limit: 1,
          offset: 0,
        }),
      ),
    );

    renderWithProviders(<AlbumsPage initialLimit={1} />);

    const range = await screen.findByText(/1.+4/);
    const live = range.closest("[aria-live]");
    expect(live).not.toBeNull();
    expect(live).toHaveAttribute("aria-live", "polite");
  });
});
