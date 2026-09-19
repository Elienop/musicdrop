import { screen, waitFor, within } from "@testing-library/react";
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
    instrumental: false,
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
    // The ordinary album: every file under the library folder.
    outside_library: null,
    tracks: [
      makeTrack({
        id: 1,
        title: "Airbag",
        track: 1,
        duration_seconds: 284,
        format: "FLAC",
      }),
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
    // TWO artist-named links now: the back link AND the rail artist-name
    // link (names navigate) — both walk UP the spine.
    const links = await screen.findAllByRole("link", { name: "Radiohead" });
    expect(links).toHaveLength(2);
    for (const link of links) {
      expect(link).toHaveAttribute("href", "/artists/Radiohead");
    }
  });

  test("renders the album header from the API", async () => {
    server.use(http.get(DETAIL_URL, () => HttpResponse.json(makeDetail())));

    renderDetail(1);

    expect(
      await screen.findByRole("heading", { name: "OK Computer" }),
    ).toBeInTheDocument();
    // "Radiohead" appears twice (back link + the rail artist-name link —
    // names navigate, both up the spine).
    expect(screen.getAllByRole("link", { name: "Radiohead" })).toHaveLength(2);
    expect(screen.getByText("1997")).toBeInTheDocument();
    // The header sub-line renders the bare count "3 tracks"; the lyrics-coverage
    // summary ("0 of 3 tracks have lyrics") also matches /3 tracks/, so scope to
    // the exact header text to keep this assertion about the header.
    expect(screen.getByText("3 tracks")).toBeInTheDocument();
    expect(screen.getByText("Alternative Rock")).toBeInTheDocument();

    // Header cover image points at the album's /cover endpoint, FULL-size (no
    // ?size=thumb — this hero renders up to 384 CSS px, past what a 320px
    // thumb covers; see the coverSrc comment in AlbumDetailPage.tsx). It's
    // decorative (alt="") since the <h2> already names the album, so it's
    // queried by src rather than alt text.
    const cover = document.querySelector('img[src="/api/albums/1/cover"]');
    expect(cover).not.toBeNull();
    expect(cover).toHaveAttribute("alt", "");
  });

  test("hero cover requests the FULL-size image, not the thumb", async () => {
    // Regression: a 320px thumb blown up to the 384px rail is a visible
    // quality regression — this single per-page hero keeps the original.
    server.use(http.get(DETAIL_URL, () => HttpResponse.json(makeDetail())));

    renderDetail(1);
    await screen.findByRole("heading", { name: "OK Computer" });

    expect(
      document.querySelector('img[src*="/api/albums/1/cover"]'),
    ).not.toHaveAttribute("src", expect.stringContaining("size=thumb"));
  });

  test("renders the tracklist in order", async () => {
    server.use(http.get(DETAIL_URL, () => HttpResponse.json(makeDetail())));

    renderDetail(1);

    expect(await screen.findByText("Airbag")).toBeInTheDocument();
    expect(screen.getByText("Paranoid Android")).toBeInTheDocument();
    expect(screen.getByText("Subterranean Homesick Alien")).toBeInTheDocument();
  });

  test("shows the track's audio format in its own column, only when known", async () => {
    server.use(http.get(DETAIL_URL, () => HttpResponse.json(makeDetail())));

    renderDetail();

    expect(await screen.findByText("FLAC")).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Format" })).toBeInTheDocument();
    // Exactly one FLAC cell: the format-less tracks fall back to "-".
    expect(screen.getAllByText("FLAC")).toHaveLength(1);
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
    expect(within(row as HTMLElement).getAllByText("-").length).toBeGreaterThan(0);
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

  test("a disc header shares its tbody with the disc's tracks (honest rowgroup scope)", async () => {
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

    const disc2 = await screen.findByText("Disc 2");
    // scope="rowgroup" scopes the header to the rest of ITS row group, so
    // the header row must live in the SAME <tbody> as the tracks it labels.
    expect(disc2.closest("th")).toHaveAttribute("scope", "rowgroup");
    const tbody = disc2.closest("tbody") as HTMLElement;
    expect(within(tbody).getByText("Two")).toBeInTheDocument();
    expect(within(tbody).getByText("Three")).toBeInTheDocument();
    expect(within(tbody).queryByText("One")).not.toBeInTheDocument();
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
    expect(within(row as HTMLElement).getAllByText("-").length).toBeGreaterThan(0);
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
    // Back walks UP the spine: to this album's artist page. The rail
    // artist-name link matches too — both must point at the artist.
    const links = screen.getAllByRole("link", { name: /radiohead/i });
    expect(links).toHaveLength(2);
    for (const link of links) {
      expect(link).toHaveAttribute("href", "/artists/Radiohead");
    }
  });

  test("encodes the artist name in the back link", async () => {
    server.use(
      http.get(DETAIL_URL, () =>
        HttpResponse.json(makeDetail({ album_artist: "Sigur Rós" })),
      ),
    );

    renderDetail(1);

    await screen.findByRole("heading", { name: "OK Computer" });
    // Both artist-named links (back + rail name) must encode the name.
    const links = screen.getAllByRole("link", { name: /sigur rós/i });
    expect(links).toHaveLength(2);
    for (const link of links) {
      expect(link).toHaveAttribute(
        "href",
        `/artists/${encodeURIComponent("Sigur Rós")}`,
      );
    }
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

  test("closing the edit panel from inside (Cancel) returns focus to its toggle", async () => {
    server.use(http.get(DETAIL_URL, () => HttpResponse.json(makeDetail())));

    renderDetail(1);

    const edit = await screen.findByRole("button", { name: "Edit album" });
    await userEvent.click(edit);
    expect(document.getElementById("album-edit-panel")).toHaveFocus();

    // The in-panel Cancel unmounts the focused button — focus must return to
    // the disclosure toggle instead of dropping to <body>.
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(document.getElementById("album-edit-panel")).toBeNull();
    expect(edit).toHaveFocus();
  });

  test("closing the cover panel from inside (Cancel) returns focus to its toggle", async () => {
    server.use(http.get(DETAIL_URL, () => HttpResponse.json(makeDetail())));

    renderDetail(1);

    const cover = await screen.findByRole("button", { name: "Edit cover" });
    await userEvent.click(cover);
    expect(document.getElementById("album-cover-panel")).toHaveFocus();

    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(document.getElementById("album-cover-panel")).toBeNull();
    expect(cover).toHaveFocus();
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

  test("the rail shows ONE square cover dissolving through the eased fade", async () => {
    server.use(http.get(DETAIL_URL, () => HttpResponse.json(makeDetail())));

    renderDetail(1);
    const heading = await screen.findByRole("heading", { name: "OK Computer" });

    // Exactly one cover img — the old hero's blurred backdrop layer is gone.
    expect(
      document.querySelectorAll('img[src="/api/albums/1/cover"]'),
    ).toHaveLength(1);

    // The eased fade overlay (shared with the artist rail) is decorative-only.
    expect(
      document.querySelector('div[aria-hidden="true"].fade-bottom-to-base'),
    ).not.toBeNull();

    // The h1 lives in the text strip below the cover — never inside a hidden
    // layer — and keeps the RouteAnnouncer focus contract.
    expect(heading.closest('div[aria-hidden="true"]')).toBeNull();
    expect(heading).toHaveAttribute("tabindex", "-1");
  });

  test("opening the edit panel hides the read-only tracklist until it closes", async () => {
    server.use(http.get(DETAIL_URL, () => HttpResponse.json(makeDetail())));
    renderDetail(1);

    expect(
      await screen.findByRole("region", { name: "Tracklist" }),
    ).toBeInTheDocument();

    // Open the edit panel: it carries its own editable row per track, so the
    // read-only tracklist below must not double the page.
    await userEvent.click(screen.getByRole("button", { name: "Edit album" }));
    expect(screen.getByRole("region", { name: "Edit album" })).toBeInTheDocument();
    expect(
      screen.queryByRole("region", { name: "Tracklist" }),
    ).not.toBeInTheDocument();

    // Closing the panel (Cancel) brings the tracklist back.
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(
      await screen.findByRole("region", { name: "Tracklist" }),
    ).toBeInTheDocument();
  });

  /** The notice's whole sentence — the test's own copy of the production
   * string, so a reworded half fails rather than a fragment still matching. */
  const factOnly = (folder: string) =>
    `Some files are in ${folder}, outside your library folder.`;
  const withRemedy = (folder: string) =>
    `${factOnly(folder)} If an import stopped part-way, add that folder again.`;

  /** One path component at Linux's NAME_MAX, with nothing to break at — the
   * string the wrapping classes exist for. */
  const TORTURE_FOLDER = `/downloads/${"z".repeat(255)}`;

  /** Serve one album whose files sit outside the library folder. */
  function serveOutside(folder: string, holds_every_track: boolean) {
    server.use(
      http.get(DETAIL_URL, () =>
        HttpResponse.json(
          makeDetail({ outside_library: { folder, holds_every_track } }),
        ),
      ),
    );
  }

  test("states the fact AND the remedy when that folder holds every track", async () => {
    const folder = "/downloads/Radiohead - OK Computer";
    serveOutside(folder, true);
    renderDetail(1);

    // The path sits in its own span, so getByText's node text stops short of
    // it — textContent carries the assembled sentence.
    const notice = await screen.findByText(/outside your library folder/);
    expect(notice.textContent).toBe(withRemedy(folder));

    // A fact, not a failure: nothing announces, and the calm muted tone.
    expect(
      notice.closest('[role="status"],[role="alert"],[aria-live],output'),
    ).toBeNull();
    expect(notice.closest(".text-muted-foreground")).not.toBeNull();

    // Route focus lands on the h1, which comes after the notice, so the h1
    // has to carry it — otherwise it is reachable only by reading backwards.
    const heading = screen.getByRole("heading", { name: "OK Computer" });
    expect(heading).toHaveAccessibleDescription(withRemedy(folder));

    const tracklist = screen.getByRole("region", { name: "Tracklist" });
    expect(
      notice.compareDocumentPosition(tracklist) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  test("states the fact ALONE when that folder holds only part of the album", async () => {
    const folder = "/downloads/OK Computer/disc1";
    serveOutside(folder, false);
    renderDetail(1);

    // Re-adding a part-folder sweeps the rest to Trash, so the remedy is the
    // server's call to make, not a sentence the page always shows.
    const notice = await screen.findByText(/outside your library folder/);
    expect(notice.textContent).toBe(factOnly(folder));
    expect(screen.queryByText(/add that folder again/)).toBeNull();
  });

  test("says nothing about the library folder when the album is wholly inside it", async () => {
    server.use(http.get(DETAIL_URL, () => HttpResponse.json(makeDetail())));
    renderDetail(1);

    // Wait for the loaded page first — on the skeleton this would pass for
    // the wrong reason.
    const heading = await screen.findByRole("heading", { name: "OK Computer" });
    expect(screen.queryByText(/outside your library folder/)).toBeNull();
    expect(heading).not.toHaveAttribute("aria-describedby");
  });

  test("a 255-character path component renders whole and keeps both wrapping classes", async () => {
    serveOutside(TORTURE_FOLDER, true);
    renderDetail(1);

    const notice = await screen.findByText(/outside your library folder/);
    // Nothing clamped or truncated, and the <wbr>s between the separators add
    // no text of their own.
    expect(notice.textContent).toBe(withRemedy(TORTURE_FOLDER));

    // jsdom lays nothing out, so these are tripwires, not the oracle: the
    // 320px measurement is. `min-w-0` lowers the flex item's min-content
    // floor; `break-words` splits the one component no line can hold.
    const path = screen.getByText(TORTURE_FOLDER);
    expect(path).toHaveClass("break-words");
    expect(path.closest(".min-w-0")).not.toBeNull();
    // One break opportunity, after `/downloads/` — the root slash gets none.
    expect(path.querySelectorAll("wbr")).toHaveLength(1);
  });

  test("the cover rail cannot outgrow a narrow viewport (max-w-full beside shrink-0)", async () => {
    server.use(http.get(DETAIL_URL, () => HttpResponse.json(makeDetail())));
    renderDetail(1);

    // `shrink-0` alone holds the rail at its w-96 (384px) inside a 320px
    // viewport and scrolls the whole document sideways — every 320px shot of
    // this page shows that scrollbar. The skeleton has carried the pair since
    // it was written; the live rail did not.
    const heading = await screen.findByRole("heading", { name: "OK Computer" });
    const rail = heading.closest("aside");
    expect(rail).toHaveClass("w-96", "max-w-full", "shrink-0");
  });

  test("a break opportunity follows every separator except the root one", async () => {
    // An 11-character first component is the case the root <wbr> spoils: at
    // 320px line 1 would end on a lone "/" with the component below it.
    const folder = "/music-inbox/slskd/OK Computer";
    serveOutside(folder, true);
    renderDetail(1);

    const notice = await screen.findByText(/outside your library folder/);
    expect(notice.textContent).toBe(withRemedy(folder));

    const path = screen.getByText(folder);
    // Three separators, two break opportunities.
    expect(path.querySelectorAll("wbr")).toHaveLength(2);
    // Everything before the FIRST opportunity is the root slash plus the whole
    // first component, so a line can never end on the lone "/".
    const nodes = [...path.childNodes];
    const firstBreak = nodes.findIndex((n) => n.nodeName === "WBR");
    expect(
      nodes
        .slice(0, firstBreak)
        .map((n) => n.textContent)
        .join(""),
    ).toBe("/music-inbox/");
  });
});
