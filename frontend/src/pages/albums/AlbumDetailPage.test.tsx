import { screen, within } from "@testing-library/react";
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
  test("renders the album header from the API", async () => {
    server.use(http.get(DETAIL_URL, () => HttpResponse.json(makeDetail())));

    renderDetail(1);

    expect(
      await screen.findByRole("heading", { name: "OK Computer" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Radiohead")).toBeInTheDocument();
    expect(screen.getByText("1997")).toBeInTheDocument();
    expect(screen.getByText(/3 tracks/i)).toBeInTheDocument();
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
    expect(within(row as HTMLElement).getByText("–")).toBeInTheDocument();
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
    // The album artist still appears exactly once — in the header.
    expect(screen.getAllByText("Various Artists")).toHaveLength(1);
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
    // Back link to the library is present.
    expect(
      screen.getByRole("link", { name: /library/i }),
    ).toBeInTheDocument();
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
    expect(within(row as HTMLElement).getByText("–")).toBeInTheDocument();
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

  test("has a back link to the library", async () => {
    server.use(http.get(DETAIL_URL, () => HttpResponse.json(makeDetail())));

    renderDetail(1);

    await screen.findByRole("heading", { name: "OK Computer" });
    const back = screen.getByRole("link", { name: /library|albums|back/i });
    expect(back).toHaveAttribute("href", "/");
  });
});
