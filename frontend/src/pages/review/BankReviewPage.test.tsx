import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
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
    { index: 0, confidence: 71.2, data_source: "MusicBrainz", disambiguation: null, release_id: "mb-1" },
    { index: 1, confidence: 64.0, data_source: "MusicBrainz", disambiguation: "remaster", release_id: "mb-2" },
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
    // The up-front collision notice + a View link to the existing copy.
    expect(await screen.findByText(/already in your library/i)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /^view$/i })).toHaveAttribute("href", "/albums/7");
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

  test("a stale row offers Review now: starts an attended import of the folder, deletes the row, navigates", async () => {
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
    expect(importBody).toEqual({ path: "/inbox/BoC" });
    expect(deleted).toBe(true);
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

  test("a vanished row shows the gone notice", async () => {
    server.use(http.get(ITEM, () => HttpResponse.json({ detail: "Bank item not found" }, { status: 404 })));
    renderRow();
    expect(await screen.findByText(/no longer in the bank/i)).toBeInTheDocument();
  });
});
