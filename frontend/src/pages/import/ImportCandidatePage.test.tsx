import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { delay, http, HttpResponse } from "msw";
import {
  MemoryRouter,
  Route,
  Routes,
  useLocation,
  useNavigationType,
  useParams,
  useSearchParams,
} from "react-router";
import { beforeEach, describe, expect, test, vi } from "vitest";

import type {
  Candidate,
  ImportAlbumSummary,
  ImportJobState,
} from "@/api/useImport";
import { APPLY_NEXT_QUESTION_MS } from "@/api/useImport";
import { albumOriginFromState } from "@/components/albums/album-grid";
import { ImportCandidatePage } from "@/pages/import/ImportCandidatePage";
import {
  containerQueryVariants,
  unwiredContainerQueries,
} from "@/test/containerQuery";
import { renderWithProviders } from "@/test/render";
import { server } from "@/test/msw-server";

const CANDIDATE_URL = `${window.location.origin}/api/import/job-1/albums/1`;
const CHOICE_URL = `${window.location.origin}/api/import/job-1/albums/1/choice`;
const DUPLICATES_URL = `${window.location.origin}/api/import/:jobId/albums/:index/duplicates`;
const JOB_URL = `${window.location.origin}/api/import/job-1`;

/** One live-feed snapshot for album 1. `row: null` means the album is no longer
 * in the feed at all. */
