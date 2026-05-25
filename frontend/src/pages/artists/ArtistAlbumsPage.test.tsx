import { screen } from "@testing-library/react";
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
