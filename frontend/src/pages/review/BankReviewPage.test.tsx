import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { delay, http, HttpResponse } from "msw";
import { beforeEach, describe, expect, test, vi } from "vitest";

import { BankReviewPage } from "@/pages/review/BankReviewPage";
import { renderWithProviders } from "@/test/render";
import { server } from "@/test/msw-server";

const { mockNavigate } = vi.hoisted(() => ({ mockNavigate: vi.fn() }));
vi.mock("react-router", async (importOriginal) => ({
  ...(await importOriginal<typeof import("react-router")>()),
  useNavigate: () => mockNavigate,
}));

const O = window.location.origin;
const ITEM = `${O}/api/bank/:itemId`;
const DECISION = `${O}/api/bank/:itemId/decision`;
const DUP = `${O}/api/bank/:itemId/duplicates`;
const SEARCH = `${O}/api/bank/:itemId/search`;
const RESCAN = `${O}/api/bank/:itemId/rescan`;
const ACTIVE = `${O}/api/imports/active`;
const IMPORT_URL = `${O}/api/import`;

/** One in-library album the candidate would collide with. */
const existingAlbum = {
  album_id: 7,
  album_artist: "Boards of Canada",
  album: "Music Has the Right to Children",
  year: 1998,
  track_count: 17,
  format: "MP3",
  bitrate_kbps: 320,
  folder: "/library/BoC/MHTRTC",
  release: null,
  tracks: [
    { track: 1, disc: 1, title: "Echoes", format: "FLAC", bitrate_kbps: 987 },
  ],
};

/** A complete banked Candidate — the exact generated shape (every field). */
const candidate = {
  confidence: 71.2,
  recommendation: "medium",
  data_source: "MusicBrainz",
  data_url: null,
  cover_after_url: null,
  has_current_art: true, // true on purpose: the bank page must STILL show no live art
  changed_fields: ["album"],
  album_before: { artist: "Boards of Canada", album: "MHTRTC", year: 1998, label: null, media: null, country: null },
  album_after: { artist: "Boards of Canada", album: "Music Has the Right to Children", year: 1998, label: "Warp", media: "CD", country: "GB" },
  tracks: [
    { index: 1, title_before: "01 wildlife", title_after: "Wildlife Analysis", track_before: 1, track_after: 1, status: "changed" },
  ],
  missing: [],
  unmatched: [],
  options: [
    // options[0] mirrors the candidate's top diff (selected=0 renders identically).
    {
      index: 0, confidence: 71.2, data_source: "MusicBrainz", disambiguation: null, release_id: "mb-1",
      album_artist: "Boards of Canada", album: "Music Has the Right to Children", year: 1998,
      album_after: { artist: "Boards of Canada", album: "Music Has the Right to Children", year: 1998, label: "Warp", media: "CD", country: "GB" },
      changed_fields: ["album"],
      tracks: [{ index: 1, title_before: "01 wildlife", title_after: "Wildlife Analysis", track_before: 1, track_after: 1, status: "changed" }],
      missing: [], unmatched: [], cover_after_url: null, data_url: null,
    },
    // options[1] carries a DISTINCT per-release diff so a switch re-renders.
    {
      index: 1, confidence: 64.0, data_source: "MusicBrainz", disambiguation: "remaster", release_id: "mb-2",
      album_artist: "Boards of Canada", album: "Music Has the Right to Children (Remaster)", year: 2004,
      album_after: { artist: "Boards of Canada", album: "Music Has the Right to Children (Remaster)", year: 2004, label: "Warp", media: "CD", country: "GB" },
      changed_fields: ["album", "year"],
      tracks: [{ index: 1, title_before: "01 wildlife", title_after: "Wildlife Analysis (Remaster)", track_before: 1, track_after: 1, status: "changed" }],
      missing: [], unmatched: [], cover_after_url: null, data_url: null,
    },
  ],
};

function bankItem(over: Record<string, unknown> = {}) {
  return {
    id: "b1",
    folder: "/inbox/BoC",
    source: "sweep",
    reason: "needs_review",
    artist: "Boards of Canada",
    album: "MHTRTC",
    recommendation: "medium",
    confidence: 71.2,
    parked: { album_index: 0, folder: "/inbox/BoC", candidate },
    duplicate: null,
    fingerprint: "f1",
    status: "needs_review",
    decided: null,
    error: null,
    album_id: null,
    banked_at: "2026-06-12T08:00:00Z",
    decided_at: null,
    resolved_at: null,
    ...over,
  };
}

const duplicatePrompt = {
  album_index: 0,
  incoming: { album: "Echoes", album_artist: "P F", year: 2001, track_count: 26, format: "FLAC", bitrate_kbps: 900, folder: "/inbox/Echoes", has_current_art: false },
  existing: [{ album_id: 7, album: "Echoes", album_artist: "P F", year: 2001, track_count: 26, format: "MP3", bitrate_kbps: 320, folder: "/library/P F/Echoes" }],
};

function renderRow(id = "b1") {
  return renderWithProviders(<BankReviewPage />, {
    route: `/review/bank/${id}`,
    path: "/review/bank/:itemId",
  });
}

