import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";
import { beforeEach, describe, expect, test, vi } from "vitest";

import type { ImportJobState } from "@/api/useImport";
import { ImportPage } from "@/pages/import/ImportPage";
import { ELAPSED_AFTER_S } from "@/pages/import/importStatus";
import { renderWithProviders } from "@/test/render";
import { server } from "@/test/msw-server";

const IMPORT_URL = `${window.location.origin}/api/import`;
const JOB_URL = `${window.location.origin}/api/import/job-1`;
const ACTIVE_URL = `${window.location.origin}/api/imports/active`;
// The sweep tests start/poll a distinct job id so the two run views can't
// shadow each other's handlers.
const SWEEP_JOB_URL = `${window.location.origin}/api/import/s1`;
const SWEEP_PAUSE_URL = `${window.location.origin}/api/import/s1/pause`;

function makeJob(overrides: Partial<ImportJobState> = {}): ImportJobState {
  return {
    job_id: "job-1",
    phase: "reviewing",
    progress: { applied: 1, needs_review: 1, skipped: 0, not_landed: 0 },
    albums: [
      {
        index: 0,
        folder: "/music/incoming/Radiohead - OK Computer",
        artist: "Radiohead",
        album: "OK Computer",
        recommendation: "strong",
        confidence: 99,
        status: "applied",
        album_id: 41,
        did_not_land: false,
      },
      {
        index: 1,
        folder: "/music/incoming/Unknown Album",
        artist: "Radiohead",
        album: "Kid A",
        recommendation: "medium",
        confidence: 76,
        status: "needs_review",
        album_id: null,
        did_not_land: false,
      },
    ],
    summary: null,
    error: null,
    origin: "manual",
    set_aside: 0,
    elapsed_seconds: 0,
    // Default: the worker is NOT blocked — the row above is set aside, which
    // is not the same thing (an unattended duplicate and a `search` re-lookup
    // both wear one while beets works). A test that means "parked on a person"
    // says `awaiting_decision: true`.
    awaiting_decision: false,
    ...overrides,
  };
}

/** A sweep-origin job: no per-album feed by design — the sweep block carries
 * the whole progress story. Mirrors makeJob for the banking run view. */
function sweepJob(overrides: Partial<ImportJobState> = {}): ImportJobState {
  return {
    job_id: "s1",
    phase: "scanning",
    progress: { applied: 0, needs_review: 0, skipped: 0, not_landed: 0 },
    albums: [],
    summary: null,
    error: null,
    origin: "sweep",
    set_aside: 0,
    elapsed_seconds: 0,
    // A sweep is unattended by definition — it never blocks on a person.
    awaiting_decision: false,
    sweep: {
      processed: 0,
      auto_applied: 0,
      banked: 0,
      skipped_known: 0,
      current_folder: null,
      paused: false,
    },
    ...overrides,
  };
}

/** Render at a given URL so `useSearchParams` (the `?job=` seam) resolves. */
function renderAt(route: string) {
  return renderWithProviders(<ImportPage />, { route, path: "/import" });
}

/** The spinner belonging to a status line. It is ALWAYS mounted — its box is
 * reserved so the line can't jump sideways on every park — so tests read its
 * classes (`animate-spin` vs `invisible`), never its presence. */
function spinnerOf(line: HTMLElement): Element {
  const spinner = line.closest("p")?.querySelector("svg");
  if (!spinner) throw new Error("the status line has no spinner box");
  return spinner;
}

/** The `<p>` a status-line fragment sits in. The elapsed value is TWO nodes —
 * an aria-hidden `· 2m 12s` and its `sr-only` spoken twin, because a screen
 * reader reads "12m" as a letter — so no single element holds the whole line's
 * text any more. */
function lineOf(fragment: HTMLElement): HTMLElement {
  const line = fragment.closest("p");
  if (!line) throw new Error("the fragment is not inside a status line");
  return line;
}

/** Renders the AlbumOrigin router state an outgoing feed link arrives with. */
function OriginProbe() {
  const state = useLocation().state as
    | { from?: { label: string; to: string } }
    | null;
  return (
    <p>
      origin: {state?.from ? `${state.from.label} ${state.from.to}` : "none"}
    </p>
  );
}

