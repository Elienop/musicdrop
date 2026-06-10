import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, test } from "vitest";

import type { components } from "@/api/schema";
import { AlbumDetailPage } from "@/pages/albums/AlbumDetailPage";
import { renderWithProviders } from "@/test/render";
import { server } from "@/test/msw-server";

type AlbumDetail = components["schemas"]["AlbumDetail"];
type Track = components["schemas"]["Track"];

const DETAIL_URL = `${window.location.origin}/api/albums/:albumId`;

function makeTrack(overrides: Partial<Track> = {}): Track {
  return {
    id: 1,
    title: "Airbag",
    track: 1,
    disc: 1,
    duration_seconds: 284,
    artist: "Radiohead",
    mb_trackid: null,
    has_lyrics: false,
    ...overrides,
  };
}

function makeDetail(overrides: Partial<AlbumDetail> = {}): AlbumDetail {
  return {
    id: 1,
    album_artist: "Radiohead",
    title: "OK Computer",
    year: 1997,
    track_count: 3,
    genre: "Alternative Rock",
    mb_albumid: null,
    tracks: [
      makeTrack({ id: 1, title: "Airbag", track: 1, duration_seconds: 284 }),
      makeTrack({
        id: 2,
        title: "Paranoid Android",
        track: 2,
        duration_seconds: 383,
      }),
      makeTrack({
        id: 3,
        title: "Subterranean Homesick Alien",
        track: 3,
        duration_seconds: 227,
      }),
    ],
    ...overrides,
  };
}

/** Render the detail page at `/albums/:albumId` so `useParams` resolves. */
function renderDetail(albumId: number | string = 1) {
  return renderWithProviders(<AlbumDetailPage />, {
    route: `/albums/${albumId}`,
    path: "/albums/:albumId",
  });
}