describe("BankReviewPage", () => {
  beforeEach(() => {
    mockNavigate.mockClear();
    server.use(
      http.get(ACTIVE, () => HttpResponse.json({ active: false, origin: "manual", needs_review_count: 0 })),
      // No collision by default — candidate-screen tests get the normal footer;
      // collision tests override this to return an existing album.
      http.get(DUP, () => HttpResponse.json({ existing: [] })),
    );
  });

  test("renders the banked candidate INSTANTLY from the payload — no job endpoints", async () => {
    // Only the bank-row handler exists; any request to /api/import/* would be
    // unhandled and fail the test (MSW errors on unhandled requests).
    server.use(http.get(ITEM, () => HttpResponse.json(bankItem())));
    renderRow();
    expect(
      await screen.findByRole("heading", { name: /Music Has the Right to Children/ }),
    ).toBeInTheDocument();
    // Exact match: the header's confidence span is "71%"; the candidate
    // switcher's option text ("71% · MusicBrainz") would also match a regex.
    expect(screen.getByText("71%")).toBeInTheDocument();
    expect(screen.getByText("Wildlife Analysis")).toBeInTheDocument();
  });

  test("Apply posts the SELECTED candidate index and navigates back to Review", async () => {
    let body: unknown = null;
    server.use(
      http.get(ITEM, () => HttpResponse.json(bankItem())),
      http.post(DECISION, async ({ request }) => {
        body = await request.json();
        return HttpResponse.json(bankItem({ status: "queued", decided: { action: "apply", candidate_index: 1, duplicate_action: null } }));
      }),
    );
    renderRow();
    await screen.findByRole("heading", { name: /Music Has the Right/ });
    await userEvent.selectOptions(screen.getByLabelText("Candidate release"), "1");
    // The preview re-renders for the selected release (the Remaster diff).
    expect(
      await screen.findByText("Music Has the Right to Children (Remaster)"),
    ).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /apply/i }));
    await waitFor(() => expect(mockNavigate).toHaveBeenCalledWith("/review"));
    expect(body).toEqual({ action: "apply", candidate_index: 1 });
  });

  test("Ignore posts {action: ignore}", async () => {
    let body: unknown = null;
    server.use(
      http.get(ITEM, () => HttpResponse.json(bankItem())),
      http.post(DECISION, async ({ request }) => {
        body = await request.json();
        return HttpResponse.json(bankItem({ status: "ignored" }));
      }),
    );
    renderRow();
    await screen.findByRole("heading", { name: /Music Has the Right/ });
    await userEvent.click(screen.getByRole("button", { name: /^ignore$/i }));
    await waitFor(() => expect(body).toEqual({ action: "ignore" }));
  });

  test("a duplicate row renders both panels and posts the duplicate action", async () => {
    let body: unknown = null;
    server.use(
      http.get(ITEM, () =>
        HttpResponse.json(bankItem({ reason: "needs_dup_resolution", parked: null, duplicate: duplicatePrompt, recommendation: null, confidence: null })),
      ),
      http.post(DECISION, async ({ request }) => {
        body = await request.json();
        return HttpResponse.json(bankItem({ reason: "needs_dup_resolution", parked: null, duplicate: duplicatePrompt, status: "queued", decided: { action: "duplicate", candidate_index: null, duplicate_action: "merge" } }));
      }),
    );
    renderRow();
    expect(await screen.findByRole("heading", { name: /already in your library/i })).toBeInTheDocument();
    expect(screen.getAllByText("Echoes").length).toBeGreaterThanOrEqual(2);
    await userEvent.click(screen.getByRole("button", { name: /merge/i }));
    await waitFor(() => expect(mockNavigate).toHaveBeenCalledWith("/review"));
    expect(body).toEqual({ action: "duplicate", duplicate_action: "merge" });
  });

  /**
   * The re-match label on the bank duplicate screen. LEGACY rows (banked
   * before the sweep stored the candidate payload) cannot pin their apply to
   * the release on screen, so the apply re-runs the match online and may land
   * a different one — these rows must say so. Post-fix rows pin, and must NOT.
   *
   * The expected copy is written out here on PURPOSE rather than imported from
   * the page: a shared constant would follow any production edit and the
   * assertion would never fail. This literal is the drift alarm.
   */
  describe("the legacy re-match label", () => {
    const LEGACY_NOTE =
      "No release id is stored for this row: importing it re-matches the folder online, so the release that lands may differ from the one shown. Rescan the folder to see a fresh match.";

    /** A bank duplicate row; `parked` defaults to the POST-FIX payload
     * (options[0].release_id === "mb-1"), so tests opt INTO legacy shapes. */
    function dupRow(over: Record<string, unknown> = {}) {
      return bankItem({
        reason: "needs_dup_resolution",
        duplicate: duplicatePrompt,
        recommendation: null,
        confidence: null,
        ...over,
      });
    }

    test("a LEGACY row (no parked payload) shows the label", async () => {
      server.use(http.get(ITEM, () => HttpResponse.json(dupRow({ parked: null }))));
      renderRow();
      await screen.findByRole("heading", { name: /already in your library/i });
      expect(screen.getByText(LEGACY_NOTE)).toBeInTheDocument();
    });

    test("a POST-FIX row (options[0].release_id set) does NOT show it, and still posts the same body", async () => {
      let body: unknown = null;
      server.use(
        http.get(ITEM, () => HttpResponse.json(dupRow())),
        http.post(DECISION, async ({ request }) => {
          body = await request.json();
          return HttpResponse.json(dupRow({ status: "queued" }));
        }),
      );
      renderRow();
      // Positive control: the screen really rendered, so the absence below is
      // a real absence rather than an unmounted page.
      await screen.findByRole("heading", { name: /already in your library/i });
      expect(screen.queryByText(LEGACY_NOTE)).not.toBeInTheDocument();

      // Pinning changes what the APPLY does, never the decision payload — the
      // legacy side of this is already pinned by the duplicate test above.
      await userEvent.click(screen.getByRole("button", { name: /merge/i }));
      await waitFor(() => expect(mockNavigate).toHaveBeenCalledWith("/review"));
      expect(body).toEqual({ action: "duplicate", duplicate_action: "merge" });
    });

    test("an ID-LESS SOURCE row (options[0].release_id null) shows the label", async () => {
      // options[1] KEEPS its release_id, so this also proves the predicate
      // reads index 0 specifically — not "some option lacks an id".
      const idless = {
        ...candidate,
        options: [
          { ...candidate.options[0], release_id: null },
          ...candidate.options.slice(1),
        ],
      };
      server.use(
        http.get(ITEM, () =>
          HttpResponse.json(
            dupRow({ parked: { album_index: 0, folder: "/inbox/BoC", candidate: idless } }),
          ),
        ),
      );
      renderRow();
      await screen.findByRole("heading", { name: /already in your library/i });
      expect(screen.getByText(LEGACY_NOTE)).toBeInTheDocument();
    });

    test("a row with NO candidate options shows the label", async () => {
      // The clause the implementation folds into the optional chain
      // (`options[0]` undefined → `== null`). Pinned so the simplification
      // cannot silently regress into a crash or a missing label.
      server.use(
        http.get(ITEM, () =>
          HttpResponse.json(
            dupRow({
              parked: {
                album_index: 0,
                folder: "/inbox/BoC",
                candidate: { ...candidate, options: [] },
              },
            }),
          ),
        ),
      );
      renderRow();
      await screen.findByRole("heading", { name: /already in your library/i });
      expect(screen.getByText(LEGACY_NOTE)).toBeInTheDocument();
    });

    test("a FAILED legacy row still shows it — the label is keyed on stored data, not status", async () => {
      // A failed apply re-enters this screen for a retry; the row is still
      // unpinned, so the honesty note must survive the status change.
      server.use(
        http.get(ITEM, () =>
          HttpResponse.json(dupRow({ parked: null, status: "failed", error: "duplicate block" })),
        ),
      );
      renderRow();
      await screen.findByRole("heading", { name: /already in your library/i });
      expect(screen.getByText(LEGACY_NOTE)).toBeInTheDocument();
    });
  });

  describe("the candidate screen's unpinned-option note", () => {
    const OPTION_NOTE =
      "The selected match has no stored release id: Apply re-matches the folder online, so the release that lands may differ from the one shown.";

    test("shows when the SELECTED option lacks a release id, even though another option has one", async () => {
      const idless = {
        ...candidate,
        options: [
          { ...candidate.options[0], release_id: null },
          candidate.options[1], // keeps "mb-2" — pins selected-index keying
        ],
      };
      server.use(
        http.get(ITEM, () =>
          HttpResponse.json(
            bankItem({ parked: { album_index: 0, folder: "/inbox/BoC", candidate: idless } }),
          ),
        ),
      );
      renderRow();
      await screen.findAllByText(/after import/i);
      expect(screen.getByText(OPTION_NOTE)).toBeInTheDocument();
    });

    test("absent when the selected option carries an id, and follows a switch both ways", async () => {
      const mixed = {
        ...candidate,
        options: [
          candidate.options[0], // "mb-1" — pinned
          { ...candidate.options[1], release_id: null }, // id-less
        ],
      };
      server.use(
        http.get(ITEM, () =>
          HttpResponse.json(
            bankItem({ parked: { album_index: 0, folder: "/inbox/BoC", candidate: mixed } }),
          ),
        ),
      );
      renderRow();
      await screen.findAllByText(/after import/i);
      // Default selection (index 0) has an id: no note.
      expect(screen.queryByText(OPTION_NOTE)).not.toBeInTheDocument();
      // Switch to the id-less option: the note follows the SELECTION.
      fireEvent.change(screen.getByRole("combobox", { name: /candidate release/i }), {
        target: { value: "1" },
      });
      expect(screen.getByText(OPTION_NOTE)).toBeInTheDocument();
      // And back: it clears again.
      fireEvent.change(screen.getByRole("combobox", { name: /candidate release/i }), {
        target: { value: "0" },
      });
      expect(screen.queryByText(OPTION_NOTE)).not.toBeInTheDocument();
    });
  });

  test("a no_match row shows the folder and offers as-is / as tracks / ignore", async () => {
    let body: unknown = null;
    server.use(
      http.get(ITEM, () =>
        HttpResponse.json(bankItem({ reason: "no_match", parked: null, recommendation: null, confidence: null, album: null, artist: null })),
      ),
      http.post(DECISION, async ({ request }) => {
        body = await request.json();
        return HttpResponse.json(bankItem({ reason: "no_match", parked: null, status: "queued", decided: { action: "asis", candidate_index: null, duplicate_action: null } }));
      }),
    );
    renderRow();
    expect(await screen.findByText("/inbox/BoC")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /as tracks/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^ignore$/i })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /use as-is/i }));
    await waitFor(() => expect(body).toEqual({ action: "asis" }));
  });

  test("rescues a no-match row: search swaps in a candidate screen", async () => {
    const noMatchRow = bankItem({
      reason: "no_match",
      parked: null,
      duplicate: null,
      recommendation: null,
      confidence: null,
      album: null,
      artist: null,
    });
    // The search lands a real needs_review row (parked payload) on the SAME id.
    const rescued = bankItem();
    server.use(
      http.get(ITEM, () => HttpResponse.json(noMatchRow)),
      http.post(SEARCH, () => HttpResponse.json({ item: rescued, found: true })),
    );
    renderRow();
    await screen.findByRole("heading", { name: /no match found/i });
    // The no-match screen opens the search inline (defaultOpen) — no toggle.
    expect(
      screen.getByRole("form", { name: /search for a different release/i }),
    ).toBeInTheDocument();
    fireEvent.change(
      screen.getByRole("textbox", { name: /release url or id/i }),
      { target: { value: "https://musicbrainz.org/release/x" } },
    );
    fireEvent.click(screen.getByRole("button", { name: /^search$/i }));
    // The cache write re-branches the SAME route to the candidate screen (the
    // "After import" panel heading + tracklist column both mark it).
    expect((await screen.findAllByText(/after import/i)).length).toBeGreaterThan(0);
  });

  test("shows the no-hit feedback and keeps the screen", async () => {
    const noMatchRow = bankItem({
      reason: "no_match",
      parked: null,
      duplicate: null,
      recommendation: null,
      confidence: null,
      album: null,
      artist: null,
    });
    server.use(
      http.get(ITEM, () => HttpResponse.json(noMatchRow)),
      http.post(SEARCH, () => HttpResponse.json({ item: noMatchRow, found: false })),
    );
    renderRow();
    await screen.findByRole("heading", { name: /no match found/i });
    fireEvent.change(
      screen.getByRole("textbox", { name: /release url or id/i }),
      { target: { value: "bad-id" } },
    );
    fireEvent.click(screen.getByRole("button", { name: /^search$/i }));
    expect(
      await screen.findByText(/no release found/i),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: /no match found/i }),
    ).toBeInTheDocument();
  });

  test("surfaces a 409 search conflict and refetches the row", async () => {
    server.use(
      http.get(ITEM, () => HttpResponse.json(bankItem())),
      http.post(SEARCH, () =>
        HttpResponse.json(
          { detail: "the folder changed since it was banked" },
          { status: 409 },
        ),
      ),
    );
    renderRow();
    await screen.findAllByText(/after import/i);
    // The candidate screen folds the search behind the "Different release"
    // toggle — open it before typing (the no-match screen is pre-opened).
    await userEvent.click(screen.getByRole("button", { name: /different release/i }));
    fireEvent.change(
      screen.getByRole("textbox", { name: /release url or id/i }),
      { target: { value: "x" } },
    );
    fireEvent.click(screen.getByRole("button", { name: /^search$/i }));
    expect(
      await screen.findByText(/folder changed since it was banked/i),
    ).toBeInTheDocument();
  });

  test("a detected duplicate shows the up-front resolver and resolves in one decision", async () => {
    let body: unknown = null;
    server.use(
      http.get(ITEM, () => HttpResponse.json(bankItem())),
      http.get(DUP, () => HttpResponse.json({ existing: [existingAlbum] })),
      http.post(DECISION, async ({ request }) => {
        body = await request.json();
        return HttpResponse.json(bankItem({ status: "queued", decided: { action: "duplicate", candidate_index: 0, duplicate_action: "replace" } }));
      }),
    );
    renderRow();
    await screen.findByRole("heading", { name: /Music Has the Right/ });
    // The up-front collision card + a View link to the existing copy. Pin the
    // heading role — the bar's notice line also matches /already in your library/i.
    expect(
      await screen.findByRole("heading", { name: /already in your library/i }),
    ).toBeInTheDocument();
    // The bar carries an in-line notice line while the collision fold is active.
    expect(
      screen.getByText("This album is already in your library."),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /^view$/i })).toHaveAttribute("href", "/albums/7");
    // The existing copy's own tracklist is shown so the choice is informed.
    const section = within(
      screen.getByRole("region", { name: /already in your library/i }),
    );
    expect(section.getByText("Echoes")).toBeInTheDocument();
    expect(section.getByText(/FLAC · 987 kbps/)).toBeInTheDocument();
    // The collision fold drops Apply + the apply-style actions but keeps the
    // rest of the control bar (Ignore, the search toggle, and Rescan).
    expect(screen.queryByRole("button", { name: /^apply$/i })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^ignore$/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /different release/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /rescan folder/i })).toBeInTheDocument();
    // One click both pins the selected release and resolves the collision.
    await userEvent.click(screen.getByRole("button", { name: /replace old/i }));
    await waitFor(() => expect(mockNavigate).toHaveBeenCalledWith("/review"));
    expect(body).toEqual({ action: "duplicate", candidate_index: 0, duplicate_action: "replace" });
  });

  test("no detected duplicate keeps the normal apply footer", async () => {
    // The beforeEach default returns { existing: [] }.
    server.use(http.get(ITEM, () => HttpResponse.json(bankItem())));
    renderRow();
    await screen.findByRole("heading", { name: /Music Has the Right/ });
    expect(screen.getByRole("button", { name: /apply/i })).toBeInTheDocument();
    expect(screen.queryByText(/already in your library/i)).not.toBeInTheDocument();
  });

  test("while the duplicate check is in flight Apply is disabled and a checking hint shows", async () => {
    server.use(
      http.get(ITEM, () => HttpResponse.json(bankItem())),
      // The check never lands — the footer must hold the apply-style actions.
      http.get(DUP, async () => {
        await delay("infinite");
        return HttpResponse.json({ existing: [] });
      }),
    );
    renderRow();
    await screen.findByRole("heading", { name: /Music Has the Right/ });
    expect(screen.getByText(/checking your library/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /apply/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /use as-is/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /as tracks/i })).toBeDisabled();
    // Ignore is always safe — it never queues an apply.
    expect(screen.getByRole("button", { name: /^ignore$/i })).toBeEnabled();
  });

  test("a failed row whose re-check errors still offers the four duplicate actions", async () => {
    server.use(
      http.get(ITEM, () =>
        HttpResponse.json(
          bankItem({
            status: "failed",
            error: "the apply imported nothing - decide again",
            decided: { action: "apply", candidate_index: 0, duplicate_action: null },
          }),
        ),
      ),
      // The re-check itself fails — the dup actions are the documented fallback.
      http.get(DUP, () => HttpResponse.json({ detail: "boom" }, { status: 500 })),
    );
    renderRow();
    await screen.findByRole("heading", { name: /Music Has the Right/ });
    for (const name of [/skip new/i, /keep both/i, /replace old/i, /merge/i]) {
      expect(await screen.findByRole("button", { name })).toBeInTheDocument();
    }
  });

  test("a failed row with no collision shows the error and the normal retry footer", async () => {
    server.use(
      http.get(ITEM, () =>
        HttpResponse.json(bankItem({ status: "failed", error: "the apply imported nothing (the lookup may have failed transiently) - decide again", decided: { action: "apply", candidate_index: 0, duplicate_action: null } })),
      ),
    );
    renderRow();
    expect(await screen.findByRole("alert")).toHaveTextContent(/imported nothing/);
    expect(screen.getByRole("button", { name: /apply/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /replace old/i })).not.toBeInTheDocument();
  });

  test("a decision 409 shows the backend's reason inline (string detail tolerated)", async () => {
    server.use(
      http.get(ITEM, () => HttpResponse.json(bankItem())),
      http.post(DECISION, () =>
        HttpResponse.json({ detail: "row is queued; decisions need one of ['failed', 'needs_review', 'stale']" }, { status: 409 }),
      ),
    );
    renderRow();
    await screen.findByRole("heading", { name: /Music Has the Right/ });
    await userEvent.click(screen.getByRole("button", { name: /apply/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/row is queued/);
    expect(mockNavigate).not.toHaveBeenCalled();
  });

  // `incremental: false` is beets' `-I`. Banking the folder recorded it in
  // beets' import history, and this screen deletes the row on success — so
  // without the override a keep-downloads run skips every album here and the
  // album is in neither the bank nor the library.
  test("a stale row offers Review now: starts an attended import of the folder past beets' history, deletes the row, navigates", async () => {
    let importBody: unknown = null;
    let deleted = false;
    server.use(
      http.get(ITEM, () => HttpResponse.json(bankItem({ status: "stale", error: "the folder changed since it was banked - nothing was imported; re-sweep or remove the row" }))),
      http.post(IMPORT_URL, async ({ request }) => {
        importBody = await request.json();
        return HttpResponse.json({ job_id: "jx" });
      }),
      http.delete(ITEM, () => {
        deleted = true;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    renderRow();
    await userEvent.click(await screen.findByRole("button", { name: /review now/i }));
    await waitFor(() => expect(mockNavigate).toHaveBeenCalledWith("/import?job=jx"));
    expect(importBody).toEqual({
      path: "/inbox/BoC",
      options: {
        operation: "default",
        unattended: false,
        sweep: false,
        incremental: false,
      },
    });
    expect(deleted).toBe(true);
  });

  test("while the re-scan is starting Review now is aria-disabled, not disabled, and swallows the repeat", async () => {
    // The Pagination rule (the import page's Pause button carries the
    // measurement): this button holds focus when it is pressed, so disabling it
    // on that commit strands keyboard focus on <body> — and the failure sentence
    // right above it invites another press. The import-slot gate stays a real
    // `disabled`, pinned by the test below.
    let posts = 0;
    server.use(
      http.get(ITEM, () => HttpResponse.json(bankItem({ status: "stale", error: "changed" }))),
      http.post(IMPORT_URL, async () => {
        posts += 1;
        await delay("infinite");
        return HttpResponse.json({ job_id: "jx" });
      }),
    );
    renderRow();
    const button = await screen.findByRole("button", { name: /review now/i });
    await userEvent.click(button);

    await waitFor(() => expect(button).toHaveAttribute("aria-disabled", "true"));
    expect(button).not.toBeDisabled();

    await userEvent.click(button);
    expect(posts).toBe(1);
  });

  test("a stale-row start failure carries the server's own reason", async () => {
    // One of the three refusals behind this status. A hard-coded "an import is
    // already running" sent the user off to wait for an import that was not
    // running.
    server.use(
      http.get(ITEM, () => HttpResponse.json(bankItem({ status: "stale", error: "changed" }))),
      http.post(IMPORT_URL, () =>
        HttpResponse.json(
          { detail: "A library operation is in progress; import available when it finishes" },
          { status: 409 },
        ),
      ),
    );
    renderRow();
    await userEvent.click(await screen.findByRole("button", { name: /review now/i }));
    // By text, not by role: the stale screen's own warning banner is an alert
    // too, so the role alone matches two nodes here. The full stop is the
    // client's — server details carry none, every sentence in the app does, and
    // `startErrorSentence` is the one place the two conventions meet.
    const alert = await screen.findByText(
      "A library operation is in progress; import available when it finishes.",
    );
    expect(alert).toHaveAttribute("role", "alert");
    // A 503 on this arm carries repr'd paths, which Chromium will not break
    // at `/` (the Trash page measured the same family at 320px).
    expect(alert).toHaveClass("break-words");
    expect(screen.queryByText(/an import is already running;/i)).not.toBeInTheDocument();
    // The button keeps focus through the failure, so the sentence describes it.
    expect(
      screen.getByRole("button", { name: /review now/i }),
    ).toHaveAttribute("aria-describedby", alert.id);
    expect(alert.id).not.toBe("");
  });

  // The 409 and the hint open on the same clause, and the hint is the fuller
  // sentence (it names the slot AND what still works), so once the probe
  // catches up the alert stands down rather than repeating it one line above.
  test("the start alert stands down once the import-slot hint is up", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      let started = false;
      server.use(
        http.get(ITEM, () => HttpResponse.json(bankItem({ status: "stale", error: "changed" }))),
        http.post(IMPORT_URL, () => {
          started = true;
          return HttpResponse.json(
            { detail: "An import is already running" },
            { status: 409 },
          );
        }),
        http.get(ACTIVE, () =>
          HttpResponse.json(
            started
              ? { active: true, job_id: "j9", origin: "manual", needs_review_count: 0 }
              : { active: false, origin: "manual", needs_review_count: 0 },
          ),
        ),
      );
      const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
      renderRow();
      await user.click(await screen.findByRole("button", { name: /review now/i }));
      // The probe still reads idle, so the alert is the only thing that speaks.
      expect(
        await screen.findByText("An import is already running."),
      ).toBeInTheDocument();

      // The idle probe cadence is 30s (useActiveImport).
      await vi.advanceTimersByTimeAsync(31_000);
      expect(await screen.findByText(/needs the import slot/i)).toBeInTheDocument();
      expect(
        screen.queryByText("An import is already running."),
      ).not.toBeInTheDocument();
      expect(
        screen.getByRole("button", { name: /review now/i }),
      ).toHaveAttribute("aria-describedby", "stale-rescan-hint");
    } finally {
      vi.useRealTimers();
    }
  });

  test("the stale re-scan is gated while an import runs — with the visible reason", async () => {
    server.use(
      http.get(ACTIVE, () => HttpResponse.json({ active: true, job_id: "j9", origin: "manual", needs_review_count: 0 })),
      http.get(ITEM, () => HttpResponse.json(bankItem({ status: "stale", error: "changed" }))),
    );
    renderRow();
    await waitFor(() => expect(screen.getByRole("button", { name: /review now/i })).toBeDisabled());
    expect(screen.getByText(/needs the import slot/i)).toBeInTheDocument();
  });

  test("queued and done rows render their notices (no decision actions)", async () => {
    server.use(http.get(ITEM, () => HttpResponse.json(bankItem({ status: "queued", decided: { action: "apply", candidate_index: 0, duplicate_action: null } }))));
    renderRow();
    expect(await screen.findByText(/queued to apply/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /apply/i })).not.toBeInTheDocument();

    server.use(http.get(ITEM, () => HttpResponse.json(bankItem({ status: "done", album_id: 7, decided: { action: "apply", candidate_index: 0, duplicate_action: null } }))));
    const second = renderWithProviders(<BankReviewPage />, { route: "/review/bank/b1", path: "/review/bank/:itemId" });
    expect(await within(second.container).findByText(/imported/i)).toBeInTheDocument();
    expect(within(second.container).getByRole("link", { name: /view album/i })).toHaveAttribute("href", "/albums/7");
  });

  /**
   * The queued body must stay true for EVERY decision the runner can carry
   * out. It used to promise "This decision waits for the import slot. Files
   * move automatically" — both clauses are false for an ENFORCED skip_new,
   * which imports nothing: it never takes the import slot and moves no files.
   * The fixture therefore queues exactly that decision.
   *
   * Written out rather than imported from the page, per the convention below:
   * a shared constant would follow any production edit and never fail.
   */
  test("the queued notice promises only what holds for every decision", async () => {
    server.use(
      http.get(ITEM, () =>
        HttpResponse.json(bankItem({ status: "queued", decided: { action: "duplicate", candidate_index: null, duplicate_action: "skip_new" } })),
      ),
    );
    renderRow();
    expect(
      await screen.findByText("This decision will be applied automatically; no further action needed."),
    ).toBeInTheDocument();
    expect(screen.queryByText(/files move automatically/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/waits for the import slot/i)).not.toBeInTheDocument();
  });

  test("a skip_new dup resolution says it kept the existing copy, not 'landed'", async () => {
    server.use(
      http.get(ITEM, () =>
        HttpResponse.json(bankItem({ status: "done", album_id: null, decided: { action: "duplicate", candidate_index: 0, duplicate_action: "skip_new" } })),
      ),
    );
    renderRow();
    expect(await screen.findByText(/kept your existing copy/i)).toBeInTheDocument();
    expect(screen.queryByText(/landed in your library/i)).not.toBeInTheDocument();
    // No album landed — the settled notice offers Remove, not View album.
    expect(screen.getByRole("button", { name: /remove from bank/i })).toBeInTheDocument();
  });

  /**
   * The OTHER half of the skip_new outcome, and the reason that arm reads
   * `album_id` instead of the decision alone. An enforced skip_new resolves
   * without importing only while the stored library copy SURVIVES; when the
   * user deleted or moved it between banking and applying there is nothing
   * left to keep, so the import runs and LANDS — status done, album_id set,
   * decision still skip_new. Keyed on the decision alone this notice would
   * print "Nothing new was imported" directly beside a View-album button for
   * the album that just imported.
   *
   * Pinned both ways: the null side is the test directly above (unchanged).
   * The copy is written out here on PURPOSE rather than imported from the
   * page — a shared constant would follow any production edit and the
   * assertion would never fail. These literals are the drift alarm.
   */
  test("a skip_new whose kept copy was GONE says it imported, and links the landed album", async () => {
    server.use(
      http.get(ITEM, () =>
        HttpResponse.json(bankItem({ status: "done", album_id: 7, decided: { action: "duplicate", candidate_index: 0, duplicate_action: "skip_new" } })),
      ),
    );
    renderRow();
    expect(
      await screen.findByText("Imported — your existing copy was gone"),
    ).toBeInTheDocument();
    // The body names the row (artist - album), so a mutation that drops the
    // label fails here too.
    expect(
      screen.getByText(
        "The copy you chose to keep was no longer in your library, so Boards of Canada - MHTRTC was imported.",
      ),
    ).toBeInTheDocument();
    // The self-contradiction this branch exists to prevent.
    expect(screen.queryByText(/nothing new was imported/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/kept your existing copy/i)).not.toBeInTheDocument();
    // The action stays the landed album's — not the Remove button the
    // no-album side offers.
    expect(screen.getByRole("link", { name: /view album/i })).toHaveAttribute("href", "/albums/7");
    expect(screen.queryByRole("button", { name: /remove from bank/i })).not.toBeInTheDocument();
  });

  test("a replace dup resolution says it replaced the old copy", async () => {
    server.use(
      http.get(ITEM, () =>
        HttpResponse.json(bankItem({ status: "done", album_id: 7, decided: { action: "duplicate", candidate_index: 0, duplicate_action: "replace" } })),
      ),
    );
    renderRow();
    expect(await screen.findByText(/^replaced$/i)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /view album/i })).toHaveAttribute("href", "/albums/7");
  });

  /**
   * The merge honest failure. A merge the runner could NOT carry out no longer
   * resolves quietly — it fails the row and parks the runner's own reason on
   * it, which FailedBanner renders VERBATIM.
   *
   * The row carries `error_retryable: false` — the shape the runner now
   * produces for this failure. That reason forbids deciding again (it would
   * import a THIRD copy), so the emphasized line must not instruct the retry
   * the muted line under it rules out, and the banner must point at the
   * recovery that works. The duplicate actions stay ENABLED regardless: the
   * strip is the escape hatch for the ordinary already-in-library failure, and
   * a not-retryable headline is a copy change, not a lockout.
   *
   * FIXTURE CONTRACT — the literal below is `_MERGE_NOT_MERGED_ERROR` from
   * `backend/app/bank/apply_runner.py`, duplicated VERBATIM on purpose: that
   * constant is authoritative, and this copy is the drift alarm (exactly as
   * the legacy-label block above explains — an import would follow production
   * and never fail). If the backend rewords it, update this string to match.
   */
  test("a merge that could not run shows the reason, drops the retry instruction, and links Duplicates", async () => {
    const MERGE_FAILED_ERROR =
      "the album landed in your library as a second copy and the merge never ran (your " +
      "library copy no longer matched it) - deciding again would import it a third time; " +
      "remove one of the two copies instead, from its album page or the Duplicates page";
    server.use(
      http.get(ITEM, () =>
        HttpResponse.json(
          bankItem({
            reason: "needs_dup_resolution",
            parked: null,
            duplicate: duplicatePrompt,
            recommendation: null,
            confidence: null,
            status: "failed",
            error: MERGE_FAILED_ERROR,
            error_retryable: false,
            decided: { action: "duplicate", candidate_index: null, duplicate_action: "merge" },
          }),
        ),
      ),
    );
    renderRow();
    // Role-scoped on purpose: the fixture error and the comparison panel's own
    // heading share the words "already in your library", so a text query for
    // either would match both.
    await screen.findByRole("heading", { name: /already in your library/i });
    const banner = screen.getByRole("alert");
    // Exact strings, not regexes: the banner's wrapper carries BOTH lines in
    // its textContent, so a substring match would find two elements.
    expect(within(banner).getByText("The apply failed.")).toBeInTheDocument();
    expect(within(banner).getByText(MERGE_FAILED_ERROR)).toBeInTheDocument();
    // The instruction the parked reason contradicts. Asserted on the banner's
    // whole textContent, not as a queryByText miss, so re-adding the clause
    // anywhere inside the banner — split across elements included — fails.
    expect(banner).not.toHaveTextContent("Decide again to retry.");
    // The recovery that actually works for this failure.
    expect(within(banner).getByRole("link", { name: "Open Duplicates" })).toHaveAttribute(
      "href",
      "/duplicates",
    );
    // The strip below is unaffected — the row is still decidable as a duplicate.
    expect(screen.getByRole("button", { name: /merge/i })).toBeEnabled();
  });

  /**
   * The DEFAULT arm. Ordinary failures stay retryable, and a row banked before
   * `error_retryable` existed carries no such key at all — the fixture factory
   * omits it, so the first render IS that old shape. Both must keep the retry
   * instruction and offer no Duplicates link.
   *
   * Rendered twice because the two inputs reach the arm by different routes:
   * absent leans on the `!== false` predicate, `true` on the value itself, so
   * narrowing the check to `=== true` survives the second and dies on the first.
   */
  test("a retryable failure keeps the retry headline, field true or absent", async () => {
    server.use(
      http.get(ITEM, () => HttpResponse.json(bankItem({ status: "failed", error: "beets exited 1" }))),
    );
    const absent = renderRow();
    expect(
      await within(absent.container).findByText("The apply failed. Decide again to retry."),
    ).toBeInTheDocument();
    expect(
      within(absent.container).queryByRole("link", { name: "Open Duplicates" }),
    ).not.toBeInTheDocument();

    server.use(
      http.get(ITEM, () =>
        HttpResponse.json(bankItem({ status: "failed", error: "beets exited 1", error_retryable: true })),
      ),
    );
    const explicit = renderRow();
    expect(
      await within(explicit.container).findByText("The apply failed. Decide again to retry."),
    ).toBeInTheDocument();
    expect(
      within(explicit.container).queryByRole("link", { name: "Open Duplicates" }),
    ).not.toBeInTheDocument();
  });

  test("a vanished row shows the gone notice", async () => {
    server.use(http.get(ITEM, () => HttpResponse.json({ detail: "Bank item not found" }, { status: 404 })));
    renderRow();
    expect(await screen.findByText(/no longer in the bank/i)).toBeInTheDocument();
  });

  test("a transient load error shows a retry, not the terminal gone notice", async () => {
    // A 5xx (backend restart while the apply runner hammers the disk) must not
    // falsely claim the row was applied/removed — offer a retry instead.
    server.use(http.get(ITEM, () => new HttpResponse(null, { status: 500 })));
    renderRow();
    expect(
      await screen.findByRole("button", { name: /retry/i }),
    ).toBeInTheDocument();
    expect(screen.queryByText(/no longer in the bank/i)).not.toBeInTheDocument();
  });

  test("rescues a stale row in place: rescan swaps in a candidate screen", async () => {
    const staleRow = bankItem({ status: "stale", error: "the folder changed" });
    // The rescan lands a real needs_review candidate row on the SAME id.
    const rescued = bankItem({ status: "needs_review" });
    server.use(
      http.get(ITEM, () => HttpResponse.json(staleRow)),
      http.post(RESCAN, () => HttpResponse.json(rescued)),
    );
    renderRow();
    await screen.findByText(/changed after it was banked/i);
    fireEvent.click(screen.getByRole("button", { name: /rescan folder/i }));
    // The cache write re-branches the SAME route to the candidate screen.
    expect((await screen.findAllByText(/after import/i)).length).toBeGreaterThan(0);
  });

  test("offers rescan on the duplicate-prompt screen", async () => {
    const duplicateRow = bankItem({
      reason: "needs_dup_resolution",
      parked: null,
      duplicate: duplicatePrompt,
      recommendation: null,
      confidence: null,
    });
    server.use(http.get(ITEM, () => HttpResponse.json(duplicateRow)));
    renderRow();
    await screen.findByRole("heading", { name: /already in your library/i });
    expect(screen.getByRole("button", { name: /rescan folder/i })).toBeEnabled();
  });

  test("surfaces a rescan 409 with the backend detail", async () => {
    server.use(
      http.get(ITEM, () => HttpResponse.json(bankItem())),
      http.post(RESCAN, () =>
        HttpResponse.json({ detail: "no audio files remain in the folder" }, { status: 409 }),
      ),
    );
    renderRow();
    await screen.findAllByText(/after import/i);
    fireEvent.click(screen.getByRole("button", { name: /rescan folder/i }));
    expect(await screen.findByText(/no audio files remain/i)).toBeInTheDocument();
  });

  test("filters legacy 'None' segments out of candidate switcher labels", async () => {
    // Rows banked before the adapter-side fix carry beets' raw
    // "Deezer, None, 2024, …" disambig string forever — display-side guard.
    const legacy = {
      ...candidate,
      options: [
        candidate.options[0],
        {
          ...candidate.options[1],
          disambiguation: "Deezer, None, 2024, None, Hit Wave Music, None",
        },
      ],
    };
    server.use(
      http.get(ITEM, () =>
        HttpResponse.json(
          bankItem({ parked: { album_index: 0, folder: "/inbox/BoC", candidate: legacy } }),
        ),
      ),
    );
    renderRow();
    await screen.findAllByText(/after import/i);
    expect(
      screen.getByRole("option", { name: "64% · MusicBrainz · Deezer, 2024, Hit Wave Music" }),
    ).toBeInTheDocument();
  });

  test("rescan rides the control bar next to Apply, with the tooltip", async () => {
    server.use(http.get(ITEM, () => HttpResponse.json(bankItem())));
    renderRow();
    await screen.findAllByText(/after import/i);
    const rescan = screen.getByRole("button", { name: /rescan folder/i });
    expect(rescan).toHaveAttribute(
      "title",
      "Re-reads the folder from disk and matches it again.",
    );
    expect(screen.getByRole("button", { name: /apply/i })).toBeInTheDocument();
  });
});
