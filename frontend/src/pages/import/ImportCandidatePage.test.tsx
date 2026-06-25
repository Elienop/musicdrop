import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router";
import { describe, expect, test } from "vitest";

import type { Candidate } from "@/api/useImport";
import { ImportCandidatePage } from "@/pages/import/ImportCandidatePage";
import { renderWithProviders } from "@/test/render";
import { server } from "@/test/msw-server";

const CANDIDATE_URL = `${window.location.origin}/api/import/job-1/albums/1`;
const CHOICE_URL = `${window.location.origin}/api/import/job-1/albums/1/choice`;

function makeCandidate(overrides: Partial<Candidate> = {}): Candidate {
  return {
    confidence: 76,
    recommendation: "medium",
    data_source: "MusicBrainz",
    data_url: "https://musicbrainz.org/release/abc",
    cover_after_url: "https://coverartarchive.org/release/abc/front-500",
    has_current_art: false,
    changed_fields: ["album", "label"],
    album_before: {
      artist: "Radiohead",
      album: "OK Computr",
      year: null,
      label: null,
      country: null,
      media: null,
    },
    album_after: {
      artist: "Radiohead",
      album: "OK Computer",
      year: 1997,
      label: "Parlophone",
      country: "GB",
      media: "CD",
    },
    tracks: [
      {
        index: 1,
        status: "unchanged",
        title_before: "Airbag",
        title_after: "Airbag",
        track_before: 1,
        track_after: 1,
      },
      {
        index: 2,
        status: "changed",
        title_before: "Paranoid Andrid",
        title_after: "Paranoid Android",
        track_before: 2,
        track_after: 2,
      },
    ],
    missing: [{ index: 10, title: "Lull" }],
    unmatched: [{ title: "bonus.mp3", track: null }],
    options: [
      // options[0] is the canonical top — its diff MIRRORS the candidate's top
      // diff (so resolveSelected at selected=0 renders identically).
      {
        index: 0,
        confidence: 76,
        data_source: "MusicBrainz",
        disambiguation: "1997 UK CD",
        release_id: "abc",
        album_artist: "Radiohead",
        album: "OK Computer",
        year: 1997,
        album_after: {
          artist: "Radiohead",
          album: "OK Computer",
          year: 1997,
          label: "Parlophone",
          country: "GB",
          media: "CD",
        },
        changed_fields: ["album", "label"],
        tracks: [
          {
            index: 1,
            status: "unchanged",
            title_before: "Airbag",
            title_after: "Airbag",
            track_before: 1,
            track_after: 1,
          },
          {
            index: 2,
            status: "changed",
            title_before: "Paranoid Andrid",
            title_after: "Paranoid Android",
            track_before: 2,
            track_after: 2,
          },
        ],
        missing: [{ index: 10, title: "Lull" }],
        unmatched: [{ title: "bonus.mp3", track: null }],
        cover_after_url: "https://coverartarchive.org/release/abc/front-500",
        data_url: "https://musicbrainz.org/release/abc",
      },
      // options[1] carries a DISTINCT per-release diff — selecting it must
      // re-render the whole preview (album, %, tracklist all change).
      {
        index: 1,
        confidence: 71,
        data_source: "MusicBrainz",
        disambiguation: "2008 reissue",
        release_id: "def",
        album_artist: "Radiohead",
        album: "OK Computer OKNOTOK",
        year: 2017,
        album_after: {
          artist: "Radiohead",
          album: "OK Computer OKNOTOK",
          year: 2017,
          label: "XL Recordings",
          country: "GB",
          media: "CD",
        },
        changed_fields: ["album", "year"],
        tracks: [
          {
            index: 1,
            status: "changed",
            title_before: "Airbag",
            title_after: "Airbag (2017 Remaster)",
            track_before: 1,
            track_after: 1,
          },
        ],
        missing: [],
        unmatched: [],
        cover_after_url: "https://coverartarchive.org/release/def/front-500",
        data_url: "https://musicbrainz.org/release/def",
      },
    ],
    search_revision: 0,
    ...overrides,
  };
}

function renderAt(route = "/import/albums/1?job=job-1") {
  return renderWithProviders(<ImportCandidatePage />, {
    route,
    path: "/import/albums/:index",
  });
}

/** Mounts the candidate page with seeded router state plus a /review probe so
 * origin-driven back links AND post-submit navigation are observable. */
