import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, test } from "vitest";

import type { components } from "@/api/schema";
import { ArtistAlbumsPage } from "@/pages/artists/ArtistAlbumsPage";
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
        album_artist: "Radiohead",
        title: "Kid A",
        year: 2000,
        track_count: 10,
        genre: "Electronic",
      },
    ],
    total: 2,
    limit: 50,
    offset: 0,
    ...overrides,
  };
}

/** Render the page at `/artists/:artistName` so `useParams` resolves. */
function renderAt(artistName: string) {
  return renderWithProviders(<ArtistAlbumsPage />, {
    route: `/artists/${artistName}`,
    path: "/artists/:artistName",
  });
}

/** Render at an explicit route (e.g. with a `?offset=` query) under the same
 * `/artists/:artistName` pattern. */
function renderAtRoute(route: string) {
  return renderWithProviders(<ArtistAlbumsPage />, {
    route,
    path: "/artists/:artistName",
  });
}

describe("ArtistAlbumsPage", () => {
  test("renders the artist's albums from the API filtered by route param", async () => {
    let seenArtist: string | null = null;
    server.use(
      http.get(ALBUMS_URL, ({ request }) => {
        seenArtist = new URL(request.url).searchParams.get("artist");
        return HttpResponse.json(makePage());
      }),
    );

    renderAt("Radiohead");

    expect(await screen.findByText("OK Computer")).toBeInTheDocument();
    expect(screen.getByText("Kid A")).toBeInTheDocument();
    // The route param (not a query string) drove the backend filter.
    expect(seenArtist).toBe("Radiohead");
  });

  test("shows the artist name as the heading", async () => {
    server.use(http.get(ALBUMS_URL, () => HttpResponse.json(makePage())));

    renderAt("Radiohead");

    expect(
      await screen.findByRole("heading", { level: 2, name: "Radiohead" }),
    ).toBeInTheDocument();
  });

  test("renders the artist poster in the header (decorative)", async () => {
    server.use(http.get(ALBUMS_URL, () => HttpResponse.json(makePage())));

    const { container } = renderAt("Radiohead");

    // Wait for the page to settle, then find the header poster. It's decorative
    // (the <h2> names the artist), so it has no accessible name — query by src.
    await screen.findByRole("heading", { level: 2, name: "Radiohead" });
    const poster = container.querySelector(
      'img[src="/api/artists/image?name=Radiohead"]',
    );
    expect(poster).not.toBeNull();
    expect(poster).toHaveAttribute("alt", "");
  });

  test("decodes a URL-encoded artist name from the route", async () => {
    let seenArtist: string | null = null;
    server.use(
      http.get(ALBUMS_URL, ({ request }) => {
        seenArtist = new URL(request.url).searchParams.get("artist");
        return HttpResponse.json(makePage({ items: [], total: 0 }));
      }),
    );

    renderAt(encodeURIComponent("Sigur Rós"));

    await screen.findByRole("heading", { level: 2, name: "Sigur Rós" });
    expect(seenArtist).toBe("Sigur Rós");
  });

  test("has a back affordance to the Artists roster", async () => {
    server.use(http.get(ALBUMS_URL, () => HttpResponse.json(makePage())));

    renderAt("Radiohead");

    await screen.findByText("OK Computer");
    const back = screen.getByRole("link", { name: /artists/i });
    expect(back).toHaveAttribute("href", "/");
  });

  test("album cards link to the album detail page", async () => {
    server.use(http.get(ALBUMS_URL, () => HttpResponse.json(makePage())));

    renderAt("Radiohead");

    await screen.findByText("OK Computer");
    const ok = screen.getByRole("link", { name: /OK Computer/i });
    expect(ok).toHaveAttribute("href", "/albums/1");
  });

  test("announces a loading status to screen readers while pending", () => {
    server.use(
      http.get(
        ALBUMS_URL,
        () => new Promise<HttpResponse<AlbumPage>>(() => {}),
      ),
    );

    renderAt("Radiohead");

    const status = screen.getByRole("status");
    expect(status).toHaveTextContent(/loading albums/i);
  });

  test("shows a per-artist empty state with an escape back to Artists", async () => {
    server.use(
      http.get(ALBUMS_URL, () =>
        HttpResponse.json(makePage({ items: [], total: 0 })),
      ),
    );

    renderAt("Radiohead");

    expect(
      await screen.findByText(/no albums for radiohead/i),
    ).toBeInTheDocument();
    // The escape link goes back to the roster.
    const back = screen.getAllByRole("link", { name: /artists/i });
    expect(back.length).toBeGreaterThan(0);
    expect(back[0]).toHaveAttribute("href", "/");
  });

  test("distinguishes an out-of-range page from a genuinely empty artist", async () => {
    // The artist HAS albums (total > 0), but this offset is past the end so the
    // page is empty. Must NOT claim "No albums for {artist}".
    server.use(
      http.get(ALBUMS_URL, () =>
        HttpResponse.json(makePage({ items: [], total: 5, offset: 999 })),
      ),
    );

    renderAtRoute("/artists/Radiohead?offset=999");

    expect(await screen.findByText(/nothing on this page/i)).toBeInTheDocument();
    // The misleading "no albums for this artist" copy must NOT appear.
    expect(
      screen.queryByText(/no albums for radiohead/i),
    ).not.toBeInTheDocument();
    // An escape back to the first page exists.
    expect(
      screen.getByRole("button", { name: /first page/i }),
    ).toBeInTheDocument();
  });

  test("the first-page escape clears the offset", async () => {
    server.use(
      http.get(ALBUMS_URL, ({ request }) => {
        const offset = Number(
          new URL(request.url).searchParams.get("offset") ?? "0",
        );
        // Past-the-end at 999; first page (offset 0) has real albums.
        if (offset > 0) {
          return HttpResponse.json(makePage({ items: [], total: 5, offset }));
        }
        return HttpResponse.json(makePage({ total: 5 }));
      }),
    );

    renderAtRoute("/artists/Radiohead?offset=999");

    await screen.findByText(/nothing on this page/i);
    await userEvent.click(
      screen.getByRole("button", { name: /first page/i }),
    );

    // Clearing the offset re-runs the query at the first page, which has albums.
    expect(await screen.findByText("OK Computer")).toBeInTheDocument();
    expect(screen.queryByText(/nothing on this page/i)).not.toBeInTheDocument();
  });

  test("does not crash on a malformed (un-decodable) artist param", async () => {
    server.use(
      http.get(ALBUMS_URL, () =>
        HttpResponse.json(makePage({ items: [], total: 0 })),
      ),
    );

    // A lone "%" is not valid percent-encoding; decodeURIComponent throws on it.
    // The page must fall back gracefully rather than crash.
    expect(() => renderAtRoute("/artists/%25")).not.toThrow();
  });

  test("shows an error state with a retry on a 500", async () => {
    server.use(
      http.get(ALBUMS_URL, () => new HttpResponse(null, { status: 500 })),
    );

    renderAt("Radiohead");

    expect(
      await screen.findByText(/couldn.t load albums/i),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument();
  });

  test("shows the album count in a polite live region", async () => {
    server.use(http.get(ALBUMS_URL, () => HttpResponse.json(makePage())));

    renderAt("Radiohead");

    const count = await screen.findByText("2 albums");
    const live = count.closest("[aria-live]");
    expect(live).not.toBeNull();
    expect(live).toHaveAttribute("aria-live", "polite");
  });
});
