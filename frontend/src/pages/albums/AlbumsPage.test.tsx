import { fireEvent, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, test } from "vitest";

import type { components } from "@/api/schema";
import { AlbumsPage } from "@/pages/albums/AlbumsPage";
import { renderWithProviders } from "@/test/render";
import { server } from "@/test/msw-server";

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
