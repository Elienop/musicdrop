import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { delay, http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router";
import { beforeEach, describe, expect, test } from "vitest";

import type { Candidate } from "@/api/useImport";
import { ImportCandidatePage } from "@/pages/import/ImportCandidatePage";
import { unwiredContainerQueries } from "@/test/containerQuery";
import { renderWithProviders } from "@/test/render";
import { server } from "@/test/msw-server";

const CANDIDATE_URL = `${window.location.origin}/api/import/job-1/albums/1`;
const CHOICE_URL = `${window.location.origin}/api/import/job-1/albums/1/choice`;
const DUPLICATES_URL = `${window.location.origin}/api/import/:jobId/albums/:index/duplicates`;

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
  beforeEach(() => {
    server.use(
      http.get(DUPLICATES_URL, () => HttpResponse.json({ existing: [] })),
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
  // viewport's. jsdom computes no layout, so the widths that chose 27rem are
  // browser-measured and recorded in the component; what a test CAN hold is
  // that the variants are wired to a declared container — rename one side and
  // CSS reports nothing, the panel silently keeps one arm.
  test("wires every container-query variant to a declared container", async () => {
    server.use(http.get(CANDIDATE_URL, () => HttpResponse.json(makeCandidate())));
    const { container } = renderAt();

    await screen.findByText("Paranoid Android");
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

    // Open the folded search row first (the toggle is never disabled), so the
    // release field is mounted and we can assert it locks with the rest.
    await user.click(await screen.findByRole("button", { name: /different release/i }));
    await user.click(screen.getByRole("button", { name: /^Apply/i }));
    // With the Apply in flight, the no-undo relookup controls must lock out —
    // firing a rescan/search against the same album mid-Apply is an avoidable
    // concurrent-action window the backend can only swallow.
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /rescan folder/i })).toBeDisabled(),
    );
    expect(screen.getByLabelText(/release url or id/i)).toBeDisabled();
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