function renderWithOrigin(state?: unknown) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter
        initialEntries={[
          { pathname: "/import/albums/1", search: "?job=job-1", state },
        ]}
      >
        <Routes>
          <Route path="/import/albums/:index" element={<ImportCandidatePage />} />
          <Route path="/review" element={<p>Review page probe</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("ImportCandidatePage", () => {
  test("renders header, source, what-changes, before/after and the tracklist", async () => {
    server.use(http.get(CANDIDATE_URL, () => HttpResponse.json(makeCandidate())));
    renderAt();

    expect(
      await screen.findByRole("heading", { name: /Radiohead — OK Computer/i }),
    ).toBeInTheDocument();
    // "Medium match" is unique to the header (the switcher options carry % +
    // source, not the recommendation label).
    expect(screen.getByText(/Medium match/i)).toBeInTheDocument();
    // The confidence sits in its own header <span> ("76%"); a bare /76%/ would
    // also match the switcher's first <option>, so pin it to the SPAN.
    expect(
      screen.getByText(
        (_, el) => el?.tagName === "SPAN" && el.textContent === "76%",
      ),
    ).toBeInTheDocument();
    // what-changes chips. Cover art is NOT claimed as a change — with the
    // default config the import never fetches/changes art (the after-panel
    // shows the release's art for reference only), so "+ cover art" is absent.
    expect(screen.queryByText("+ cover art")).not.toBeInTheDocument();
    expect(screen.getByText(/not applied/i)).toBeInTheDocument();
    expect(screen.getByText("1 of 2 titles")).toBeInTheDocument();
    // before vs after album title both present
    expect(screen.getByText("OK Computr")).toBeInTheDocument();
    expect(screen.getByText("OK Computer")).toBeInTheDocument();
    // tracklist incl. missing + unmatched
    expect(screen.getByText("Paranoid Android")).toBeInTheDocument();
    expect(screen.getByText("Lull")).toBeInTheDocument();
    expect(screen.getByText("bonus.mp3")).toBeInTheDocument();
  });

  test("shows the missing + not-on-release caveat chips", async () => {
    server.use(http.get(CANDIDATE_URL, () => HttpResponse.json(makeCandidate())));
    renderAt();

    // The Changes row carries caveat chips distinct from the tracklist rows:
    // one for the missing track, one for the unmatched file.
    expect(await screen.findByText("1 missing")).toBeInTheDocument();
    expect(screen.getByText("1 not on release")).toBeInTheDocument();
  });

  test("a changed track row announces 'changed' as sr-only text, not a bare svg label", async () => {
    server.use(http.get(CANDIDATE_URL, () => HttpResponse.json(makeCandidate())));
    renderAt();

    const after = await screen.findByText("Paranoid Android");
    const row = after.closest("tr") as HTMLElement;
    // The marker icon is decorative; the state is real (visually hidden) text.
    expect(within(row).getByText("changed")).toHaveClass("sr-only");
  });

  test("surfaces a track-number change when the title is identical (multi-disc renumber)", async () => {
    // A multi-disc match renumbers tracks to album-global indices, so a row can
    // be "changed" with an unchanged title. The # cell must show before→after so
    // the highlight isn't a mystery (it otherwise shows only the final number).
    // The preview renders the selected option's diff (options[0] at selected=0),
    // so the renumbered track must live on options[0].tracks, not just the
    // top-level candidate.tracks.
    const base = makeCandidate();
    const candidate: Candidate = {
      ...base,
      options: [
        {
          ...base.options[0],
          tracks: [
            {
              index: 19,
              status: "changed",
              title_before: "Wall Street Shuffle",
              title_after: "Wall Street Shuffle",
              track_before: 1,
              track_after: 19,
            },
          ],
          missing: [],
          unmatched: [],
        },
        ...base.options.slice(1),
      ],
    };
    server.use(http.get(CANDIDATE_URL, () => HttpResponse.json(candidate)));
    renderAt();

    expect(await screen.findByText("1 → 19")).toBeInTheDocument();
  });

  test("does not show a number delta when only the title changed", async () => {
    // The default fixture's changed track keeps its number (2 → 2); the # cell
    // must show the single position, never a no-op "2 → 2" arrow.
    server.use(http.get(CANDIDATE_URL, () => HttpResponse.json(makeCandidate())));
    renderAt();

    await screen.findByText("Paranoid Android");
    expect(screen.queryByText("2 → 2")).not.toBeInTheDocument();
  });

  test("labels the covers honestly — yours kept, release art is reference", async () => {
    server.use(
      http.get(CANDIDATE_URL, () =>
        HttpResponse.json(makeCandidate({ has_current_art: true })),
      ),
    );
    renderAt();

    // NOW panel says your cover is kept; AFTER panel marks the matched
    // release's art reference-only ("not applied") so it can't read as a swap.
    expect(await screen.findByText(/kept on import/i)).toBeInTheDocument();
    expect(screen.getByText(/not applied/i)).toBeInTheDocument();
  });

  test("decision buttons carry no title tooltips — the hint is visible text via aria-describedby", async () => {
    server.use(http.get(CANDIDATE_URL, () => HttpResponse.json(makeCandidate())));
    renderAt();

    const asIs = await screen.findByRole("button", { name: /use as-is/i });
    // title= never surfaces on keyboard focus or on a disabled button.
    expect(asIs).not.toHaveAttribute("title");
    expect(asIs).toHaveAttribute("aria-describedby", "review-actions-hint");
    expect(asIs).toHaveAccessibleDescription(/imports with your current tags/i);
    // The hint is VISIBLE helper text under the action bar.
    expect(
      screen.getByText(/no MusicBrainz match is applied/i),
    ).toBeVisible();
  });

  test("does not offer the no-op As-tracks button, but keeps Use as-is", async () => {
    server.use(http.get(CANDIDATE_URL, () => HttpResponse.json(makeCandidate())));
    renderAt();

    // Use as-is stays — confirms the bar rendered before asserting the absence.
    expect(
      await screen.findByRole("button", { name: /use as-is/i }),
    ).toBeInTheDocument();
    // As-tracks is a silent no-op until Slice B builds per-track import, so the
    // button is hidden (the action is still supported in code/types).
    expect(
      screen.queryByRole("button", { name: /as tracks/i }),
    ).not.toBeInTheDocument();
  });

  test("Apply posts apply + the top candidate index, then returns to the feed", async () => {
    let body: unknown = null;
    server.use(
      http.get(CANDIDATE_URL, () => HttpResponse.json(makeCandidate())),
      http.post(CHOICE_URL, async ({ request }) => {
        body = await request.json();
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const user = userEvent.setup();
    renderAt();

    await user.click(await screen.findByRole("button", { name: /^Apply/i }));
    await waitFor(() =>
      expect(body).toEqual({ action: "apply", candidate_index: 0 }),
    );
  });

  test("selecting an alternate re-renders the whole preview for that release", async () => {
    server.use(http.get(CANDIDATE_URL, () => HttpResponse.json(makeCandidate())));
    const user = userEvent.setup();
    renderAt();

    // Top match (selected === 0): the top release + its recommendation word.
    await screen.findByRole("heading", { name: /Radiohead — OK Computer$/i });
    expect(screen.getByText(/Medium match/i)).toBeInTheDocument();
    expect(
      screen.getByText(
        (_, el) => el?.tagName === "SPAN" && el.textContent === "76%",
      ),
    ).toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText(/candidate release/i), "1");

    // The header %, album, and tracklist now reflect the SELECTED release.
    expect(
      await screen.findByRole("heading", {
        name: /Radiohead — OK Computer OKNOTOK/i,
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        (_, el) => el?.tagName === "SPAN" && el.textContent === "71%",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("Airbag (2017 Remaster)")).toBeInTheDocument();
    // The recommendation WORD is dropped for an alternate (beets computes it
    // once per album, not per candidate) — the honest % stays.
    expect(screen.queryByText(/Medium match/i)).not.toBeInTheDocument();
    // And the old apology hint is gone — the preview is real now, not the top.
    expect(
      screen.queryByText(/Showing the top match/i),
    ).not.toBeInTheDocument();
  });

  test("legacy bare-option row: an alternate falls back to the top, keeping the word + an explanatory note", async () => {
    // A row banked before per-candidate previews: the options carry no diff
    // (no `album_after`), so a non-top selection cannot re-render — it falls
    // back to the TOP match. The header must stay coherent (keep the
    // recommendation word) and a note must explain the mismatch.
    const legacy = makeCandidate({
      options: [
        {
          index: 0,
          confidence: 76,
          data_source: "MusicBrainz",
          disambiguation: "1997 UK CD",
          changed_fields: [],
          tracks: [],
          missing: [],
          unmatched: [],
        },
        {
          index: 1,
          confidence: 71,
          data_source: "MusicBrainz",
          disambiguation: "2008 reissue",
          changed_fields: [],
          tracks: [],
          missing: [],
          unmatched: [],
        },
      ],
    });
    server.use(http.get(CANDIDATE_URL, () => HttpResponse.json(legacy)));
    const user = userEvent.setup();
    renderAt();

    await screen.findByRole("heading", { name: /Radiohead — OK Computer$/i });
    await user.selectOptions(screen.getByLabelText(/candidate release/i), "1");

    // (b) the legacy note explains the fallback (role=status).
    const note = await screen.findByText(/Showing the top match/i);
    expect(note).toHaveAttribute("role", "status");
    // (a) the recommendation word STAYS — the preview is still the top match.
    expect(screen.getByText(/Medium match/i)).toBeInTheDocument();
    // (c) album + tracklist stay the TOP match's — no alternate diff to show.
    expect(
      screen.getByRole("heading", { name: /Radiohead — OK Computer$/i }),
    ).toBeInTheDocument();
    expect(screen.getByText("Paranoid Android")).toBeInTheDocument();
    expect(screen.queryByText(/OKNOTOK/)).not.toBeInTheDocument();
    // The header % stays the top's (76%), even though the dropdown shows the
    // alternate — the note + kept word make that honest, not contradictory.
    expect(
      screen.getByText(
        (_, el) => el?.tagName === "SPAN" && el.textContent === "76%",
      ),
    ).toBeInTheDocument();
  });

  test("switching candidate then Apply posts the chosen index", async () => {
    let body: unknown = null;
    server.use(
      http.get(CANDIDATE_URL, () => HttpResponse.json(makeCandidate())),
      http.post(CHOICE_URL, async ({ request }) => {
        body = await request.json();
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const user = userEvent.setup();
    renderAt();

    await user.selectOptions(
      await screen.findByLabelText(/candidate release/i),
      "1",
    );
    await user.click(screen.getByRole("button", { name: /^Apply/i }));
    await waitFor(() =>
      expect(body).toEqual({ action: "apply", candidate_index: 1 }),
    );
  });

  test("Skip posts skip (no candidate index)", async () => {
    let body: unknown = null;
    server.use(
      http.get(CANDIDATE_URL, () => HttpResponse.json(makeCandidate())),
      http.post(CHOICE_URL, async ({ request }) => {
        body = await request.json();
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const user = userEvent.setup();
    renderAt();

    await user.click(await screen.findByRole("button", { name: /^Skip/i }));
    await waitFor(() =>
      expect(body).toEqual({ action: "skip", candidate_index: null }),
    );
  });

  test("a 500 on submit surfaces an inline error and does not navigate", async () => {
    server.use(
      http.get(CANDIDATE_URL, () => HttpResponse.json(makeCandidate())),
      http.post(CHOICE_URL, () =>
        HttpResponse.json({ detail: "boom" }, { status: 500 }),
      ),
    );
    const user = userEvent.setup();
    renderAt();

    await user.click(await screen.findByRole("button", { name: /^Apply/i }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/Couldn’t submit that choice — try again\./);
    // Did NOT navigate away — still on the review screen.
    expect(
      screen.getByRole("heading", { name: /Radiohead — OK Computer/i }),
    ).toBeInTheDocument();
  });

  test("a 404 (no longer parked) shows the calm 'not waiting' notice", async () => {
    server.use(
      http.get(CANDIDATE_URL, () =>
        HttpResponse.json({ detail: "Import album not found" }, { status: 404 }),
      ),
    );
    renderAt();
    expect(
      await screen.findByText(/isn.t waiting for review/i),
    ).toBeInTheDocument();
  });

  test("a missing job id sends the user back with a notice", async () => {
    // No ?job= -> nothing to fetch (the GET handler is never hit).
    renderAt("/import/albums/1");
    expect(screen.getByText(/nothing to review/i)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Import" })).toHaveAttribute(
      "href",
      "/import",
    );
  });

  test("entered from Review: the back link reads Review and points at /review", async () => {
    server.use(http.get(CANDIDATE_URL, () => HttpResponse.json(makeCandidate())));
    renderWithOrigin({ from: { label: "Review", to: "/review" } });

    const back = await screen.findByRole("link", { name: "Review" });
    expect(back).toHaveAttribute("href", "/review");
  });

  test("post-apply navigation follows the Review origin", async () => {
    server.use(
      http.get(CANDIDATE_URL, () => HttpResponse.json(makeCandidate())),
      http.post(CHOICE_URL, () => new HttpResponse(null, { status: 204 })),
    );
    const user = userEvent.setup();
    renderWithOrigin({ from: { label: "Review", to: "/review" } });

    await user.click(await screen.findByRole("button", { name: /^Apply/i }));
    expect(await screen.findByText("Review page probe")).toBeInTheDocument();
  });

  test("without an origin the back link falls back to the job's import feed", async () => {
    server.use(http.get(CANDIDATE_URL, () => HttpResponse.json(makeCandidate())));
    renderWithOrigin(undefined);

    const back = await screen.findByRole("link", { name: "Import" });
    expect(back).toHaveAttribute("href", "/import?job=job-1");
  });

  test("the match heading is the page h1 (RouteAnnouncer focus contract)", async () => {
    server.use(http.get(CANDIDATE_URL, () => HttpResponse.json(makeCandidate())));
    renderAt();

    const h1 = await screen.findByRole("heading", {
      level: 1,
      name: /Radiohead — OK Computer/i,
    });
    expect(h1).toHaveAttribute("tabindex", "-1");
  });

  test("searching by release id posts a search choice and shows the new release", async () => {
    let current = makeCandidate();
    let posted: unknown = null;
    server.use(
      http.get(CANDIDATE_URL, () => HttpResponse.json(current)),
      http.post(CHOICE_URL, async ({ request }) => {
        posted = await request.json();
        // The worker re-looks-up + re-parks: the next GET returns the new release
        // with a bumped search_revision (the page's completion signal).
        current = makeCandidate({
          search_revision: 1,
          album_after: {
            artist: "2 Brothers",
            album: "Dreams",
            year: 1994,
            label: "Lowland",
            country: "NL",
            media: "CD",
          },
          options: [
            {
              ...makeCandidate().options[0],
              album: "Dreams",
              album_after: {
                artist: "2 Brothers",
                album: "Dreams",
                year: 1994,
                label: "Lowland",
                country: "NL",
                media: "CD",
              },
            },
          ],
        });
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const user = userEvent.setup();
    renderAt();

    await screen.findByRole("heading", { name: /Radiohead — OK Computer/i });
    await user.type(
      screen.getByLabelText(/release url or id/i),
      "https://musicbrainz.org/release/dreams",
    );
    await user.click(screen.getByRole("button", { name: /^Search/i }));

    await waitFor(() =>
      expect(posted).toEqual({
        action: "search",
        candidate_index: null,
        search: {
          release_id: "https://musicbrainz.org/release/dreams",
          artist: null,
          album: null,
          force_non_va: true,
        },
      }),
    );
    // The re-looked-up release renders once the revision bumps.
    expect(
      await screen.findByRole("heading", { name: /2 Brothers — Dreams/i }),
    ).toBeInTheDocument();
  });

  test("the not-Various-Artists toggle defaults checked; a name search omits release_id", async () => {
    let posted: unknown = null;
    server.use(
      http.get(CANDIDATE_URL, () =>
        HttpResponse.json(makeCandidate({ search_revision: 1 })),
      ),
      http.post(CHOICE_URL, async ({ request }) => {
        posted = await request.json();
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const user = userEvent.setup();
    renderAt();

    await screen.findByRole("heading", { name: /Radiohead — OK Computer/i });
    // Reveal the name search, then assert the toggle defaults on.
    await user.click(screen.getByText(/or search by name/i));
    expect(
      screen.getByRole("checkbox", {
        name: /not a various-artists compilation/i,
      }),
    ).toBeChecked();
    await user.type(screen.getByLabelText(/^artist/i), "2 Brothers");
    await user.type(screen.getByLabelText(/^album/i), "Dreams");
    await user.click(screen.getByRole("button", { name: /^Search/i }));

    await waitFor(() =>
      expect(posted).toEqual({
        action: "search",
        candidate_index: null,
        search: {
          release_id: null,
          artist: "2 Brothers",
          album: "Dreams",
          force_non_va: true,
        },
      }),
    );
  });

  test("a no-match search surfaces the backend feedback note", async () => {
    let current = makeCandidate();
    server.use(
      http.get(CANDIDATE_URL, () => HttpResponse.json(current)),
      http.post(CHOICE_URL, async () => {
        current = makeCandidate({
          search_revision: 1,
          search_feedback:
            "No release found for that search — showing your previous matches.",
        });
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const user = userEvent.setup();
    renderAt();

    await screen.findByRole("heading", { name: /Radiohead — OK Computer/i });
    await user.type(screen.getByLabelText(/release url or id/i), "artist-url");
    await user.click(screen.getByRole("button", { name: /^Search/i }));

    expect(
      await screen.findByText(/No release found for that search/i),
    ).toBeInTheDocument();
  });
});