/** ImportPage plus probe routes for every link that leaves the feed. */
function renderFeedWithProbes(route: string) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[route]}>
        <Routes>
          <Route path="/import" element={<ImportPage />} />
          <Route path="/import/albums/:index" element={<OriginProbe />} />
          <Route path="/import/albums/:index/duplicate" element={<OriginProbe />} />
          <Route path="/albums/:albumId" element={<OriginProbe />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("ImportPage — entry", () => {
  beforeEach(() => {
    // ImportEntry polls the active-import probe (for the Resume banner).
    // Default to "idle" so the pre-existing entry tests see no banner and an
    // enabled Start; banner tests override with their own server.use(...).
    server.use(
      http.get(ACTIVE_URL, () =>
        HttpResponse.json({ active: false, job_id: null }),
      ),
    );
  });

  test("shows the path input + Start when there is no active job", () => {
    renderAt("/import");
    expect(
      screen.getByRole("heading", { level: 1, name: "Add from folder" }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Folder path")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /start import/i }),
    ).toBeInTheDocument();
  });

  test("Start is disabled until a path is typed (blank-path guard)", async () => {
    const user = userEvent.setup();
    renderAt("/import");

    const button = screen.getByRole("button", { name: /start import/i });
    expect(button).toBeDisabled();

    await user.type(screen.getByLabelText("Folder path"), "/music/incoming");
    expect(button).toBeEnabled();
  });

  test("starting posts the path and flips to the feed via ?job=", async () => {
    let seenBody: unknown = null;
    server.use(
      http.post(IMPORT_URL, async ({ request }) => {
        seenBody = await request.json();
        return HttpResponse.json({ job_id: "job-1" }, { status: 202 });
      }),
      http.get(JOB_URL, () =>
        HttpResponse.json(makeJob({ phase: "scanning", albums: [] })),
      ),
    );
    const user = userEvent.setup();
    renderAt("/import");

    await user.type(screen.getByLabelText("Folder path"), "/music/incoming");
    await user.click(screen.getByRole("button", { name: /start import/i }));

    // The job query now drives the page (the live feed's scanning cue shows).
    expect(
      await screen.findByText(/scanning your folder/i),
    ).toBeInTheDocument();
    expect(seenBody).toEqual({ path: "/music/incoming" });
  });

  test("a 409 with no resumable import surfaces an accurate, non-dead-end message", async () => {
    // The swap-lock case: no import owns the slot (probe idle -> Start enabled),
    // but a config Apply / duplicate resolve holds the beets lock, so POST
    // /api/import 409s. The message must not claim a resumable import.
    server.use(
      http.post(IMPORT_URL, () =>
        HttpResponse.json(
          { detail: "A library operation is in progress" },
          { status: 409 },
        ),
      ),
    );
    const user = userEvent.setup();
    renderAt("/import");

    await user.type(screen.getByLabelText("Folder path"), "/music/incoming");
    await user.click(screen.getByRole("button", { name: /start import/i }));

    expect(
      await screen.findByText(/library operation is in progress/i),
    ).toBeInTheDocument();
    // Still on the entry screen (no ?job=, so the input is still shown).
    expect(screen.getByLabelText("Folder path")).toBeInTheDocument();
  });

  test("shows a Resume banner to the running job and disables Start while active", async () => {
    server.use(
      http.get(ACTIVE_URL, () =>
        HttpResponse.json({ active: true, job_id: "job-9" }),
      ),
    );
    renderAt("/import");

    const resume = await screen.findByRole("link", { name: /resume/i });
    expect(resume).toHaveAttribute("href", "/import?job=job-9");
    // Start is gated while an import is already running.
    expect(
      screen.getByRole("button", { name: /start import/i }),
    ).toBeDisabled();
  });

  test("shows the inbox-origin + set-aside cue for an unattended import", async () => {
    server.use(
      http.get(ACTIVE_URL, () =>
        HttpResponse.json({
          active: true,
          job_id: "job-7",
          origin: "inbox",
          needs_review_count: 3,
        }),
      ),
    );
    renderAt("/import");

    expect(
      await screen.findByText(/an inbox import is running/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/3 albums set aside for review/i),
    ).toBeInTheDocument();
  });

  test("a 409 refreshes the probe so the Resume banner appears (race recovery)", async () => {
    // The race: the user lands while the probe still reads idle (Start enabled),
    // types a path and clicks Start, but an import started elsewhere between
    // probes -> POST 409. onError invalidates ["active-import"]; the refetch now
    // reports the running job, so the Resume banner materializes instead of a
    // dead-end. The probe returns "active" only AFTER the POST has fired.
    let started = false;
    server.use(
      http.post(IMPORT_URL, () => {
        started = true;
        return HttpResponse.json(
          { detail: "An import is already running" },
          { status: 409 },
        );
      }),
      http.get(ACTIVE_URL, () =>
        started
          ? HttpResponse.json({ active: true, job_id: "job-7" })
          : HttpResponse.json({ active: false, job_id: null }),
      ),
    );
    const user = userEvent.setup();
    renderAt("/import");

    await user.type(screen.getByLabelText("Folder path"), "/music/incoming");
    await user.click(screen.getByRole("button", { name: /start import/i }));

    // The 409-triggered probe invalidation surfaces the running job as a Resume.
    const resume = await screen.findByRole("link", { name: /resume/i });
    expect(resume).toHaveAttribute("href", "/import?job=job-7");
  });
});

describe("ImportPage — live feed", () => {
  test("renders applied + the current needs_review row from the job", async () => {
    server.use(http.get(JOB_URL, () => HttpResponse.json(makeJob())));
    renderAt("/import?job=job-1");

    // Both albums show; the strong one is calm-applied, the medium one needs
    // review.
    expect(await screen.findByText("OK Computer")).toBeInTheDocument();
    expect(screen.getByText("Kid A")).toBeInTheDocument();
    expect(screen.getByText("Imported")).toBeInTheDocument();
    expect(screen.getByText("Needs review")).toBeInTheDocument();
  });

  test("feed row covers request the thumb, not the full-size original", async () => {
    server.use(http.get(JOB_URL, () => HttpResponse.json(makeJob())));
    renderAt("/import?job=job-1");

    await screen.findByText("OK Computer");
    // AlbumRow renders CoverArt at size-10 (40 CSS px) and the feed shows
    // dozens of rows at once — the densest cover consumer in the app. A
    // full-size original here is multi-hundred KB per row for nothing.
    const cover = document.querySelector('img[data-slot="cover-art"]');
    expect(cover?.getAttribute("src")).toBe("/api/albums/41/cover?size=thumb");
  });

  test("pins the needs-review album to the top of the feed", async () => {
    server.use(http.get(JOB_URL, () => HttpResponse.json(makeJob())));
    renderAt("/import?job=job-1");

    await screen.findByText("OK Computer");
    // makeJob: index 0 = applied "OK Computer", index 1 = needs_review "Kid A".
    // The one awaiting review is pinned first, above the already-applied row.
    const rows = screen.getAllByRole("listitem");
    expect(rows[0]).toHaveTextContent("Kid A");
    expect(rows[1]).toHaveTextContent("OK Computer");
  });

  test("settled feed rows list newest-first under the pinned decision", async () => {
    server.use(
      http.get(JOB_URL, () =>
        HttpResponse.json(
          makeJob({
            progress: { applied: 2, needs_review: 1, skipped: 0, not_landed: 0 },
            albums: [
              {
                index: 0,
                folder: "/music/incoming/Radiohead - OK Computer",
                artist: "Radiohead",
                album: "OK Computer",
                recommendation: "strong",
                confidence: 99,
                status: "applied",
                album_id: 41,
                did_not_land: false,
              },
              {
                index: 1,
                folder: "/music/incoming/Radiohead - Kid A",
                artist: "Radiohead",
                album: "Kid A",
                recommendation: "strong",
                confidence: 98,
                status: "applied",
                album_id: 42,
                did_not_land: false,
              },
              {
                index: 2,
                folder: "/music/incoming/Radiohead - Amnesiac",
                artist: "Radiohead",
                album: "Amnesiac",
                recommendation: "medium",
                confidence: 74,
                status: "needs_review",
                album_id: null,
                did_not_land: false,
              },
            ],
          }),
        ),
      ),
    );
    renderAt("/import?job=job-1");

    await screen.findByText("OK Computer");
    // Pinned decision first, then settled rows newest-first — the latest
    // landed album sits right under the thing to act on, not at the bottom.
    const rows = screen.getAllByRole("listitem");
    expect(rows[0]).toHaveTextContent("Amnesiac");
    expect(rows[1]).toHaveTextContent("Kid A");
    expect(rows[2]).toHaveTextContent("OK Computer");
  });

  test("the finished feed lists newest-first too", async () => {
    server.use(
      http.get(JOB_URL, () =>
        HttpResponse.json(
          makeJob({
            phase: "done",
            progress: { applied: 2, needs_review: 0, skipped: 0, not_landed: 0 },
            albums: [
              {
                index: 0,
                folder: "/music/incoming/Radiohead - OK Computer",
                artist: "Radiohead",
                album: "OK Computer",
                recommendation: "strong",
                confidence: 99,
                status: "applied",
                album_id: 41,
                did_not_land: false,
              },
              {
                index: 1,
                folder: "/music/incoming/Radiohead - Kid A",
                artist: "Radiohead",
                album: "Kid A",
                recommendation: "strong",
                confidence: 98,
                status: "applied",
                album_id: 42,
                did_not_land: false,
              },
            ],
          }),
        ),
      ),
    );
    renderAt("/import?job=job-1");

    await screen.findByText("Import finished");
    const rows = screen.getAllByRole("listitem");
    expect(rows[0]).toHaveTextContent("Kid A");
    expect(rows[1]).toHaveTextContent("OK Computer");
  });

  test("the pinned decision outranks newest-first even at a lower index", async () => {
    // Guards the pin sort key: the pending row is at the LOWEST index here, so
    // newest-first alone would sink it to the bottom. Only the pin term keeps it
    // on top — deleting that term from FeedList must fail this test. The fixture
    // can't occur in today's sequential review (the album awaiting a decision is
    // always the latest), so the pin key is defensive; this test keeps it honest.
    server.use(
      http.get(JOB_URL, () =>
        HttpResponse.json(
          makeJob({
            progress: { applied: 2, needs_review: 1, skipped: 0, not_landed: 0 },
            albums: [
              {
                index: 0,
                folder: "/music/incoming/Radiohead - Amnesiac",
                artist: "Radiohead",
                album: "Amnesiac",
                recommendation: "medium",
                confidence: 74,
                status: "needs_review",
                album_id: null,
                did_not_land: false,
              },
              {
                index: 1,
                folder: "/music/incoming/Radiohead - OK Computer",
                artist: "Radiohead",
                album: "OK Computer",
                recommendation: "strong",
                confidence: 99,
                status: "applied",
                album_id: 41,
                did_not_land: false,
              },
              {
                index: 2,
                folder: "/music/incoming/Radiohead - Kid A",
                artist: "Radiohead",
                album: "Kid A",
                recommendation: "strong",
                confidence: 98,
                status: "applied",
                album_id: 42,
                did_not_land: false,
              },
            ],
          }),
        ),
      ),
    );
    renderAt("/import?job=job-1");

    await screen.findByText("OK Computer");
    const rows = screen.getAllByRole("listitem");
    expect(rows[0]).toHaveTextContent("Amnesiac");
    expect(rows[1]).toHaveTextContent("Kid A");
    expect(rows[2]).toHaveTextContent("OK Computer");
  });

  test("the Review affordance links to the right album index", async () => {
    server.use(http.get(JOB_URL, () => HttpResponse.json(makeJob())));
    renderAt("/import?job=job-1");

    const review = await screen.findByRole("link", { name: /review/i });
    // index 1 is the needs_review album; the link carries the job id across the
    // chunk-4 seam.
    expect(review).toHaveAttribute("href", "/import/albums/1?job=job-1");
  });

  test("a needs_dup_resolution row shows an Already-in-library badge + a Resolve link", async () => {
    server.use(
      http.get(JOB_URL, () =>
        HttpResponse.json(
          makeJob({
            // index 0 becomes a duplicate awaiting resolution. Keep `progress`
            // internally consistent with the single duplicate row (the default
            // makeJob progress disagrees: it claims an applied + a needs_review).
            progress: { applied: 0, needs_review: 0, skipped: 0, not_landed: 0 },
            albums: [
              {
                index: 0,
                folder: "/music/incoming/Radiohead - OK Computer",
                artist: "Radiohead",
                album: "OK Computer",
                recommendation: "strong",
                confidence: 99,
                status: "needs_dup_resolution",
                album_id: null,
                did_not_land: false,
              },
            ],
          }),
        ),
      ),
    );
    renderAt("/import?job=job-1");

    // The badge label for the new status.
    expect(await screen.findByText("Already in library")).toBeInTheDocument();
    // The Resolve affordance routes to the dup page, carrying the job id across
    // the same `?job=` seam the Review link uses.
    const resolve = screen.getByRole("link", { name: /resolve/i });
    expect(resolve).toHaveAttribute(
      "href",
      "/import/albums/0/duplicate?job=job-1",
    );
  });

  test("the live cue surfaces a parked duplicate to resolve", async () => {
    server.use(
      http.get(JOB_URL, () =>
        HttpResponse.json(
          makeJob({
            progress: { applied: 0, needs_review: 0, skipped: 0, not_landed: 0 },
            albums: [
              {
                index: 0,
                folder: "/music/incoming/Radiohead - OK Computer",
                artist: "Radiohead",
                album: "OK Computer",
                recommendation: "strong",
                confidence: 99,
                status: "needs_dup_resolution",
                album_id: null,
                did_not_land: false,
              },
            ],
          }),
        ),
      ),
    );
    renderAt("/import?job=job-1");

    // The visible cue line flags the parked duplicate (derived client-side from
    // the feed rows — `progress` has no duplicate counter).
    expect(
      await screen.findByText(/1 already in library/),
    ).toBeInTheDocument();
  });

  test("humanizes the recommendation enum in the feed sub-line", async () => {
    server.use(http.get(JOB_URL, () => HttpResponse.json(makeJob())));
    renderAt("/import?job=job-1");

    // The needs_review row (confidence 76, recommendation "medium") shows the
    // humanized label, not the raw enum token.
    expect(await screen.findByText(/76% · Medium match/)).toBeInTheDocument();
    // The raw "medium" token must not leak into the sub-line.
    expect(screen.queryByText(/76% · medium/)).not.toBeInTheDocument();
  });

  test("shows a scanning cue while the feed is still empty", async () => {
    server.use(
      http.get(JOB_URL, () =>
        HttpResponse.json(
          makeJob({
            phase: "scanning",
            progress: { applied: 0, needs_review: 0, skipped: 0, not_landed: 0 },
            albums: [],
          }),
        ),
      ),
    );
    renderAt("/import?job=job-1");

    expect(await screen.findByText(/scanning your folder/i)).toBeInTheDocument();
  });

  test("a short scan's cue carries no elapsed value at all", async () => {
    server.use(
      http.get(JOB_URL, () =>
        HttpResponse.json(
          makeJob({
            phase: "scanning",
            progress: { applied: 0, needs_review: 0, skipped: 0, not_landed: 0 },
            albums: [],
            elapsed_seconds: ELAPSED_AFTER_S - 1,
          }),
        ),
      ),
    );
    renderAt("/import?job=job-1");

    // Exactly today's line — a fast import must gain no extra text.
    expect(
      await screen.findByText("Scanning your folder…"),
    ).toBeInTheDocument();
  });

  test("a long scan's cue carries the elapsed value", async () => {
    server.use(
      http.get(JOB_URL, () =>
        HttpResponse.json(
          makeJob({
            phase: "scanning",
            progress: { applied: 0, needs_review: 0, skipped: 0, not_landed: 0 },
            albums: [],
            elapsed_seconds: 132,
          }),
        ),
      ),
    );
    renderAt("/import?job=job-1");

    // The whole point: a ten-minute MusicBrainz lookup must not look wedged.
    // Two units, so the line visibly moves every second rather than once a
    // minute — a frozen line is the very thing being ruled out.
    const segment = await screen.findByText("· 2m 12s");
    expect(lineOf(segment)).toHaveTextContent("Scanning your folder…");
    // The middot's trailing space is non-breaking, so a wrap can never strand
    // it at the end of a line. (RTL normalizes it away, hence the raw read.)
    expect(segment.textContent).toContain("·\u00a0");
    expect(segment.textContent).toContain("2m\u00a012s");
    // A screen reader reads "2m" as a letter, so the visible half is hidden and
    // a spoken twin carries the words.
    expect(segment).toHaveAttribute("aria-hidden", "true");
    expect(screen.getByText("2 minutes.")).toHaveClass("sr-only");
  });

  test("the cue keeps working after a decision, while the worker scans on", async () => {
    // `phase` latches to "reviewing" at the first parked album and never
    // returns to "scanning", so this run — album 1 decided, nothing parked,
    // the worker looking up album 2 — used to sit with no spinner and a frozen
    // count for as long as the lookup took.
    server.use(
      http.get(JOB_URL, () =>
        HttpResponse.json(
          makeJob({
            phase: "reviewing",
            progress: { applied: 1, needs_review: 0, skipped: 0, not_landed: 0 },
            albums: [
              {
                index: 0,
                folder: "/music/incoming/Radiohead - OK Computer",
                artist: "Radiohead",
                album: "OK Computer",
                recommendation: "strong",
                confidence: 99,
                status: "applied",
                album_id: 41,
                did_not_land: false,
              },
            ],
            elapsed_seconds: 300,
          }),
        ),
      ),
    );
    renderAt("/import?job=job-1");

    const segment = await screen.findByText("· 5m");
    const line = lineOf(segment);
    expect(line).toHaveTextContent("1 album imported");
    expect(spinnerOf(line)).toHaveClass("animate-spin");
  });

  test("a run parked on the operator keeps its elapsed value", async () => {
    // The number counts the WHOLE run, from the start — it is not a "time since
    // last progress" gauge, so hiding it here would make it vanish and come
    // back later carrying the operator's own thinking time (the owner's ruling).
    server.use(
      http.get(JOB_URL, () =>
        HttpResponse.json(
          makeJob({ elapsed_seconds: 600, awaiting_decision: true }),
        ),
      ),
    );
    renderAt("/import?job=job-1");

    const segment = await screen.findByText("· 10m");
    const line = lineOf(segment);
    expect(line).toHaveTextContent("1 album imported · 1 album needs review");
    // Words for the screen reader, since "10m" is read as a letter.
    expect(screen.getByText("10 minutes.")).toHaveClass("sr-only");
    // Nobody is working, so no spinner — but its box stays, or the whole line
    // would jump sideways on every park and unpark.
    expect(spinnerOf(line)).toHaveClass("invisible");
    expect(spinnerOf(line)).not.toHaveClass("animate-spin");
  });

  test("an empty feed never renders a count, blocked or not", async () => {
    // `park()` buffers a park whose row doesn't exist yet, so the live state is
    // phase=scanning, albums=[], awaiting_decision=true — nobody is "working",
    // and gating this branch on that rendered "0 albums imported".
    server.use(
      http.get(JOB_URL, () =>
        HttpResponse.json(
          makeJob({
            phase: "scanning",
            progress: { applied: 0, needs_review: 0, skipped: 0, not_landed: 0 },
            albums: [],
            awaiting_decision: true,
          }),
        ),
      ),
    );
    renderAt("/import?job=job-1");

    expect(await screen.findByText("Scanning your folder…")).toBeInTheDocument();
    expect(screen.queryByText(/albums imported/)).not.toBeInTheDocument();
  });

  // B1/B2 on the visible side of the cadence tests: the two set-aside rows that
  // do NOT block the worker. This is the owner's own slskd inbox path, and it
  // used to sit spinner-less and elapsed-less for the rest of the run.
  test.each(["needs_review", "needs_dup_resolution"] as const)(
    "an unattended run with a set-aside row (%s) still looks alive",
    async (status) => {
      server.use(
        http.get(JOB_URL, () =>
          HttpResponse.json(
            makeJob({
              phase: "reviewing",
              origin: "inbox",
              progress: { applied: 1, needs_review: 0, skipped: 1, not_landed: 0 },
              albums: [
                {
                  index: 0,
                  folder: "/music/incoming/Kid A",
                  artist: "Radiohead",
                  album: "Kid A",
                  recommendation: "medium",
                  confidence: 76,
                  status,
                  album_id: null,
                  did_not_land: false,
                },
              ],
              elapsed_seconds: 132,
              awaiting_decision: false,
            }),
          ),
        ),
      );
      renderAt("/import?job=job-1");

      const line = await screen.findByText(/1 album imported/);
      expect(spinnerOf(line)).toHaveClass("animate-spin");
      expect(line.textContent).toContain("2m\u00a012s");
    },
  );

  test("the live cue surfaces a skipped count when any album was skipped", async () => {
    server.use(
      http.get(JOB_URL, () =>
        HttpResponse.json(
          makeJob({
            phase: "reviewing",
            progress: { applied: 1, needs_review: 1, skipped: 1, not_landed: 0 },
          }),
        ),
      ),
    );
    renderAt("/import?job=job-1");

    // The visible cue counts imported + skipped + the one awaiting review.
    expect(await screen.findByText(/1 skipped/)).toBeInTheDocument();
  });

  test("announces the run through one polite live region", async () => {
    server.use(http.get(JOB_URL, () => HttpResponse.json(makeJob())));
    renderAt("/import?job=job-1");

    // Exactly one spoken region; the visible cue is no longer a live region.
    const status = await screen.findByRole("status");
    expect(status).toHaveAttribute("aria-live", "polite");
    expect(screen.getAllByRole("status")).toHaveLength(1);
  });

  test("the feed Review link threads the Import origin to the decision screen", async () => {
    server.use(http.get(JOB_URL, () => HttpResponse.json(makeJob())));
    renderFeedWithProbes("/import?job=job-1");

    await userEvent.click(
      await screen.findByRole("link", { name: /^review$/i }),
    );
    expect(
      await screen.findByText("origin: Import /import?job=job-1"),
    ).toBeInTheDocument();
  });

  test("an applied row's album link carries the Import origin", async () => {
    server.use(http.get(JOB_URL, () => HttpResponse.json(makeJob())));
    renderFeedWithProbes("/import?job=job-1");

    await userEvent.click(
      await screen.findByRole("link", { name: "OK Computer" }),
    );
    expect(
      await screen.findByText("origin: Import /import?job=job-1"),
    ).toBeInTheDocument();
  });

  test("a user-DECIDED row with an album_id links too — non-null id is the link condition", async () => {
    // A Review-screen Apply sets status "decided"; the album_id follow-up
    // does not touch status. The row must still link to the landed album.
    const job = makeJob();
    job.albums = [
      {
        ...job.albums[0]!,
        status: "decided",
        album: "In Rainbows",
        album_id: 77,
      },
    ];
    server.use(http.get(JOB_URL, () => HttpResponse.json(job)));
    renderFeedWithProbes("/import?job=job-1");

    const link = await screen.findByRole("link", { name: "In Rainbows" });
    expect(link).toHaveAttribute("href", "/albums/77");
  });
});

describe("ImportPage — terminal states", () => {
  // The number counts the whole run and is shown throughout, the finish line
  // included (the owner's ruling) — a ten-minute import that ends by dropping
  // its own duration answers nothing.
  test("the finished summary keeps the run's elapsed value", async () => {
    server.use(
      http.get(JOB_URL, () =>
        HttpResponse.json(
          makeJob({
            phase: "done",
            progress: { applied: 2, needs_review: 0, skipped: 1, not_landed: 0 },
            albums: [],
            elapsed_seconds: 840,
          }),
        ),
      ),
    );
    renderAt("/import?job=job-1");

    const segment = await screen.findByText("· 14m");
    expect(segment.closest("p")).toHaveTextContent(
      "2 albums imported · 1 skipped",
    );
    expect(screen.getByText("14 minutes.")).toHaveClass("sr-only");
  });

  test("a short run's finished summary gains no extra text", async () => {
    server.use(
      http.get(JOB_URL, () =>
        HttpResponse.json(
          makeJob({
            phase: "done",
            progress: { applied: 2, needs_review: 0, skipped: 1, not_landed: 0 },
            albums: [],
            elapsed_seconds: ELAPSED_AFTER_S - 1,
          }),
        ),
      ),
    );
    renderAt("/import?job=job-1");

    expect(
      await screen.findByText("2 albums imported · 1 skipped"),
    ).toBeInTheDocument();
  });

  test("done links each applied album to its library page — no blanket view-in-library", async () => {
    server.use(
      http.get(JOB_URL, () =>
        HttpResponse.json(
          makeJob({
            phase: "done",
            summary: "1 imported, 1 skipped",
            progress: { applied: 1, needs_review: 0, skipped: 1, not_landed: 0 },
            albums: [
              {
                index: 0,
                folder: "/music/incoming/Radiohead - OK Computer",
                artist: "Radiohead",
                album: "OK Computer",
                recommendation: "strong",
                confidence: 99,
                status: "applied",
                album_id: 41,
                did_not_land: false,
              },
              {
                index: 1,
                folder: "/music/incoming/Unknown Album",
                artist: "Radiohead",
                album: "Kid A",
                recommendation: "none",
                confidence: 0,
                status: "skipped",
                album_id: null,
                did_not_land: false,
              },
            ],
          }),
        ),
      ),
    );
    renderAt("/import?job=job-1");

    expect(await screen.findByText("Import finished")).toBeInTheDocument();
    expect(screen.getByText(/1 album imported · 1 skipped/)).toBeInTheDocument();
    // The applied row links to its library page; the skipped row does not.
    const albumLink = screen.getByRole("link", { name: "OK Computer" });
    expect(albumLink).toHaveAttribute("href", "/albums/41");
    expect(screen.queryByRole("link", { name: "Kid A" })).not.toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: /view in library/i }),
    ).not.toBeInTheDocument();
  });

  test("a row that never landed shows a Didn't-land badge and the done body counts it", async () => {
    // A decided/applied row whose library album id never arrived on a terminal
    // job carries did_not_land; progress.not_landed mirrors the count. The row
    // must flag the failure and the summary must own up to it.
    server.use(
      http.get(JOB_URL, () =>
        HttpResponse.json(
          makeJob({
            phase: "done",
            progress: { applied: 0, needs_review: 0, skipped: 0, not_landed: 1 },
            albums: [
              {
                index: 0,
                folder: "/music/incoming/Radiohead - OK Computer",
                artist: "Radiohead",
                album: "OK Computer",
                recommendation: "strong",
                confidence: 99,
                status: "decided",
                album_id: null,
                did_not_land: true,
              },
            ],
          }),
        ),
      ),
    );
    renderAt("/import?job=job-1");

    // The row badge names the failure (its own element, exact text).
    expect(await screen.findByText("Didn't land")).toBeInTheDocument();
    // The finished body appends the count.
    expect(screen.getByText(/1 didn't land/)).toBeInTheDocument();
  });

  test("a decided row that landed shows the Imported badge, not Decided", async () => {
    // Once the album_id follows a decided Apply, the vague "Decided" chip
    // upgrades to the positive "Imported" — album_id != null is the signal.
    server.use(
      http.get(JOB_URL, () =>
        HttpResponse.json(
          makeJob({
            phase: "done",
            progress: { applied: 1, needs_review: 0, skipped: 0, not_landed: 0 },
            albums: [
              {
                index: 0,
                folder: "/music/incoming/Radiohead - In Rainbows",
                artist: "Radiohead",
                album: "In Rainbows",
                recommendation: "strong",
                confidence: 99,
                status: "decided",
                album_id: 77,
                did_not_land: false,
              },
            ],
          }),
        ),
      ),
    );
    renderAt("/import?job=job-1");

    expect(await screen.findByText("Imported")).toBeInTheDocument();
    expect(screen.queryByText("Decided")).not.toBeInTheDocument();
  });

  test("reaching done refreshes the cached library surfaces (albums landed)", async () => {
    // Imported albums are in the library now — within the 30s staleTime the
    // grids/roster/browse/search/stats would otherwise keep serving lists
    // without them.
    server.use(
      http.get(JOB_URL, () =>
        HttpResponse.json(
          makeJob({
            phase: "done",
            progress: { applied: 1, needs_review: 0, skipped: 0, not_landed: 0 },
          }),
        ),
      ),
    );
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const spy = vi.spyOn(queryClient, "invalidateQueries");
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={["/import?job=job-1"]}>
          <Routes>
            <Route path="/import" element={<ImportPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    await screen.findByText("Import finished");
    await waitFor(() => {
      const keys = spy.mock.calls.map(([filters]) => filters?.queryKey);
      expect(keys).toEqual(
        expect.arrayContaining([
          ["albums"],
          ["artists"],
          ["browse"],
          ["search"],
          ["stats"],
          ["album"],
          ["duplicates"],
          ["trash"],
          ["lyrics"],
        ]),
      );
    });
  });

  test("failed shows the error message + a start-over link", async () => {
    server.use(
      http.get(JOB_URL, () =>
        HttpResponse.json(
          makeJob({ phase: "failed", error: "lookup exploded", albums: [] }),
        ),
      ),
    );
    renderAt("/import?job=job-1");

    expect(await screen.findByText("Import failed")).toBeInTheDocument();
    expect(screen.getByText("lookup exploded")).toBeInTheDocument();
    // Two distinct CTAs render, both -> /import: the shared ImportShell chrome's
    // ghost "Start over", and the JobFailed panel's "Import another folder".
    const startOver = screen.getByRole("link", { name: /start over/i });
    expect(startOver).toHaveAttribute("href", "/import");
    const another = screen.getByRole("link", { name: /import another folder/i });
    expect(another).toHaveAttribute("href", "/import");
  });

  test("a failure carries how long the run lasted, on its own line", async () => {
    // The clock stops at both terminal transitions: forty seconds versus forty
    // minutes is a bad path versus a late crash. (Below ELAPSED_AFTER_S no
    // duration renders at all, so a 3s failure is the untimed case.)
    server.use(
      http.get(JOB_URL, () =>
        HttpResponse.json(
          makeJob({
            phase: "failed",
            error: "lookup exploded",
            albums: [],
            elapsed_seconds: 2412,
          }),
        ),
      ),
    );
    renderAt("/import?job=job-1");

    // `error` is the worker's raw `str(exc)`. The duration must NOT read as
    // part of that sentence, and must not be able to open a wrapped line on a
    // bare middot — so it is its own sentence on its own line, not a segment.
    expect(await screen.findByText(/lookup exploded/)).toBeInTheDocument();
    const duration = screen.getByText("Ran for 40m 12s.");
    expect(duration).toHaveAttribute("aria-hidden", "true");
    expect(duration.closest("span.block")).not.toBeNull();
    expect(duration.textContent).not.toContain("·");
    // "40m 12s" is read as a letter; the spoken twin carries the words.
    expect(screen.getByText("Ran for 40 minutes.")).toHaveClass("sr-only");
  });

  test("a transient job-fetch error shows a retry", async () => {
    server.use(http.get(JOB_URL, () => new HttpResponse(null, { status: 500 })));
    renderAt("/import?job=job-1");

    expect(
      await screen.findByText(/couldn.t load the import/i),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument();
  });

  test("a 404 (expired job) shows a not-found notice, not the transient retry", async () => {
    server.use(
      http.get(JOB_URL, () =>
        HttpResponse.json(
          { detail: "Import job not found" },
          { status: 404 },
        ),
      ),
    );
    renderAt("/import?job=job-1");

    expect(
      await screen.findByText(/no longer available/i),
    ).toBeInTheDocument();
    // Distinct from the transient error: offers a fresh start, with no Retry.
    expect(
      screen.getByRole("link", { name: /start a new import/i }),
    ).toHaveAttribute("href", "/import");
    expect(
      screen.queryByRole("button", { name: /retry/i }),
    ).not.toBeInTheDocument();
  });
});

describe("ImportPage — sweep & bank", () => {
  beforeEach(() => {
    // Entry-screen tests poll the active-import probe; default to idle.
    server.use(
      http.get(ACTIVE_URL, () =>
        HttpResponse.json({ active: false, job_id: null }),
      ),
    );
  });

  test("Sweep & bank posts options.sweep and navigates into the job", async () => {
    let body: unknown = null;
    server.use(
      http.post(IMPORT_URL, async ({ request }) => {
        body = await request.json();
        return HttpResponse.json({ job_id: "s1" }, { status: 202 });
      }),
      // the run view it lands on:
      http.get(SWEEP_JOB_URL, () => HttpResponse.json(sweepJob())),
    );
    const user = userEvent.setup();
    renderAt("/import");

    await user.click(
      await screen.findByRole("button", { name: /sweep & bank/i }),
    );
    await user.type(screen.getByLabelText("Folder path"), "/library");
    await user.click(screen.getByRole("button", { name: /start sweep/i }));

    await waitFor(() =>
      expect(body).toEqual({
        path: "/library",
        options: { operation: "default", unattended: false, sweep: true },
      }),
    );
  });

  test("Review now (the default) posts no options — unchanged contract", async () => {
    let body: unknown = null;
    server.use(
      http.post(IMPORT_URL, async ({ request }) => {
        body = await request.json();
        return HttpResponse.json({ job_id: "job-1" }, { status: 202 });
      }),
      http.get(JOB_URL, () =>
        HttpResponse.json(
          makeJob({
            phase: "scanning",
            progress: { applied: 0, needs_review: 0, skipped: 0, not_landed: 0 },
            albums: [],
          }),
        ),
      ),
    );
    const user = userEvent.setup();
    renderAt("/import");

    await user.type(screen.getByLabelText("Folder path"), "/in");
    await user.click(screen.getByRole("button", { name: /start import/i }));

    await waitFor(() => expect(body).toEqual({ path: "/in" }));
  });

  test("a 422 string detail from the start guard is shown verbatim", async () => {
    server.use(
      http.post(IMPORT_URL, () =>
        HttpResponse.json(
          { detail: "In-library sources must move; copy would duplicate files" },
          { status: 422 },
        ),
      ),
    );
    const user = userEvent.setup();
    renderAt("/import");

    await user.type(screen.getByLabelText("Folder path"), "/library");
    await user.click(screen.getByRole("button", { name: /start import/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/must move/);
  });

  test("a sweep-origin job renders the sweep run view: counters + pause", async () => {
    let paused = false;
    server.use(
      http.get(SWEEP_JOB_URL, () =>
        HttpResponse.json(
          sweepJob({
            sweep: {
              processed: 12,
              auto_applied: 8,
              banked: 4,
              skipped_known: 2,
              current_folder: "/library/Adele/21",
              paused: false,
            },
          }),
        ),
      ),
      http.post(SWEEP_PAUSE_URL, () => {
        paused = true;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const user = userEvent.setup();
    renderAt("/import?job=s1");

    expect(await screen.findByText("Processed")).toBeInTheDocument();
    expect(screen.getByText("12")).toBeInTheDocument();
    expect(screen.getByText("Banked")).toBeInTheDocument();
    expect(screen.getByText(/sweeping 21/i)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /pause sweep/i }));
    await waitFor(() => expect(paused).toBe(true));
  });

  // A sweep returns before LiveFeed ever renders, so it carried the elapsed
  // value and showed it nowhere — on the longest-running import there is.
  test("a long sweep's status line carries the elapsed value", async () => {
    server.use(
      http.get(SWEEP_JOB_URL, () =>
        HttpResponse.json(
          sweepJob({
            elapsed_seconds: 3700,
            sweep: {
              processed: 12,
              auto_applied: 8,
              banked: 4,
              skipped_known: 2,
              current_folder: "/library/Adele/21",
              paused: false,
            },
          }),
        ),
      ),
    );
    renderAt("/import?job=s1");

    const segment = await screen.findByText("· 1h 1m");
    expect(segment.closest("p")).toHaveTextContent("Sweeping 21…");
    expect(screen.getByText("1 hour 1 minute.")).toHaveClass("sr-only");
  });

  test("a short sweep's status line reads exactly as it did before", async () => {
    server.use(
      http.get(SWEEP_JOB_URL, () =>
        HttpResponse.json(sweepJob({ elapsed_seconds: ELAPSED_AFTER_S - 1 })),
      ),
    );
    renderAt("/import?job=s1");

    expect(await screen.findByText("Sweeping your folder…")).toBeInTheDocument();
  });

  test("a finished sweep summarizes and links to Review; paused names the pause", async () => {
    server.use(
      http.get(SWEEP_JOB_URL, () =>
        HttpResponse.json(
          sweepJob({
            phase: "done",
            summary: "swept 30, auto-applied 20, banked 10",
            elapsed_seconds: 840,
            sweep: {
              processed: 30,
              auto_applied: 20,
              banked: 10,
              skipped_known: 0,
              current_folder: null,
              paused: true,
            },
          }),
        ),
      ),
    );
    renderAt("/import?job=s1");

    // Exact match: the sr-only announcer also says "Sweep paused. …" — the
    // default whole-text match singles out the visible EmptyState title.
    expect(await screen.findByText("Sweep paused")).toBeInTheDocument();
    // The finished summary carries the run's duration too (the owner's ruling),
    // and says the pause exactly once — in the title above, not again here.
    expect(screen.getByText("· 14m").closest("p")).toHaveTextContent(
      "swept 30, auto-applied 20, banked 10",
    );
    // Whether the summary STRING names the pause is the backend's pin
    // (test_import_registry.test_sweep_summary_reports_counters_and_pause):
    // the frontend renders that string verbatim, so no mutation here could
    // move it. The visible title above is what this page owes.
    expect(
      screen.getByRole("link", { name: /review banked albums/i }),
    ).toHaveAttribute("href", "/review");
  });

  test("the resume banner names a running sweep", async () => {
    server.use(
      http.get(ACTIVE_URL, () =>
        HttpResponse.json({
          active: true,
          job_id: "s1",
          origin: "sweep",
          needs_review_count: 0,
          sweep: {
            processed: 3,
            auto_applied: 2,
            banked: 1,
            skipped_known: 0,
            current_folder: null,
            paused: false,
          },
        }),
      ),
    );
    renderAt("/import");

    expect(await screen.findByText(/a sweep is running/i)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /resume/i })).toHaveAttribute(
      "href",
      "/import?job=s1",
    );
  });
});