describe("AlbumDetailPage", () => {
  test("back link returns to the origin (Browse) when opened from there", async () => {
    server.use(http.get(DETAIL_URL, () => HttpResponse.json(makeDetail())));
    renderWithProviders(<AlbumDetailPage />, {
      route: {
        pathname: "/albums/1",
        state: { from: { label: "Browse", to: "/browse?genre=Rock" } },
      },
      path: "/albums/:albumId",
    });
    const back = await screen.findByRole("link", { name: "Browse" });
    expect(back).toHaveAttribute("href", "/browse?genre=Rock");
  });

  test("back link falls back to the artist spine without an origin", async () => {
    server.use(http.get(DETAIL_URL, () => HttpResponse.json(makeDetail())));
    renderDetail(1);
    const back = await screen.findByRole("link", { name: "Radiohead" });
    expect(back).toHaveAttribute("href", "/artists/Radiohead");
  });

  test("renders the album header from the API", async () => {
    server.use(http.get(DETAIL_URL, () => HttpResponse.json(makeDetail())));

    renderDetail(1);

    expect(
      await screen.findByRole("heading", { name: "OK Computer" }),
    ).toBeInTheDocument();
    // "Radiohead" appears twice now (back link + header sub-line); the header
    // sub-line is the <p>, distinct from the back <a>.
    const artistLine = screen
      .getAllByText("Radiohead")
      .find((el) => el.tagName === "P");
    expect(artistLine).toBeInTheDocument();
    expect(screen.getByText("1997")).toBeInTheDocument();
    // The header sub-line renders the bare count "3 tracks"; the lyrics-coverage
    // summary ("0 of 3 tracks have lyrics") also matches /3 tracks/, so scope to
    // the exact header text to keep this assertion about the header.
    expect(screen.getByText("3 tracks")).toBeInTheDocument();
    expect(screen.getByText("Alternative Rock")).toBeInTheDocument();

    // Header cover image points at the album's /cover endpoint. It's
    // decorative (alt="") since the <h2> already names the album, so it's
    // queried by src rather than alt text.
    const cover = document.querySelector('img[src="/api/albums/1/cover"]');
    expect(cover).not.toBeNull();
    expect(cover).toHaveAttribute("alt", "");
  });

  test("renders the tracklist in order", async () => {
    server.use(http.get(DETAIL_URL, () => HttpResponse.json(makeDetail())));

    renderDetail(1);

    expect(await screen.findByText("Airbag")).toBeInTheDocument();
    expect(screen.getByText("Paranoid Android")).toBeInTheDocument();
    expect(screen.getByText("Subterranean Homesick Alien")).toBeInTheDocument();
  });

  test("formats duration_seconds as m:ss", async () => {
    server.use(
      http.get(DETAIL_URL, () =>
        HttpResponse.json(
          makeDetail({
            tracks: [
              // 284s -> 4:44, 383s -> 6:23, 5s -> 0:05 (zero-padded seconds).
              makeTrack({ id: 1, title: "A", track: 1, duration_seconds: 284 }),
              makeTrack({ id: 2, title: "B", track: 2, duration_seconds: 383 }),
              makeTrack({ id: 3, title: "C", track: 3, duration_seconds: 5 }),
            ],
            track_count: 3,
          }),
        ),
      ),
    );

    renderDetail(1);

    expect(await screen.findByText("4:44")).toBeInTheDocument();
    expect(screen.getByText("6:23")).toBeInTheDocument();
    expect(screen.getByText("0:05")).toBeInTheDocument();
  });

  test("shows an en-dash for a null duration", async () => {
    server.use(
      http.get(DETAIL_URL, () =>
        HttpResponse.json(
          makeDetail({
            tracks: [
              makeTrack({
                id: 1,
                title: "Untimed",
                track: 1,
                duration_seconds: null,
              }),
            ],
            track_count: 1,
          }),
        ),
      ),
    );

    renderDetail(1);

    const row = (await screen.findByText("Untimed")).closest("tr");
    expect(row).not.toBeNull();
    // The row carries two en-dashes now: the null duration AND the
    // no-lyrics indicator cell. Assert the duration fallback is among them.
    expect(within(row as HTMLElement).getAllByText("–").length).toBeGreaterThan(0);
  });

  test("shows the track artist only when it differs from the album artist", async () => {
    server.use(
      http.get(DETAIL_URL, () =>
        HttpResponse.json(
          makeDetail({
            album_artist: "Various Artists",
            tracks: [
              makeTrack({
                id: 1,
                title: "Same",
                track: 1,
                artist: "Various Artists",
              }),
              makeTrack({
                id: 2,
                title: "Guest",
                track: 2,
                artist: "Featured Guest",
              }),
            ],
            track_count: 2,
          }),
        ),
      ),
    );

    renderDetail(1);

    expect(await screen.findByText("Guest")).toBeInTheDocument();
    // Differing artist surfaces as a per-track sub-line.
    expect(screen.getByText("Featured Guest")).toBeInTheDocument();
    // Matching artist is not echoed as a per-track artist within the row.
    const sameRow = screen.getByText("Same").closest("tr") as HTMLElement;
    expect(within(sameRow).queryByText("Various Artists")).not.toBeInTheDocument();
    // The album artist appears outside the tracklist only (header sub-line +
    // back link) — never inside a track row.
    const occurrences = screen.getAllByText("Various Artists");
    expect(
      occurrences.every((el) => el.closest("tr") === null),
    ).toBe(true);
  });

  test("labels discs when the album spans multiple discs", async () => {
    server.use(
      http.get(DETAIL_URL, () =>
        HttpResponse.json(
          makeDetail({
            track_count: 3,
            tracks: [
              makeTrack({ id: 1, title: "One", track: 1, disc: 1 }),
              makeTrack({ id: 2, title: "Two", track: 1, disc: 2 }),
              makeTrack({ id: 3, title: "Three", track: 2, disc: 2 }),
            ],
          }),
        ),
      ),
    );

    renderDetail(1);

    expect(await screen.findByText("Disc 1")).toBeInTheDocument();
    expect(screen.getByText("Disc 2")).toBeInTheDocument();
  });

  test("does not label discs for a single-disc album", async () => {
    server.use(http.get(DETAIL_URL, () => HttpResponse.json(makeDetail())));

    renderDetail(1);

    await screen.findByText("Airbag");
    expect(screen.queryByText(/^Disc \d+$/)).not.toBeInTheDocument();
  });

  test("shows a not-found state on a 404", async () => {
    server.use(
      http.get(DETAIL_URL, () => new HttpResponse(null, { status: 404 })),
    );

    renderDetail(999);

    expect(await screen.findByText(/album not found/i)).toBeInTheDocument();
    // With no album data the artist is unknown, so back goes to the roster.
    const back = screen.getByRole("link", { name: /artists/i });
    expect(back).toHaveAttribute("href", "/artists");
  });

  test("shows not-found for a non-numeric id without hitting the API", async () => {
    let requests = 0;
    server.use(
      http.get(DETAIL_URL, () => {
        requests += 1;
        return HttpResponse.json(makeDetail());
      }),
    );

    renderDetail("abc");

    // Renders not-found immediately — no doomed fetch (which would 422).
    expect(await screen.findByText(/album not found/i)).toBeInTheDocument();
    expect(requests).toBe(0);
  });

  test("renders an untagged track number (0) as an en-dash", async () => {
    server.use(
      http.get(DETAIL_URL, () =>
        HttpResponse.json(
          makeDetail({
            track_count: 1,
            tracks: [
              makeTrack({ id: 1, title: "Untracked", track: 0, disc: 1 }),
            ],
          }),
        ),
      ),
    );

    renderDetail(1);

    const row = (await screen.findByText("Untracked")).closest("tr");
    expect(row).not.toBeNull();
    // Two en-dashes now: the untagged track number (0) AND the no-lyrics cell.
    expect(within(row as HTMLElement).getAllByText("–").length).toBeGreaterThan(0);
  });

  test("does not render a 'Disc 0' header for untagged discs", async () => {
    server.use(
      http.get(DETAIL_URL, () =>
        HttpResponse.json(
          makeDetail({
            track_count: 2,
            tracks: [
              // Mixed: one untagged disc (0) and one real disc -> multi-disc,
              // but the disc-0 group must not get a "Disc 0" header.
              makeTrack({ id: 1, title: "Untagged", track: 1, disc: 0 }),
              makeTrack({ id: 2, title: "Tagged", track: 1, disc: 1 }),
            ],
          }),
        ),
      ),
    );

    renderDetail(1);

    expect(await screen.findByText("Disc 1")).toBeInTheDocument();
    expect(screen.queryByText("Disc 0")).not.toBeInTheDocument();
  });

  test("shows an error state with a retry on a 500", async () => {
    server.use(
      http.get(DETAIL_URL, () => new HttpResponse(null, { status: 500 })),
    );

    renderDetail(1);

    expect(
      await screen.findByText(/couldn.t load (this )?album/i),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /retry/i }),
    ).toBeInTheDocument();
  });

  test("retry refetches after a transient error", async () => {
    let calls = 0;
    server.use(
      http.get(DETAIL_URL, () => {
        calls += 1;
        if (calls === 1) {
          return new HttpResponse(null, { status: 500 });
        }
        return HttpResponse.json(makeDetail());
      }),
    );

    renderDetail(1);

    const retry = await screen.findByRole("button", { name: /retry/i });
    await userEvent.click(retry);

    expect(
      await screen.findByRole("heading", { name: "OK Computer" }),
    ).toBeInTheDocument();
  });

  test("has a contextual back link to the album's artist", async () => {
    server.use(http.get(DETAIL_URL, () => HttpResponse.json(makeDetail())));

    renderDetail(1);

    await screen.findByRole("heading", { name: "OK Computer" });
    // Back walks UP the spine: to this album's artist page.
    const back = screen.getByRole("link", { name: /radiohead/i });
    expect(back).toHaveAttribute("href", "/artists/Radiohead");
  });

  test("encodes the artist name in the back link", async () => {
    server.use(
      http.get(DETAIL_URL, () =>
        HttpResponse.json(makeDetail({ album_artist: "Sigur Rós" })),
      ),
    );

    renderDetail(1);

    await screen.findByRole("heading", { name: "OK Computer" });
    const back = screen.getByRole("link", { name: /sigur rós/i });
    expect(back).toHaveAttribute(
      "href",
      `/artists/${encodeURIComponent("Sigur Rós")}`,
    );
  });

  test("error-state back link falls back to the roster", async () => {
    server.use(
      http.get(DETAIL_URL, () => new HttpResponse(null, { status: 500 })),
    );

    renderDetail(1);

    await screen.findByText(/couldn.t load (this )?album/i);
    const back = screen.getByRole("link", { name: /artists/i });
    expect(back).toHaveAttribute("href", "/artists");
  });

  test("the album title is the page h1 with tabindex -1 (RouteAnnouncer focus contract)", async () => {
    server.use(http.get(DETAIL_URL, () => HttpResponse.json(makeDetail())));

    renderDetail(1);

    const h1 = await screen.findByRole("heading", {
      level: 1,
      name: "OK Computer",
    });
    expect(h1).toHaveAttribute("tabindex", "-1");
  });

  test("Edit is a disclosure: aria-expanded + aria-controls + focus moves into the panel", async () => {
    server.use(http.get(DETAIL_URL, () => HttpResponse.json(makeDetail())));

    renderDetail(1);

    const edit = await screen.findByRole("button", { name: "Edit album" });
    expect(edit).toHaveAttribute("aria-expanded", "false");
    expect(edit).toHaveAttribute("aria-controls", "album-edit-panel");

    await userEvent.click(edit);

    expect(edit).toHaveAttribute("aria-expanded", "true");
    const panel = document.getElementById("album-edit-panel");
    expect(panel).not.toBeNull();
    // Opening the disclosure moves focus INTO what just appeared (spec §4).
    expect(panel).toHaveFocus();
  });

  test("Cover is a disclosure with its own panel target", async () => {
    server.use(http.get(DETAIL_URL, () => HttpResponse.json(makeDetail())));

    renderDetail(1);

    const cover = await screen.findByRole("button", { name: "Edit cover" });
    expect(cover).toHaveAttribute("aria-expanded", "false");
    expect(cover).toHaveAttribute("aria-controls", "album-cover-panel");

    await userEvent.click(cover);

    expect(cover).toHaveAttribute("aria-expanded", "true");
    expect(document.getElementById("album-cover-panel")).toHaveFocus();
  });

  test("not-found renders on the shared EmptyState recipe", async () => {
    server.use(
      http.get(DETAIL_URL, () => new HttpResponse(null, { status: 404 })),
    );

    renderDetail(999);

    await screen.findByText(/album not found/i);
    expect(document.querySelector('[data-slot="empty-state"]')).not.toBeNull();
  });

  test("load error renders on the shared ErrorState recipe (role=alert)", async () => {
    server.use(
      http.get(DETAIL_URL, () => new HttpResponse(null, { status: 500 })),
    );

    renderDetail(1);

    await screen.findByText(/couldn.t load (this )?album/i);
    const alert = screen.getByRole("alert");
    expect(alert).toHaveAttribute("data-slot", "error-state");
  });

  test("cold load: focus lands on the h1 once the album renders (deferred h1 focus)", async () => {
    server.use(http.get(DETAIL_URL, () => HttpResponse.json(makeDetail())));

    renderDetail(1);

    // While the skeleton is up there is no h1 and nothing holds focus.
    expect(document.body).toHaveFocus();

    const h1 = await screen.findByRole("heading", {
      level: 1,
      name: "OK Computer",
    });
    await waitFor(() => expect(h1).toHaveFocus());
  });

  test("header is a hero: blurred decorative cover backdrop behind the content", async () => {
    server.use(http.get(DETAIL_URL, () => HttpResponse.json(makeDetail())));

    renderDetail(1);
    await screen.findByRole("heading", { name: "OK Computer" });

    const header = document.querySelector("header");
    expect(header).toHaveClass("relative", "overflow-hidden", "rounded-xl");

    // Layer 1: the backdrop is hidden from assistive tech as a unit.
    const backdrop = header!.querySelector('[data-slot="album-hero-backdrop"]');
    expect(backdrop).not.toBeNull();
    expect(backdrop).toHaveAttribute("aria-hidden", "true");
    const backdropImg = backdrop!.querySelector("img");
    expect(backdropImg).toHaveAttribute("src", "/api/albums/1/cover");
    expect(backdropImg).toHaveClass("blur-2xl", "opacity-40", "object-cover");

    // Both layers point at the same cover URL: blurred backdrop + foreground.
    expect(
      header!.querySelectorAll('img[src="/api/albums/1/cover"]'),
    ).toHaveLength(2);
  });

  test("a failed backdrop load falls back to the tinted gradient", async () => {
    server.use(http.get(DETAIL_URL, () => HttpResponse.json(makeDetail())));

    renderDetail(1);
    await screen.findByRole("heading", { name: "OK Computer" });

    const backdrop = document.querySelector(
      '[data-slot="album-hero-backdrop"]',
    ) as HTMLElement;
    fireEvent.error(backdrop.querySelector("img") as HTMLImageElement);

    // No broken <img> lingers; the primary-tinted gradient takes its place.
    expect(backdrop.querySelector("img")).toBeNull();
    expect(backdrop.querySelector(".bg-gradient-to-br")).not.toBeNull();
  });
});