function makeJob(
  row: Partial<ImportAlbumSummary> | null,
  overrides: Partial<ImportJobState> = {},
): ImportJobState {
  const album: ImportAlbumSummary = {
    index: 1,
    folder: "/drop/OK Computer",
    artist: "Radiohead",
    album: "OK Computer",
    recommendation: "medium",
    confidence: 76,
    status: "decided",
    album_id: null,
    did_not_land: false,
    ...row,
  };
  return {
    job_id: "job-1",
    phase: "reviewing",
    progress: { applied: 0, needs_review: 1, skipped: 0, not_landed: 0, already_known: 0 },
    albums: row === null ? [] : [album],
    error: null,
    origin: "manual",
    set_aside: 0,
    elapsed_seconds: 4,
    awaiting_decision: false,
    ...overrides,
  };
}

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
        format: "FLAC",
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
    unmatched: [{ title: "bonus.mp3", track: null, format: "MP3" }],
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
            format: "FLAC",
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
        unmatched: [{ title: "bonus.mp3", track: null, format: "MP3" }],
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
  // The candidate screen runs an up-front library-collision check on mount;
  // default it to "clean" so the existing tests keep passing. Tests that need a
  // collision override this handler.
  //
  // The job feed is read only after a landing choice, to see whether beets has
  // a second question for the album; default it to "the album landed", which is
  // the outcome that sends the user back to the list. Tests about the next
  // question override it.
  beforeEach(() => {
    server.use(
      http.get(DUPLICATES_URL, () => HttpResponse.json({ existing: [] })),
      http.get(JOB_URL, () =>
        HttpResponse.json(makeJob({ status: "applied", album_id: 7 })),
      ),
    );
  });

  test("renders header, source, what-changes, before/after and the tracklist", async () => {
    server.use(http.get(CANDIDATE_URL, () => HttpResponse.json(makeCandidate())));
    renderAt();

    expect(
      await screen.findByRole("heading", { name: /Radiohead - OK Computer/i }),
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
    // shows the release's art for reference only), so NO chip may mention it.
    // The chip renders its change string verbatim, so pin the SHAPE (any
    // art-ish wording inside a Badge) instead of one literal production never
    // emits. Scoping to `data-slot=badge` is what keeps the reference-only
    // caption <p> ("Release art · not applied") out of the count; the plural
    // query because the singular form throws when several chips match.
    const artChips = screen.queryAllByText(
      (_content, el) =>
        el?.getAttribute("data-slot") === "badge" &&
        /\bart(work)?\b/i.test(el.textContent ?? ""),
    );
    expect(artChips).toHaveLength(0);
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

  test("the match header is one wrapping text flow, glued at every separator", async () => {
    server.use(http.get(CANDIDATE_URL, () => HttpResponse.json(makeCandidate())));
    renderAt();
    await screen.findByRole("heading", { name: /Radiohead - OK Computer/i });

    // The whole line in one assertion — the joins are the thing under test, and
    // fragments cannot pin a join. Every separator is `SEGMENT_SEP`: a plain
    // space, the middot, then U+00A0, so a wrap breaks BEFORE the middot and
    // never strands one at the end of a line. Written as escapes because a
    // literal NBSP is invisible in review, and read off `textContent` because
    // the default RTL normalizer collapses U+00A0 to a plain space — a text
    // query cannot tell the two separators apart.
    const line = screen.getByText(
      (_, el) => el?.tagName === "P" && (el.textContent ?? "").startsWith("76%"),
    );
    expect(line.textContent).toBe(
      "76% \u00b7\u00a0Medium match \u00b7\u00a0MusicBrainz \u00b7\u00a01997 " +
        "\u00b7\u00a0CD \u00b7\u00a0GB \u00b7\u00a0Parlophone " +
        "\u00b7\u00a0view (opens the release page in a new tab)",
    );
    // `textContent` INCLUDES `aria-hidden` nodes, so the assertion above cannot
    // see a separator that renders but is not spoken \u2014 and one here was not.
    // The middot before the source list was hidden, and since `SEGMENT_SEP`
    // carries this line's only whitespace, Chrome's AX tree read "\u2026Medium
    // match" and "MusicBrainz\u2026" as adjacent StaticText nodes. Every separator
    // is plain text now, so dropping the hidden nodes must not change the
    // string. The `<svg>` is the control: it is the one `aria-hidden` node
    // left, and it contributes nothing to `textContent` either way.
    const withoutHidden = (node: Node): string => {
      if (node.nodeType === Node.TEXT_NODE) return node.textContent ?? "";
      if (node.nodeType !== Node.ELEMENT_NODE) return "";
      if ((node as Element).getAttribute("aria-hidden") === "true") return "";
      return [...node.childNodes].map(withoutHidden).join("");
    };
    // The helper's own control: it must actually drop a hidden subtree, or the
    // assertion below passes by doing nothing.
    const probe = document.createElement("p");
    probe.innerHTML = 'a<span aria-hidden="true">HIDDEN</span>b';
    expect(probe.textContent).toBe("aHIDDENb");
    expect(withoutHidden(probe)).toBe("ab");

    expect(withoutHidden(line)).toBe(line.textContent);
    const hidden = line.querySelectorAll('[aria-hidden="true"]');
    expect(hidden).toHaveLength(1);
    expect(hidden[0].tagName).toBe("svg"); // decorative, and carries no text
    // The layout half: as `flex items-center gap-2` this line put every segment
    // on one flex line and the browser squeezed the widest of them to three
    // line boxes at 360px. Normal inline layout wraps between words instead.
    // jsdom cannot measure that, so what is pinned here is the class the
    // squeeze needed; the wrap itself is a browser measurement.
    const tokens = line.className.split(/\s+/);
    expect(tokens).not.toContain("flex");
    // Dropping the flex row also dropped the `truncate` that was capping this
    // line's contribution to the page's scroll width. `break-words` is what
    // holds that cap now — measured at 360px with a 60-character unbreakable
    // label, 242px of element overflow and 218px of document scroll without it,
    // 0 and 17 with it (the 17 is AlbumPanel's, recorded separately).
    expect(tokens).toContain("break-words");
    // Control: a line that lost its classes entirely would also pass the line
    // above, so pin what must still be there.
    expect(tokens).toContain("text-sm");
    expect(tokens).toContain("text-muted-foreground");
  });

  test("the header h1 carries BOTH classes an unbreakable title needs", async () => {
    server.use(http.get(CANDIDATE_URL, () => HttpResponse.json(makeCandidate())));
    renderAt();
    const h1 = await screen.findByRole("heading", { name: /Radiohead - OK Computer/i });

    // Two classes, and neither works alone here. This h1 is a flex ITEM of a
    // row, so its default `min-width: auto` floors it at the longest
    // unbreakable token; `overflow-wrap` picks where lines break but does not
    // lower that floor (SettingsTrashPage.tsx:209-211 writes the rule out).
    // Measured at 360px with a 30-character artist and a 45-character album:
    // 371px of document scroll with neither, 371px with `break-words` alone,
    // and 0 with both. AlbumDetailPage's h1 needs only `break-words` because it
    // is in normal flow — the class list is not transferable, the reason is.
    const tokens = h1.className.split(/\s+/);
    expect(tokens).toContain("min-w-0");
    expect(tokens).toContain("break-words");
    // Control: pin the parent shape the two classes are answering, so a future
    // move out of the flex row makes this test say so rather than pass on.
    expect(h1.parentElement?.className.split(/\s+/)).toEqual(
      expect.arrayContaining(["flex", "flex-wrap"]),
    );
  });

  test("shows the current file's format in the Format column", async () => {
    server.use(http.get(CANDIDATE_URL, () => HttpResponse.json(makeCandidate())));
    renderAt();

    // One FLAC cell from the matched row's file; nothing else in the table
    // renders a format value.
    expect(await screen.findByText("FLAC")).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Format" })).toBeInTheDocument();
    expect(screen.getAllByText("FLAC")).toHaveLength(1);
    // The unmatched local file fills its own Format cell.
    expect(screen.getAllByText("MP3")).toHaveLength(1);
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

    // The number split lives across the two cards now: the file's own position
    // on the left ("Now"), the release's renumbered position on the right.
    await screen.findByRole("table", { name: "Current files" });
    const nowTable = screen.getByRole("table", { name: "Current files" });
    const afterTable = screen.getByRole("table", { name: "After import" });
    expect(within(nowTable).getByText("1")).toBeInTheDocument();
    expect(within(afterTable).getByText("19")).toBeInTheDocument();
  });

  // The before/after panels lay themselves out from their own width, not the
  // viewport's. jsdom computes no layout, so the widths that chose 27rem and
  // 14rem are browser-measured and recorded in the component; what a test CAN
  // hold is that the variants are wired to a declared container — rename one
  // side and CSS reports nothing, the panel silently keeps one arm.
  // The PRESENCE list is half the pin: an empty unwired list also means "no
  // variants here", so on its own it survives deleting the whole layer.
  test("wires every container-query variant to a declared container", async () => {
    server.use(http.get(CANDIDATE_URL, () => HttpResponse.json(makeCandidate())));
    const { container } = renderAt();

    await screen.findByText("Paranoid Android");
    expect(containerQueryVariants(container)).toEqual([
      "@min-[14rem]/panel:flex-row",
      "@min-[14rem]/panel:gap-0.5",
      "@min-[14rem]/panel:items-baseline",
      "@min-[27rem]/panel:flex-1",
      "@min-[27rem]/panel:flex-row",
      "@min-[27rem]/panel:gap-4",
      "@min-[27rem]/panel:self-center",
    ]);
    expect(unwiredContainerQueries(container)).toEqual([]);
  });

  test("a title-only change still shows the number as a plain position in each panel", async () => {
    // The mirrored-cell design (#112 replaced the old combined
    // `before → after` cell): each panel owns ONE bare number per row. The
    // default fixture's changed track keeps its number (2 in both), which is
    // the case a delta cell would spoil — it would render "2 → 2" and leave
    // neither table with a plain "2". The sibling test above covers the
    // renumbered case (1 → 19); this one covers the identical-number case.
    server.use(http.get(CANDIDATE_URL, () => HttpResponse.json(makeCandidate())));
    renderAt();

    await screen.findByText("Paranoid Android");
    const nowTable = screen.getByRole("table", { name: "Current files" });
    const afterTable = screen.getByRole("table", { name: "After import" });
    expect(within(nowTable).getAllByText("2")).toHaveLength(1);
    expect(within(afterTable).getAllByText("2")).toHaveLength(1);
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
    // The bar wires the hint via a generated id (useId), so assert the linkage
    // through the resolved accessible description, not a hardcoded id.
    expect(asIs).toHaveAttribute("aria-describedby");
    expect(asIs).toHaveAccessibleDescription(
      /Use as-is keeps your current tags; no MusicBrainz match is applied\./i,
    );
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
    // The apply echoes the rendered candidate's search_revision so the worker
    // can tell a stale submit (predating a search re-park) from a live one.
    await waitFor(() =>
      expect(body).toEqual({
        action: "apply",
        candidate_index: 0,
        search_revision: 0,
      }),
    );
  });

  test("selecting an alternate re-renders the whole preview for that release", async () => {
    server.use(http.get(CANDIDATE_URL, () => HttpResponse.json(makeCandidate())));
    const user = userEvent.setup();
    renderAt();

    // Top match (selected === 0): the top release + its recommendation word.
    await screen.findByRole("heading", { name: /Radiohead - OK Computer$/i });
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
        name: /Radiohead - OK Computer OKNOTOK/i,
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

    await screen.findByRole("heading", { name: /Radiohead - OK Computer$/i });
    await user.selectOptions(screen.getByLabelText(/candidate release/i), "1");

    // (b) the legacy note explains the fallback (role=status).
    const note = await screen.findByText(/Showing the top match/i);
    expect(note.tagName).toBe("OUTPUT"); // native <output> — implicit role="status"
    // (a) the recommendation word STAYS — the preview is still the top match.
    expect(screen.getByText(/Medium match/i)).toBeInTheDocument();
    // (c) album + tracklist stay the TOP match's — no alternate diff to show.
    expect(
      screen.getByRole("heading", { name: /Radiohead - OK Computer$/i }),
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
      expect(body).toEqual({
        action: "apply",
        candidate_index: 1,
        search_revision: 0,
      }),
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
    expect(alert).toHaveTextContent(/Couldn’t submit that choice\. Try again\./);
    // Did NOT navigate away — still on the review screen.
    expect(
      screen.getByRole("heading", { name: /Radiohead - OK Computer/i }),
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

  test("a 404 renders the 'already decided' copy, not the retryable error", async () => {
    // A genuine no-longer-parked album: the calm "already decided" notice is
    // correct here — and it must NOT be the generic retryable ErrorState.
    server.use(
      http.get(CANDIDATE_URL, () =>
        HttpResponse.json({ detail: "Import album not found" }, { status: 404 }),
      ),
    );
    renderAt();

    expect(await screen.findByText(/already be decided/i)).toBeInTheDocument();
    expect(screen.getByText(/isn.t waiting for review/i)).toBeInTheDocument();
    // Not the shared error surface (role=alert data-slot=error-state / "Retry").
    expect(
      screen.queryByRole("button", { name: /^retry$/i }),
    ).not.toBeInTheDocument();
    expect(
      document.querySelector('[data-slot="error-state"]'),
    ).not.toBeInTheDocument();
  });

  test("a non-404 candidate error renders the retryable ErrorState, not the 'already decided' notice", async () => {
    // A transient failure (5xx / network blip; retry:false means one is enough)
    // must NOT tell the user their still-parked decision is gone — it's a
    // retryable error, on the shared ErrorState recipe.
    server.use(
      http.get(CANDIDATE_URL, () => new HttpResponse(null, { status: 500 })),
    );
    renderAt();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveAttribute("data-slot", "error-state");
    expect(
      screen.getByRole("button", { name: /^retry$/i }),
    ).toBeInTheDocument();
    // It must not assert the decision is already gone.
    expect(
      screen.queryByText(/isn.t waiting for review/i),
    ).not.toBeInTheDocument();
    expect(screen.queryByText(/already be decided/i)).not.toBeInTheDocument();
  });

  test("a 404 arriving mid-search clears the searching state and stops the 700ms poll", async () => {
    // The stuck-poll regression: while `searching` is latched, if the candidate
    // starts 404ing (album decided from a 2nd tab / job died mid-search), the
    // page must clear `searching` — stopping the 700ms poll and unfreezing the
    // panel — instead of hammering the 404 forever under a misleading notice.
    let gone = false;
    let getCalls = 0;
    server.use(
      http.get(CANDIDATE_URL, () => {
        getCalls += 1;
        if (gone) {
          return HttpResponse.json(
            { detail: "Import album not found" },
            { status: 404 },
          );
        }
        return HttpResponse.json(makeCandidate());
      }),
      http.post(CHOICE_URL, () => {
        // The slot vanished mid-search — the candidate now 404s while the page
        // is still in its `searching` state (no revision bump ever arrives).
        gone = true;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const user = userEvent.setup();
    renderAt();

    await screen.findByRole("heading", { name: /Radiohead - OK Computer/i });
    // Enter the searching state via rescan; the follow-up candidate load 404s.
    await user.click(screen.getByRole("button", { name: /rescan folder/i }));

    // The page flips to the calm notice (searching cleared -> poll off).
    expect(
      await screen.findByText(/isn.t waiting for review/i),
    ).toBeInTheDocument();
    // With `searching` cleared, refetchInterval is false: the poll must stop.
    const callsAtStop = getCalls;
    await act(() => new Promise((r) => setTimeout(r, 1500)));
    expect(getCalls).toBe(callsAtStop);
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
      name: /Radiohead - OK Computer/i,
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

    await screen.findByRole("heading", { name: /Radiohead - OK Computer/i });
    // The search form is folded inside the bar behind the "Different release"
    // toggle — open it before touching the release field.
    await user.click(screen.getByRole("button", { name: /different release/i }));
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
      await screen.findByRole("heading", { name: /2 Brothers - Dreams/i }),
    ).toBeInTheDocument();
  });

  test("after a search re-park bumps the revision, Apply echoes the NEW revision", async () => {
    let current = makeCandidate();
    const posted: unknown[] = [];
    server.use(
      http.get(CANDIDATE_URL, () => HttpResponse.json(current)),
      http.post(CHOICE_URL, async ({ request }) => {
        posted.push(await request.json());
        // The first POST is the search: the worker re-parks with a bumped
        // revision; the poll picks it up and the page re-renders on it.
        current = makeCandidate({
          search_revision: 1,
          album_after: { ...makeCandidate().album_after, album: "Amnesiac" },
          options: [
            {
              ...makeCandidate().options[0],
              album: "Amnesiac",
              album_after: {
                ...makeCandidate().album_after,
                album: "Amnesiac",
              },
            },
          ],
        });
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const user = userEvent.setup();
    renderAt();

    await screen.findByRole("heading", { name: /Radiohead - OK Computer/i });
    await user.click(screen.getByRole("button", { name: /different release/i }));
    await user.type(
      screen.getByLabelText(/release url or id/i),
      "https://musicbrainz.org/release/amnesiac",
    );
    await user.click(screen.getByRole("button", { name: /^Search/i }));

    // The re-parked candidate (revision 1) renders; Apply must echo revision 1,
    // not the stale 0 the page loaded with.
    await screen.findByRole("heading", { name: /Radiohead - Amnesiac/i });
    await user.click(screen.getByRole("button", { name: /^Apply/i }));
    await waitFor(() =>
      expect(posted[1]).toEqual({
        action: "apply",
        candidate_index: 0,
        search_revision: 1,
      }),
    );
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

    await screen.findByRole("heading", { name: /Radiohead - OK Computer/i });
    // Open the folded search row, then assert the toggle defaults on.
    await user.click(screen.getByRole("button", { name: /different release/i }));
    expect(
      screen.getByRole("checkbox", {
        name: /not a compilation/i,
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
          search_feedback: "No release found. Showing your previous matches.",
        });
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const user = userEvent.setup();
    renderAt();

    await screen.findByRole("heading", { name: /Radiohead - OK Computer/i });
    await user.click(screen.getByRole("button", { name: /different release/i }));
    await user.type(screen.getByLabelText(/release url or id/i), "artist-url");
    await user.click(screen.getByRole("button", { name: /^Search/i }));

    expect(
      await screen.findByText(/No release found/i),
    ).toBeInTheDocument();
  });

  test("a failed search re-enables the panel instead of softlocking", async () => {
    server.use(
      http.get(CANDIDATE_URL, () => HttpResponse.json(makeCandidate())),
      http.post(CHOICE_URL, () => HttpResponse.json({ detail: "boom" }, { status: 500 })),
    );
    const user = userEvent.setup();
    renderAt();

    await screen.findByRole("heading", { name: /Radiohead - OK Computer/i });
    await user.click(screen.getByRole("button", { name: /different release/i }));
    await user.type(screen.getByLabelText(/release url or id/i), "rel-1");
    await user.click(screen.getByRole("button", { name: /^Search/i }));

    // The POST failed (no revision bump): the Search button must un-freeze and
    // the panel's error must show — not stay stuck on "Searching…" forever.
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /^Search$/ })).toBeEnabled(),
    );
    expect(screen.getByText(/Couldn.t run that search/i)).toBeInTheDocument();
  });

  test("shows the up-front library collision as a heads-up", async () => {
    server.use(
      http.get(CANDIDATE_URL, () => HttpResponse.json(makeCandidate())),
      http.get(DUPLICATES_URL, () =>
        HttpResponse.json({
          existing: [
            {
              album_id: 7,
              album_artist: "David Bowie",
              album: "Heroes",
              year: 1977,
              track_count: 1,
              format: "MP3",
              bitrate_kbps: 320,
              folder: "/library/Bowie/Heroes",
              release: null,
              tracks: [
                { track: 3, disc: 1, title: "Heroes", format: "MP3", bitrate_kbps: 320 },
              ],
            },
          ],
        }),
      ),
    );
    renderAt();
    expect(
      await screen.findByText(/already in your library/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/1 track\b/)).toBeInTheDocument();
    // Heads-up only: Apply is still the footer's action, no duplicate buttons.
    expect(
      screen.queryByRole("button", { name: /replace old/i }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /apply/i })).toBeEnabled();
  });

  test("posts a rescan choice and enters the searching state", async () => {
    const bodies: unknown[] = [];
    server.use(
      http.get(CANDIDATE_URL, () => HttpResponse.json(makeCandidate())),
      http.post(CHOICE_URL, async ({ request }) => {
        bodies.push(await request.json());
        return new HttpResponse(null, { status: 204 });
      }),
    );
    renderAt();
    const rescanButton = await screen.findByRole("button", {
      name: /rescan folder/i,
    });
    fireEvent.click(rescanButton);
    await waitFor(() => expect(bodies).toHaveLength(1));
    expect(bodies[0]).toMatchObject({ action: "rescan", candidate_index: null });
    // In-flight: the decision buttons disable until the revision bumps.
    expect(screen.getByRole("button", { name: /apply/i })).toBeDisabled();
  });

  test("locks out rescan and the release search while an Apply is in flight", async () => {
    server.use(
      http.get(CANDIDATE_URL, () => HttpResponse.json(makeCandidate())),
      // The Apply POST never resolves during the assertion window, so the
      // decide mutation stays pending.
      http.post(CHOICE_URL, async () => {
        await delay("infinite");
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const user = userEvent.setup();
    renderAt();

    // Open the folded search row BEFORE the Apply: the toggle locks with the
    // rest, so afterwards there would be no way to mount the release field.
    await user.click(await screen.findByRole("button", { name: /different release/i }));
    await user.click(screen.getByRole("button", { name: /^Apply/i }));
    // With the Apply in flight, the no-undo relookup controls must lock out —
    // firing a rescan/search against the same album mid-Apply is an avoidable
    // concurrent-action window the backend can only swallow.
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /rescan folder/i })).toBeDisabled(),
    );
    expect(screen.getByLabelText(/release url or id/i)).toBeDisabled();
    expect(screen.getByRole("button", { name: /different release/i })).toBeDisabled();
  });

  test("rescan rides the control bar and locks with the other tools", async () => {
    server.use(http.get(CANDIDATE_URL, () => HttpResponse.json(makeCandidate())));
    renderAt();
    await screen.findByRole("heading", { name: /Radiohead - OK Computer/i });
    const rescan = screen.getByRole("button", { name: /rescan folder/i });
    expect(rescan).toHaveAttribute(
      "title",
      "Re-reads the folder from disk and matches it again.",
    );
  });
});

/** beets asks two questions per album — the match, then the duplicate. These
 * pin the second one arriving WHERE the first was answered, instead of the user
 * being dropped on a list to go and find it. */
describe("ImportCandidatePage — the next question after Apply", () => {
  /** Renders the Review list. Counts its own renders, so "never passes through
   * a list" is an assertion and not an absence at one moment. */
  let reviewRenders = 0;
  function ReviewProbe() {
    reviewRenders += 1;
    const navigationType = useNavigationType();
    return (
      <>
        <p>Review page probe</p>
        <p>{`Arrived by ${navigationType}`}</p>
      </>
    );
  }

  /** Stands in for the duplicate screen, echoing the three things the hop has
   * to get right: the album, the job, and the origin it was handed. */
  function DuplicateProbe() {
    const { index } = useParams<{ index: string }>();
    const [params] = useSearchParams();
    const origin = albumOriginFromState(useLocation().state);
    return (
      <>
        <p>
          {`Duplicate probe index=${index} job=${params.get("job") ?? ""} ` +
            `back=${origin?.label ?? "none"}|${origin?.to ?? "none"}`}
        </p>
        <p>{`Arrived by ${useNavigationType()}`}</p>
      </>
    );
  }

  /** The candidate screen with BOTH of its landing places mounted. `seed` puts
   * something in the query cache first — the list page the user came through
   * leaves its own job poll behind. */
  function renderHop(seed?: (qc: QueryClient) => void) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    seed?.(qc);
    return render(
      <QueryClientProvider client={qc}>
        <MemoryRouter
          initialEntries={[
            {
              pathname: "/import/albums/1",
              search: "?job=job-1",
              state: { from: { label: "Review", to: "/review" } },
            },
          ]}
        >
          <Routes>
            <Route path="/import/albums/:index" element={<ImportCandidatePage />} />
            <Route
              path="/import/albums/:index/duplicate"
              element={<DuplicateProbe />}
            />
            <Route path="/review" element={<ReviewProbe />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );
  }

  async function clickApply() {
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: /^Apply/i }));
  }

  /** One library collision, so the up-front check ANSWERS "there is something
   * to collide with" and the screen holds for beets' verdict. Every routing
   * test below is about what happens during that hold, so it is the default
   * here; the empty answer is the fast path, pinned on its own. */
  const COLLISION = {
    album_id: 12,
    album_artist: "Radiohead",
    album: "OK Computer",
    year: 1997,
    track_count: 1,
    format: "FLAC",
    bitrate_kbps: null,
    folder: "/library/Radiohead/OK Computer",
    release: null,
    tracks: [
      { track: 1, disc: 1, title: "Airbag", format: "FLAC", bitrate_kbps: null },
    ],
  };

  beforeEach(() => {
    reviewRenders = 0;
    server.use(
      http.get(DUPLICATES_URL, () => HttpResponse.json({ existing: [COLLISION] })),
      http.get(CANDIDATE_URL, () => HttpResponse.json(makeCandidate())),
      http.post(CHOICE_URL, () => new HttpResponse(null, { status: 204 })),
    );
  });

  // The common case, and the one the wait must not tax: the library check has
  // answered "nothing matches", so the 204 returns the user to the list exactly
  // as it did before this screen learned to wait — and the feed is never asked.
  //
  // The answer is SEEDED rather than fetched, so "already answered" holds on
  // the first render: waiting for the fetch to commit has nothing on screen to
  // wait for (an empty result renders nothing), and a click that beats it would
  // exercise the unanswered branch instead. The key mirrors
  // useImportDuplicates' own.
  test("no predicted collision: the 204 returns to the list, with no feed poll", async () => {
    let feedPolls = 0;
    server.use(
      http.get(DUPLICATES_URL, () => HttpResponse.json({ existing: [] })),
      http.get(JOB_URL, () => {
        feedPolls += 1;
        return HttpResponse.json(
          makeJob({ status: "needs_dup_resolution" }, { awaiting_decision: true }),
        );
      }),
    );
    renderHop((qc) =>
      qc.setQueryData(["import", "duplicates", "job-1", 1, 0, 0], {
        existing: [],
      }),
    );
    await clickApply();

    expect(await screen.findByText("Review page probe")).toBeInTheDocument();
    // The strong half: a hold polls the feed at least once, and this feed says
    // "duplicate", so a hold would have hopped instead of landing here.
    expect(feedPolls).toBe(0);
    expect(screen.queryByText(/duplicate probe/i)).not.toBeInTheDocument();
    // Every post-decision exit replaces: Back must not re-enter a decided
    // album, which answers the candidate GET with the not-found notice.
    expect(screen.getByText("Arrived by REPLACE")).toBeInTheDocument();
  });

  // Each exit gets its own pin: they are four different `navigate` calls.
  test("the hold's exits replace too", async () => {
    server.use(
      http.get(JOB_URL, () =>
        HttpResponse.json(makeJob({ status: "decided", album_id: 7 })),
      ),
    );
    renderHop();
    await clickApply();

    expect(await screen.findByText("Arrived by REPLACE")).toBeInTheDocument();
  });

  // The fast path belongs to both landing actions, not just Apply.
  test("no predicted collision: Use as-is returns to the list too", async () => {
    let feedPolls = 0;
    server.use(
      http.get(DUPLICATES_URL, () => HttpResponse.json({ existing: [] })),
      http.get(JOB_URL, () => {
        feedPolls += 1;
        return HttpResponse.json(
          makeJob({ status: "needs_dup_resolution" }, { awaiting_decision: true }),
        );
      }),
    );
    renderHop((qc) =>
      qc.setQueryData(["import", "duplicates", "job-1", 1, 0, 0], {
        existing: [],
      }),
    );
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: /use as-is/i }));

    expect(await screen.findByText("Review page probe")).toBeInTheDocument();
    expect(feedPolls).toBe(0);
  });

  // Pending and errored are the OTHER side of the same `isSuccess` gate: an
  // unanswered check cannot rule a collision out, so the screen holds and the
  // feed decides. Pinned on the pending half, which is the one a test can hold
  // still.
  test("an unanswered library check holds too", async () => {
    server.use(
      http.get(DUPLICATES_URL, async () => {
        await delay("infinite");
        return HttpResponse.json({ existing: [] });
      }),
      http.get(JOB_URL, () =>
        HttpResponse.json(
          makeJob({ status: "needs_dup_resolution" }, { awaiting_decision: true }),
        ),
      ),
    );
    renderHop();
    await clickApply();

    expect(
      await screen.findByText(
        "Duplicate probe index=1 job=job-1 back=Review|/review",
      ),
    ).toBeInTheDocument();
    expect(reviewRenders).toBe(0);
  });

  test("the parked duplicate opens for the same album, keeping the Review origin", async () => {
    server.use(
      http.get(JOB_URL, () =>
        HttpResponse.json(
          makeJob({ status: "needs_dup_resolution" }, { awaiting_decision: true }),
        ),
      ),
    );
    renderHop();
    await clickApply();

    expect(
      await screen.findByText(
        "Duplicate probe index=1 job=job-1 back=Review|/review",
      ),
    ).toBeInTheDocument();
    // The whole point of the change: the Review list is not a stop on the way.
    expect(reviewRenders).toBe(0);
    expect(screen.getByText("Arrived by REPLACE")).toBeInTheDocument();
  });

  test("a candidate 404 during the wait does not cancel the hop", async () => {
    // The choice's own onSettled invalidates the candidate query, and the
    // re-read 404s once the worker has moved past the album. The job feed is
    // what routes here, so that 404 must not swap in the "not waiting" notice.
    let choiceMade = false;
    server.use(
      http.post(CHOICE_URL, () => {
        choiceMade = true;
        return new HttpResponse(null, { status: 204 });
      }),
      http.get(CANDIDATE_URL, () =>
        choiceMade
          ? new HttpResponse(null, { status: 404 })
          : HttpResponse.json(makeCandidate()),
      ),
      http.get(JOB_URL, () =>
        HttpResponse.json(
          makeJob({ status: "needs_dup_resolution" }, { awaiting_decision: true }),
        ),
      ),
    );
    renderHop();
    await clickApply();

    expect(
      await screen.findByText(
        "Duplicate probe index=1 job=job-1 back=Review|/review",
      ),
    ).toBeInTheDocument();
  });

  // Use as-is hands the album to beets to land exactly as Apply does, and beets
  // asks the duplicate question for it too (the prompt's "new" side is built
  // from the matched release OR the current files).
  test("Use as-is waits for the same question", async () => {
    server.use(
      http.get(JOB_URL, () =>
        HttpResponse.json(
          makeJob({ status: "needs_dup_resolution" }, { awaiting_decision: true }),
        ),
      ),
    );
    const user = userEvent.setup();
    renderHop();
    await user.click(await screen.findByRole("button", { name: /use as-is/i }));

    expect(
      await screen.findByText(
        "Duplicate probe index=1 job=job-1 back=Review|/review",
      ),
    ).toBeInTheDocument();
    expect(reviewRenders).toBe(0);
  });

  test("a pre-decision feed snapshot in the cache does not end the wait", async () => {
    // The list page polls this exact query key, so its last snapshot — with
    // this row still parked for review — is in the cache when the wait starts.
    server.use(
      http.get(JOB_URL, () =>
        HttpResponse.json(
          makeJob({ status: "needs_dup_resolution" }, { awaiting_decision: true }),
        ),
      ),
    );
    renderHop((qc) =>
      qc.setQueryData(
        ["import", "job", "job-1"],
        makeJob({ status: "needs_review" }),
      ),
    );
    await clickApply();

    expect(
      await screen.findByText(
        "Duplicate probe index=1 job=job-1 back=Review|/review",
      ),
    ).toBeInTheDocument();
  });

  test("an unreadable job feed sends the user back without waiting out the bound", async () => {
    server.use(http.get(JOB_URL, () => new HttpResponse(null, { status: 404 })));
    renderHop();
    await clickApply();

    // Real timers here: the bound is APPLY_NEXT_QUESTION_MS and findBy gives up
    // after 1s, so arriving is the feed error's doing and not the bound's.
    expect(await screen.findByText("Review page probe")).toBeInTheDocument();
  });

  test("an album that just lands goes back where the user came from", async () => {
    // Status stays `decided` (the registry writes it with the choice); the
    // library album id is what says beets is past the duplicate question.
    server.use(
      http.get(JOB_URL, () =>
        HttpResponse.json(makeJob({ status: "decided", album_id: 7 })),
      ),
    );
    renderHop();
    await clickApply();

    expect(await screen.findByText("Review page probe")).toBeInTheDocument();
  });

  test("a re-park for review releases the screen instead of moving it", async () => {
    server.use(
      http.get(JOB_URL, () => HttpResponse.json(makeJob({ status: "needs_review" }))),
    );
    renderHop();
    await clickApply();

    // Still here, and usable again: a stale submit re-parks the album in place
    // and the screen re-reads its match.
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /^Apply/i })).toBeEnabled(),
    );
    // The release is announced, not left silent: the user's focus was on Apply,
    // which went disabled during the hold.
    expect(screen.getByRole("status")).toHaveTextContent(
      "Match updated. Choose again.",
    );
    expect(reviewRenders).toBe(0);
  });

  // The hold's line lives in a region that is mounted from the first render and
  // only ever swaps its text: a live region that APPEARS already holding its
  // sentence is not reliably announced (the RouteAnnouncer shape).
  test("the status region is mounted before there is anything to say", async () => {
    server.use(
      http.get(JOB_URL, () => HttpResponse.json(makeJob({ status: "decided" }))),
    );
    renderHop();
    await screen.findByRole("button", { name: /^Apply/i });

    const region = screen.getByRole("status");
    expect(region).toBeInTheDocument();
    expect(region).toHaveTextContent("");
    expect(region.className).toContain("sr-only");

    await clickApply();
    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent(
        "Loading the duplicate question…",
      ),
    );
    // The SAME node, not a second one that replaced it.
    expect(screen.getByRole("status")).toBe(region);
    expect(region.className).not.toContain("sr-only");
  });

  // The stale-submit arm releases onto whatever the choice's own invalidation
  // fetched. When that GET beat the worker's re-park it 404'd, and nothing else
  // refetches this query — the screen released onto the not-found notice.
  test("a re-park after the choice's own 404 shows the fresh match, not the notice", async () => {
    let choiceMade = false;
    let served404 = false;
    server.use(
      http.post(CHOICE_URL, () => {
        choiceMade = true;
        return new HttpResponse(null, { status: 204 });
      }),
      http.get(CANDIDATE_URL, () => {
        if (choiceMade && !served404) {
          served404 = true;
          return new HttpResponse(null, { status: 404 });
        }
        if (!choiceMade) return HttpResponse.json(makeCandidate());
        const fresh = makeCandidate();
        return HttpResponse.json(
          makeCandidate({
            search_revision: 1,
            album_after: { ...fresh.album_after, album: "Amnesiac" },
            options: [
              {
                ...fresh.options[0],
                album: "Amnesiac",
                album_after: { ...fresh.album_after, album: "Amnesiac" },
              },
            ],
          }),
        );
      }),
      // The re-park lands after that 404, which is the ordering the arm exists
      // for.
      http.get(JOB_URL, async () => {
        await delay(60);
        return HttpResponse.json(makeJob({ status: "needs_review" }));
      }),
    );
    renderHop();
    await clickApply();

    expect(
      await screen.findByRole("heading", { name: /Radiohead - Amnesiac/i }),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(/isn.t waiting for review/i),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Apply/i })).toBeEnabled();
    expect(served404).toBe(true);
  });

  // The other half of that arm: the re-read takes a round trip, and the 404 it
  // replaces is still the query's error for all of it. Held open here with a
  // gate so the in-flight window is a fact of the test rather than a race.
  test("the notice stays away while the re-read is still in flight", async () => {
    let choiceMade = false;
    let served404 = false;
    let candidateGets = 0;
    let openGate: () => void = () => undefined;
    const gate = new Promise<void>((resolve) => {
      openGate = resolve;
    });
    server.use(
      http.post(CHOICE_URL, () => {
        choiceMade = true;
        return new HttpResponse(null, { status: 204 });
      }),
      http.get(CANDIDATE_URL, async () => {
        candidateGets += 1;
        if (choiceMade && !served404) {
          served404 = true;
          return new HttpResponse(null, { status: 404 });
        }
        if (choiceMade) await gate;
        return HttpResponse.json(makeCandidate());
      }),
      http.get(JOB_URL, async () => {
        await delay(60);
        return HttpResponse.json(makeJob({ status: "needs_review" }));
      }),
    );
    renderHop();
    await clickApply();

    // The 404 has landed and the release has fired its re-read, which is now
    // parked on the gate: the query is `error` AND `fetching` at this instant.
    await waitFor(() => expect(candidateGets).toBe(3));
    expect(screen.queryByText(/isn.t waiting for review/i)).toBeNull();
    expect(screen.getByRole("button", { name: /^Apply/i })).toBeInTheDocument();

    openGate();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /^Apply/i })).toBeEnabled(),
    );
  });

  // A single 5xx is not an expired job: useImportJob keeps polling through it
  // on purpose, so the hold rides the loop instead of dropping the user out.
  test("a transient feed error rides the poll instead of ending the hold", async () => {
    let calls = 0;
    server.use(
      http.get(JOB_URL, () => {
        calls += 1;
        if (calls === 1) return new HttpResponse(null, { status: 502 });
        return HttpResponse.json(
          makeJob({ status: "needs_dup_resolution" }, { awaiting_decision: true }),
        );
      }),
    );
    renderHop();
    await clickApply();

    // The retry is the poll's own next tick (IMPORT_POLL_MS = 1s), so the
    // window has to outlast one tick — and stay well inside the 5s bound, or
    // the test could not tell "rode the error" from "was rescued by the bound".
    expect(
      await screen.findByText(
        "Duplicate probe index=1 job=job-1 back=Review|/review",
        undefined,
        { timeout: 3_000 },
      ),
    ).toBeInTheDocument();
    expect(calls).toBeGreaterThanOrEqual(2);
    expect(reviewRenders).toBe(0);
  });

  // Every control on the screen, measured. During the hold the album is beets'
  // and nothing on this page may change what was submitted.
  test("the hold locks every control on the screen", async () => {
    server.use(
      http.get(JOB_URL, () => HttpResponse.json(makeJob({ status: "decided" }))),
    );
    const { container } = renderHop();
    await clickApply();
    await screen.findByText("Loading the duplicate question…");

    const controls = [
      ...container.querySelectorAll<HTMLElement>(
        "button, input, select, textarea",
      ),
    ];
    // The list is non-empty and names what it covered, so a future control that
    // slips the lock shows up here as a name rather than as a silent pass.
    const live = controls
      .filter((el) => !el.hasAttribute("disabled"))
      .map((el) => el.textContent?.trim() || el.getAttribute("aria-label") || el.tagName);
    expect(live).toEqual([]);
    expect(controls.length).toBeGreaterThanOrEqual(5);
  });

  // The pressed control is the one that reports progress: all four decisions
  // share one mutation, so an unqualified `isPending` spun Apply for a Use
  // as-is — for the whole hold, not the POST.
  test("Use as-is spins Use as-is, and leaves Apply alone", async () => {
    server.use(
      http.get(JOB_URL, () => HttpResponse.json(makeJob({ status: "decided" }))),
    );
    const user = userEvent.setup();
    renderHop();
    await user.click(await screen.findByRole("button", { name: /use as-is/i }));

    expect(
      await screen.findByRole("button", { name: /using as-is…/i }),
    ).toBeDisabled();
    expect(screen.getByRole("button", { name: /^Apply$/ })).toBeDisabled();
    expect(
      screen.queryByRole("button", { name: /applying…/i }),
    ).not.toBeInTheDocument();
  });

  // The posture belongs to the action being submitted, not to "the bar is
  // busy": @tanstack/query keeps `variables` after a mutation settles, so a
  // failed Apply left "Applying…" on the button through the NEXT relookup.
  test("a rescan after a failed Apply wears no decision's posture", async () => {
    let posts = 0;
    server.use(
      http.post(CHOICE_URL, async () => {
        posts += 1;
        if (posts === 1) return new HttpResponse(null, { status: 500 });
        // The rescan stays in flight for the whole assertion window.
        await delay("infinite");
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const user = userEvent.setup();
    renderHop();
    await clickApply();
    expect(
      await screen.findByText(/couldn.t submit that choice/i),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /rescan folder/i }));
    await waitFor(() => expect(posts).toBe(2));
    // The control half: the rescan IS in flight, so this is the window the
    // stale posture was measured in, not a quiet screen.
    expect(screen.getByRole("button", { name: /^Apply$/ })).toBeDisabled();
    expect(screen.queryByRole("button", { name: /applying…/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /using as-is…/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /skipping…/i })).toBeNull();
  });

  // The failure notice is hidden for ONE re-read — the re-park arm's. Any other
  // refetch of an errored candidate (here the search poll) must leave it up,
  // or the screen alternates between the notice and a stale locked copy of the
  // album at the poll cadence.
  test("a failed poll keeps the notice up while the next read is in flight", async () => {
    let gets = 0;
    let openGate: () => void = () => undefined;
    const gate = new Promise<void>((resolve) => {
      openGate = resolve;
    });
    server.use(
      http.get(CANDIDATE_URL, async () => {
        gets += 1;
        if (gets === 1) return HttpResponse.json(makeCandidate());
        if (gets === 2) return new HttpResponse(null, { status: 500 });
        await gate;
        return HttpResponse.json(makeCandidate());
      }),
    );
    const user = userEvent.setup();
    renderHop();
    // A rescan starts the 700ms candidate poll without bumping the revision,
    // so the reads keep coming until one lands.
    await user.click(await screen.findByRole("button", { name: /rescan folder/i }));

    expect(
      await screen.findByText(/library didn.t respond/i, undefined, { timeout: 3_000 }),
    ).toBeInTheDocument();
    // The next read is parked on the gate: the query is `error` AND `fetching`,
    // which is the instant the old `!isFetching` guard swapped the notice out.
    await waitFor(() => expect(gets).toBeGreaterThanOrEqual(3), { timeout: 3_000 });
    expect(screen.getByText(/library didn.t respond/i)).toBeInTheDocument();
    openGate();
  });

  // The re-park release has a round trip in it, and the OLD match is on screen
  // for all of it: locked, and saying nothing it cannot back up yet.
  test("the re-park re-read stays locked and silent until the new match lands", async () => {
    let choiceMade = false;
    let served404 = false;
    let gets = 0;
    let openGate: () => void = () => undefined;
    const gate = new Promise<void>((resolve) => {
      openGate = resolve;
    });
    server.use(
      http.post(CHOICE_URL, () => {
        choiceMade = true;
        return new HttpResponse(null, { status: 204 });
      }),
      http.get(CANDIDATE_URL, async () => {
        gets += 1;
        if (!choiceMade) return HttpResponse.json(makeCandidate());
        if (!served404) {
          served404 = true;
          return new HttpResponse(null, { status: 404 });
        }
        await gate;
        const fresh = makeCandidate();
        return HttpResponse.json(
          makeCandidate({
            search_revision: 1,
            album_after: { ...fresh.album_after, album: "Amnesiac" },
            options: [
              {
                ...fresh.options[0],
                album: "Amnesiac",
                album_after: { ...fresh.album_after, album: "Amnesiac" },
              },
            ],
          }),
        );
      }),
      http.get(JOB_URL, async () => {
        await delay(60);
        return HttpResponse.json(makeJob({ status: "needs_review" }));
      }),
    );
    const user = userEvent.setup();
    renderHop();
    await clickApply();

    await waitFor(() => expect(gets).toBe(3));
    // Still the old match, so: no claim that anything was updated, and no way
    // to Apply the release the worker has already replaced.
    expect(screen.getByRole("heading", { name: /Radiohead - OK Computer/i })).toBeInTheDocument();
    expect(screen.queryByText(/match updated/i)).toBeNull();
    expect(screen.getByRole("button", { name: /^Apply$/ })).toBeDisabled();

    openGate();
    expect(
      await screen.findByRole("heading", { name: /Radiohead - Amnesiac/i }),
    ).toBeInTheDocument();
    expect(screen.getByText("Match updated. Choose again.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Apply$/ })).toBeEnabled();

    // And it does not outlive the match it describes: a fresh relookup makes it
    // a statement about the previous one.
    await user.click(screen.getByRole("button", { name: /rescan folder/i }));
    await waitFor(() => expect(screen.queryByText(/match updated/i)).toBeNull());
  });

  test("the wait says so in one line, locks the decisions, and is bounded", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      // A row the worker never answers for: beets is still between the two
      // questions, so nothing on the feed ends the wait.
      server.use(
        http.get(JOB_URL, () => HttpResponse.json(makeJob({ status: "decided" }))),
      );
      const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
      renderHop();
      await user.click(await screen.findByRole("button", { name: /^Apply/i }));

      expect(
        await screen.findByText("Loading the duplicate question…"),
      ).toBeInTheDocument();
      expect(screen.getByRole("button", { name: /^Applying/i })).toBeDisabled();
      expect(screen.getByRole("button", { name: /^Skip/i })).toBeDisabled();
      expect(screen.getByRole("button", { name: /use as-is/i })).toBeDisabled();
      expect(screen.getByRole("button", { name: /rescan folder/i })).toBeDisabled();
      expect(reviewRenders).toBe(0);

      // The bound is five ticks of the active-import poll (1s), so the
      // literals below straddle 5s. Read as literals on purpose: advancing by
      // the constant itself would move with any value it is given.
      expect(APPLY_NEXT_QUESTION_MS).toBe(5_000);
      await vi.advanceTimersByTimeAsync(4_000);
      expect(screen.queryByText("Review page probe")).not.toBeInTheDocument();
      expect(screen.getByText("Loading the duplicate question…")).toBeVisible();

      await vi.advanceTimersByTimeAsync(1_500);
      expect(await screen.findByText("Review page probe")).toBeInTheDocument();
      expect(screen.getByText("Arrived by REPLACE")).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  // The other reason to hold reads differently: nothing is known to be coming,
  // only that nothing has ruled it out. Pinned as the complement of the line in
  // the bounded-wait test above, so a single hard-coded sentence fails one of
  // the two.
  test("an unanswered check gets the neutral line, not the duplicate one", async () => {
    server.use(
      http.get(DUPLICATES_URL, async () => {
        await delay("infinite");
        return HttpResponse.json({ existing: [] });
      }),
      // Nothing on the feed ends the wait, so the line stays up to be read.
      http.get(JOB_URL, () => HttpResponse.json(makeJob({ status: "decided" }))),
    );
    renderHop();
    await clickApply();

    expect(
      await screen.findByText("Checking for a duplicate…"),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("Loading the duplicate question…"),
    ).not.toBeInTheDocument();
  });
});
