import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";
import { beforeEach, describe, expect, test, vi } from "vitest";

import type { ImportJobState } from "@/api/useImport";
import type { AppIcon } from "@/components/icons";
import { Pause, Success } from "@/components/icons";
import { ImportPage } from "@/pages/import/ImportPage";
import { ELAPSED_AFTER_S } from "@/pages/import/importStatus";
import {
  containerQueryVariants,
  unwiredContainerQueries,
} from "@/test/containerQuery";
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

/** The `d` of an icon concept's glyph. Phosphor renders no name attribute, so
 * the only way to assert WHICH icon a panel wears is to compare its path
 * against the concept module's own render. */
function pathOf(Icon: AppIcon): string {
  const { container, unmount } = render(<Icon aria-hidden="true" />);
  const d = container.querySelector("path")?.getAttribute("d") ?? "";
  unmount();
  return d;
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

  // decisions 39, extended to this feed by the owner (2026-09-11). Under 20rem
  // of ROW the Review/Resolve button drops to its own line under the row: with
  // it inline the title measured 0px wide at viewport 320→344 on a parked
  // duplicate and 320→328 on a needs_review row. jsdom computes no layout, so
  // what a test can hold is the structure — the button is a grid item of the
  // row WRAPPER (from inside AlbumRow's `action` slot it could not take a line
  // without growing AlbumRow's box) and both arms are named, so dropping
  // either one fails. The widths, the threshold's derivation and the
  // untouched live announcer are in the branch's browser pass.
  test("a parked row's action is a grid item of the row, with both arms named", async () => {
    server.use(http.get(JOB_URL, () => HttpResponse.json(makeJob())));
    renderAt("/import?job=job-1");

    const review = await screen.findByRole("link", { name: /review/i });
    const group = review.parentElement;
    const wrapper = group?.parentElement;
    expect(wrapper).not.toBeNull();
    expect(wrapper?.className.split(/\s+/)).toContain("grid");
    // The threshold is measured against the ROW, not the viewport: at 768px
    // the sidebar opens and the row is NARROWER than at 520px.
    expect(wrapper?.className.split(/\s+/)).toContain("@container/feedrow");
    for (const token of [
      "col-start-1",
      "row-start-2",
      "@min-[20rem]/feedrow:col-start-2",
      "@min-[20rem]/feedrow:row-start-1",
    ]) {
      expect(group?.className.split(/\s+/)).toContain(token);
    }
    // Every container query on the row, and every one of them wired to a
    // container an ancestor declares — a renamed container applies nothing at
    // all, silently.
    expect(containerQueryVariants(wrapper as HTMLElement)).toEqual([
      "@min-[18rem]/rowtext:block",
      "@min-[18rem]/rowtext:flex-row",
      "@min-[18rem]/rowtext:items-center",
      "@min-[20rem]/feedrow:-ml-1",
      "@min-[20rem]/feedrow:col-start-2",
      "@min-[20rem]/feedrow:mb-0",
      "@min-[20rem]/feedrow:mr-4",
      "@min-[20rem]/feedrow:row-start-1",
    ]);
    expect(unwiredContainerQueries(wrapper as HTMLElement)).toEqual([]);
  });

  test("a feed row with no button keeps the plain box it always had", async () => {
    server.use(http.get(JOB_URL, () => HttpResponse.json(makeJob())));
    renderAt("/import?job=job-1");

    // index 0 is `applied`: a title link, no action. Only a row that carries a
    // button can gain the dropped line, so every other feed row is untouched.
    const applied = await screen.findByRole("link", { name: "OK Computer" });
    const wrapper = applied.closest("li")?.firstElementChild;
    expect(wrapper?.className.split(/\s+/)).not.toContain("grid");
    expect(wrapper?.className.split(/\s+/)).not.toContain("@container/feedrow");
    expect(containerQueryVariants(wrapper as HTMLElement)).toEqual([
      "@min-[18rem]/rowtext:block",
      "@min-[18rem]/rowtext:flex-row",
      "@min-[18rem]/rowtext:items-center",
    ]);
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
    // Two units, so the line moves on every poll rather than once a minute — a
    // frozen line is the very thing being ruled out. (Per poll, not per second:
    // a run blocked on a person backs off to a 10 s poll.)
    const segment = await screen.findByText("· 2m 12s");
    expect(lineOf(segment)).toHaveTextContent("Scanning your folder…");
    // The middot's trailing space is non-breaking, so a wrap can never strand
    // it at the end of a line. (RTL normalizes it away, hence the raw read.)
    expect(segment.textContent).toContain("·\u00a0");
    expect(segment.textContent).toContain("2m\u00a012s");
    // A screen reader reads "2m" as a letter, so the visible half is hidden and
    // a spoken twin carries the words — opening with the full stop that stands
    // in for the unspoken middot.
    expect(segment).toHaveAttribute("aria-hidden", "true");
    expect(screen.getByText(". 2 minutes.")).toHaveClass("sr-only");
  });

  // ELAPSED_AFTER_S is 30, not 60, precisely so this band renders at all. Both
  // halves of the segment read the VISIBLE label's floor, and the component
  // returns null when either half is null — so a spoken twin left on
  // `spokenElapsed`'s 60s default deletes the visible number too, for every run
  // between 30 and 59 seconds. Nothing else in the suite renders inside it.
  test("a run in the 30-59s band renders both halves of the segment", async () => {
    server.use(
      http.get(JOB_URL, () =>
        HttpResponse.json(makeJob({ elapsed_seconds: 45 })),
      ),
    );
    renderAt("/import?job=job-1");

    const segment = await screen.findByText("· 45s");
    expect(segment).toHaveAttribute("aria-hidden", "true");
    expect(lineOf(segment)).toHaveTextContent("1 album imported");
    expect(screen.getByText(". 45 seconds.")).toHaveClass("sr-only");
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

  test("each spinner's top correction matches its own icon size", async () => {
    // The extracted `StatusLine` and the resume banner are two shapes of the
    // same line, and this is the invariant both owe: the spinner sits on the
    // FIRST line box when the text wraps. `items-start` alone does not do it —
    // the icon needs (line-height 20px - icon size) / 2 of top margin, which is
    // 2px (`mt-0.5`) for the status line's size-4 icon and ZERO for the
    // banner's size-5 one, because 20px already matches the line box.
    //
    // jsdom cannot measure a line box, so what is asserted here is the pairing
    // in BOTH directions: a correction copied onto the banner, or dropped from
    // the status line, breaks a half. The browser pass measures the offset
    // itself (0px at 1280 and at 360, on all three lines).
    server.use(
      http.get(JOB_URL, () => HttpResponse.json(makeJob({ elapsed_seconds: 300 }))),
    );
    const feed = renderAt("/import?job=job-1");
    const status = spinnerOf(lineOf(await screen.findByText("· 5m")));
    expect(status.getAttribute("class")?.split(/\s+/)).toEqual(
      expect.arrayContaining(["size-4", "mt-0.5"]),
    );
    feed.unmount();

    server.use(
      http.get(ACTIVE_URL, () =>
        HttpResponse.json({
          active: true,
          job_id: "job-9",
          origin: "inbox",
          needs_review_count: 3,
        }),
      ),
    );
    renderAt("/import");
    await screen.findByRole("link", { name: /resume/i });
    const banner = spinnerOf(
      lineOf(screen.getByText(/3 albums set aside for review/)),
    );
    const bannerTokens = banner.getAttribute("class")?.split(/\s+/) ?? [];
    expect(bannerTokens).toContain("size-5");
    expect(bannerTokens).not.toContain("mt-0.5");
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
    expect(screen.getByText(". 10 minutes.")).toHaveClass("sr-only");
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
  test("the finished panel keeps the run's elapsed value", async () => {
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
    expect(screen.getByText(". 14 minutes.")).toHaveClass("sr-only");
  });

  test("a short run's finished panel gains no extra text", async () => {
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
    // must flag the failure and the done body must own up to it.
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
    // The finished body appends the count...
    expect(
      screen.getByText("0 albums imported · 0 skipped · 1 didn't land"),
    ).toBeInTheDocument();
    // ...and so does the one live region, which used to drop it — `not_landed`
    // is computed for BOTH terminal phases, and only the failed announcement
    // said it.
    expect(screen.getByRole("status")).toHaveTextContent(
      "Import complete. Imported 0, skipped 0. 1 didn't land.",
    );
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

  test.each([
    {
      what: "a failed import",
      title: "Import failed",
      url: JOB_URL,
      route: "/import?job=job-1",
      body: () => makeJob({ phase: "failed", error: "lookup exploded", albums: [] }),
    },
    {
      what: "a failed sweep",
      title: "Sweep failed",
      url: SWEEP_JOB_URL,
      route: "/import?job=s1",
      body: () => sweepJob({ phase: "failed", error: "disk full" }),
    },
  ])("$what wears the destructive chrome, not the finished panel's", async ({
    title,
    url,
    route,
    body,
  }) => {
    // The failed box used to be byte-identical to the finished one — same
    // dashed neutral border, same muted icon — so the word "failed" in the
    // title was the only thing carrying the outcome, next to earned counts.
    server.use(http.get(url, () => HttpResponse.json(body())));
    renderAt(route);

    const panel = (await screen.findByText(title)).closest(
      "[data-slot='empty-state']",
    );
    expect(panel).toHaveAttribute("data-tone", "destructive");
    expect(panel).toHaveClass("border-destructive/40", "bg-destructive/5");
    expect(panel).not.toHaveClass("border-dashed");
    expect(panel?.querySelector("svg")).toHaveClass("text-destructive");
    // The recovery is still a navigation, never ErrorState's mandatory Retry:
    // a dead worker has nothing to re-run.
    expect(
      screen.queryByRole("button", { name: /retry/i }),
    ).not.toBeInTheDocument();
  });

  test("the finished panel keeps the neutral chrome the failed one gave up", async () => {
    // The other half of the pair: if this ever went destructive too, the two
    // outcomes would be one box again and the test above would still pass.
    server.use(
      http.get(JOB_URL, () => HttpResponse.json(makeJob({ phase: "done" }))),
    );
    renderAt("/import?job=job-1");

    const panel = (await screen.findByText("Import finished")).closest(
      "[data-slot='empty-state']",
    );
    expect(panel).toHaveAttribute("data-tone", "neutral");
    expect(panel).toHaveClass("border-dashed");
    expect(panel).not.toHaveClass("bg-destructive/5");
    expect(panel?.querySelector("svg")).toHaveClass("text-muted-foreground");
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
    // "40m 12s" is read as a letter; the spoken twin carries the words.
    expect(screen.getByText("Ran for 40 minutes.")).toHaveClass("sr-only");
  });

  // The same band at the other helper. `elapsedSentence` pairs the two halves on
  // the same floor and returns undefined when either is missing, so a twin left
  // on the 60s default takes the whole visible line with it. ELAPSED_AFTER_S
  // itself, so the floor is pinned at its own boundary (the sibling test above
  // holds ELAPSED_AFTER_S - 1 down).
  test("a failure in the 30-59s band keeps both halves of its duration line", async () => {
    server.use(
      http.get(JOB_URL, () =>
        HttpResponse.json(
          makeJob({
            phase: "failed",
            error: "lookup exploded",
            albums: [],
            elapsed_seconds: ELAPSED_AFTER_S,
          }),
        ),
      ),
    );
    renderAt("/import?job=job-1");

    const duration = await screen.findByText("Ran for 30s.");
    expect(duration).toHaveAttribute("aria-hidden", "true");
    expect(screen.getByText("Ran for 30 seconds.")).toHaveClass("sr-only");
  });

  test("a failed run reports the counts it earned, and its feed, read-only", async () => {
    // A crash mid-apply is exactly when albums land or fail to land, so the
    // server keeps reporting this job's counters and computes `not_landed`
    // BECAUSE the job is terminal. The panel must not present a run that
    // imported albums as though nothing happened.
    server.use(
      http.get(JOB_URL, () =>
        HttpResponse.json(
          makeJob({
            phase: "failed",
            error: "lookup exploded",
            elapsed_seconds: 2412,
            progress: { applied: 200, needs_review: 1, skipped: 3, not_landed: 2 },
            // makeJob's rows are applied + needs_review only, so the Resolve
            // assertion below never reached its branch — it passed with
            // `readOnly` deleted. A parked duplicate is what renders that link.
            albums: [
              ...makeJob().albums,
              {
                index: 2,
                folder: "/music/incoming/Amnesiac",
                artist: "Radiohead",
                album: "Amnesiac",
                recommendation: "medium",
                confidence: 80,
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

    // Still unmistakably a failure...
    expect(await screen.findByText("Import failed")).toBeInTheDocument();
    expect(screen.getByText("lookup exploded")).toBeInTheDocument();
    // ...that owns up to what it did, on its own line — not glued to the raw
    // exception by the middot dialect.
    const counts = screen.getByText(
      "200 albums imported · 3 skipped · 2 didn't land",
    );
    expect(counts).toHaveClass("block");
    expect(screen.getByText("Ran for 40m 12s.")).toBeInTheDocument();
    // The feed rows survive the failure too — but read-only: the worker is
    // gone, so a Review button here would open a decision nothing consumes.
    // makeJob's second row is `needs_review`, which is exactly that button.
    expect(screen.getByText("OK Computer")).toBeInTheDocument();
    expect(screen.getByText("Kid A")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Review" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Resolve" })).not.toBeInTheDocument();
    // Nothing was banked, so the CTA is still a fresh run.
    expect(
      screen.getByRole("link", { name: /import another folder/i }),
    ).toHaveAttribute("href", "/import");
  });

  // The owner's unattended inbox path. `progress` does NOT partition the run: a
  // needs_review row is refused by the server's _is_imported AND its _is_skipped
  // and never landed, so it is in none of the three counters — and on a failed
  // job its Review button is gone. The panel rendered five rows badged "Needs
  // review" with no action and no explanation, and said "The import failed."
  test("a failure that set albums aside owns them, in the panel and the announcement", async () => {
    server.use(
      http.get(JOB_URL, () =>
        HttpResponse.json(
          makeJob({
            phase: "failed",
            error: "the session died",
            origin: "inbox",
            set_aside: 5,
            progress: { applied: 2, needs_review: 5, skipped: 0, not_landed: 0 },
          }),
        ),
      ),
    );
    renderAt("/import?job=job-1");

    expect(await screen.findByText("Import failed")).toBeInTheDocument();
    expect(
      screen.getByText("5 albums set aside, not imported."),
    ).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent(
      "The import failed. Imported 2, skipped 0. 5 albums set aside.",
    );
  });

  test("a run that set nothing aside gains no set-aside line", async () => {
    server.use(
      http.get(JOB_URL, () =>
        HttpResponse.json(
          makeJob({
            phase: "failed",
            error: "the session died",
            progress: { applied: 2, needs_review: 0, skipped: 1, not_landed: 0 },
          }),
        ),
      ),
    );
    renderAt("/import?job=job-1");

    expect(await screen.findByText("Import failed")).toBeInTheDocument();
    expect(screen.queryByText(/set aside/)).not.toBeInTheDocument();
  });

  // The gate was `applied + skipped + not_landed > 0`, so this run printed the
  // two zeros the early-crash branch exists to avoid.
  test("a failure that only lost albums opens on the loss, not on two zeros", async () => {
    server.use(
      http.get(JOB_URL, () =>
        HttpResponse.json(
          makeJob({
            phase: "failed",
            error: "the session died",
            albums: [],
            progress: { applied: 0, needs_review: 0, skipped: 0, not_landed: 5 },
          }),
        ),
      ),
    );
    renderAt("/import?job=job-1");

    expect(await screen.findByText("Import failed")).toBeInTheDocument();
    expect(screen.getByText("5 didn't land")).toBeInTheDocument();
    expect(screen.queryByText(/albums imported/)).not.toBeInTheDocument();
    // The announcer owed the same correction, in its own dialect.
    expect(screen.getByRole("status")).toHaveTextContent(
      "The import failed. 5 didn't land.",
    );
  });

  test("a run that died during the scan gains no count line", async () => {
    server.use(
      http.get(JOB_URL, () =>
        HttpResponse.json(
          makeJob({
            phase: "failed",
            error: "lookup exploded",
            albums: [],
            progress: { applied: 0, needs_review: 0, skipped: 0, not_landed: 0 },
          }),
        ),
      ),
    );
    renderAt("/import?job=job-1");

    expect(await screen.findByText("Import failed")).toBeInTheDocument();
    // It landed, skipped and lost nothing — "0 albums imported · 0 skipped"
    // would be noise, so the early-crash panel reads exactly as it did before.
    expect(screen.queryByText(/albums imported/)).not.toBeInTheDocument();
  });

  // The feed pins a pending row to the top so its Review button stays in view.
  // `readOnly` deletes that button, so on a failed job the pin only reorders the
  // record of the run — it reads newest-first, like the done screen.
  test("a read-only feed does not pin the pending row to the top", async () => {
    server.use(
      http.get(JOB_URL, () =>
        HttpResponse.json(
          makeJob({
            phase: "failed",
            error: "the session died",
            progress: { applied: 1, needs_review: 1, skipped: 0, not_landed: 0 },
            // The PENDING row is the OLDER one here, so the pin and newest-first
            // disagree — with both the same way round the test proves nothing.
            albums: [
              {
                index: 0,
                folder: "/music/incoming/Kid A",
                artist: "Radiohead",
                album: "Kid A",
                recommendation: "medium",
                confidence: 76,
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
            ],
          }),
        ),
      ),
    );
    renderAt("/import?job=job-1");

    await screen.findByText("Import failed");
    const titles = [...document.querySelectorAll("li")].map(
      (li) => li.textContent ?? "",
    );
    expect(titles).toHaveLength(2);
    expect(titles[0]).toContain("OK Computer");
    expect(titles[1]).toContain("Kid A");
  });

  test("a failed sweep is still a sweep: its tiles, and the Review hand-off", async () => {
    server.use(
      http.get(SWEEP_JOB_URL, () =>
        HttpResponse.json(
          sweepJob({
            phase: "failed",
            error: "disk full",
            elapsed_seconds: 2412,
            sweep: {
              processed: 200,
              auto_applied: 150,
              banked: 40,
              skipped_known: 10,
              current_folder: null,
              paused: false,
            },
          }),
        ),
      ),
    );
    renderAt("/import?job=s1");

    // Recognisable as a sweep, not as a generic "Import failed".
    expect(await screen.findByText("Sweep failed")).toBeInTheDocument();
    expect(screen.getByText("disk full")).toBeInTheDocument();
    expect(screen.getByText("Ran for 40m 12s.")).toBeInTheDocument();
    // Its counts are its tiles — the same four the finished panel shows, said
    // once. A sweep's `progress` is all zeros, so a count SENTENCE would read
    // "0 albums imported".
    for (const [label, value] of [
      ["Processed", "200"],
      ["Imported", "150"],
      ["Banked", "40"],
      ["Already known", "10"],
    ]) {
      expect(screen.getByText(label)).toBeInTheDocument();
      expect(screen.getByText(value)).toBeInTheDocument();
    }
    expect(screen.queryByText(/albums imported/)).not.toBeInTheDocument();
    // It banked 40 albums before it died — the hand-off to Review survives.
    expect(
      screen.getByRole("link", { name: /review banked albums/i }),
    ).toHaveAttribute("href", "/review");
    // And no Pause: there is nothing left to pause.
    expect(
      screen.queryByRole("button", { name: /pause sweep/i }),
    ).not.toBeInTheDocument();
  });

  test("a failed sweep that banked nothing offers a fresh run", async () => {
    server.use(
      http.get(SWEEP_JOB_URL, () =>
        HttpResponse.json(
          sweepJob({ phase: "failed", error: "disk full", elapsed_seconds: 2412 }),
        ),
      ),
    );
    renderAt("/import?job=s1");

    expect(await screen.findByText("Sweep failed")).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: /import another folder/i }),
    ).toHaveAttribute("href", "/import");
    expect(
      screen.queryByRole("link", { name: /review banked albums/i }),
    ).not.toBeInTheDocument();
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

  // The Pagination rule: a trigger that holds focus must not become `disabled`
  // on its own click — the browser drops focus to <body> and the next Tab
  // restarts at the top of the document. The Review page's Pause (the same
  // mutation, the same state) already reads this way.
  test("Pause keeps focus and swallows the re-click instead of disabling", async () => {
    let paused = false;
    let posts = 0;
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
              paused,
            },
          }),
        ),
      ),
      http.post(SWEEP_PAUSE_URL, () => {
        posts += 1;
        paused = true;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const user = userEvent.setup();
    renderAt("/import?job=s1");

    const button = await screen.findByRole("button", { name: /pause sweep/i });
    await user.click(button);

    // The label carries the state, and it is keyed on the SAME expression as
    // the aria state — keyed on `sweep.paused` alone it still read "Pause
    // sweep" for the whole in-flight window.
    await waitFor(() => expect(button).toHaveTextContent("Pausing…"));
    expect(button).toHaveAttribute("aria-disabled", "true");
    // THE oracle. Mutation-checked: restoring `disabled` alongside the aria
    // attribute fails on this line and nothing else.
    expect(button).not.toBeDisabled();
    // Intent, not a second oracle — jsdom does not blur a focused element when
    // it becomes disabled, so this assertion passes either way here. The focus
    // itself was measured in Chromium (it dropped to <body> within 50ms).
    expect(document.activeElement).toBe(button);

    // ...and inert all the same.
    await user.click(button);
    expect(posts).toBe(1);
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
    expect(screen.getByText(". 1 hour 1 minute.")).toHaveClass("sr-only");
  });

  test("a paused sweep is announced without waiting out the throttle", async () => {
    // The announcer is throttled to 4s so a 1s poll cannot spam it, and at
    // mount that window holds it on the "Loading the import." placeholder. A
    // pause is user-initiated — the person just pressed a button and is owed an
    // answer — so it bypasses the window, as the terminal states already do.
    server.use(
      http.get(SWEEP_JOB_URL, () =>
        HttpResponse.json(
          sweepJob({
            phase: "applying",
            sweep: {
              processed: 6,
              auto_applied: 4,
              banked: 2,
              skipped_known: 0,
              current_folder: "/library/Adele/21",
              paused: true,
            },
          }),
        ),
      ),
    );
    renderAt("/import?job=s1");

    // waitFor's default ceiling is 1000ms — a quarter of the throttle window,
    // so a pass here cannot be the window simply elapsing.
    //
    // Anchored, not substring-matched: the bypass is sticky, so this string is
    // what the announcer holds for the rest of the run, and `role="status"` is
    // atomic — anything appended to it is re-read in full every time a counter
    // moves. The counters above are nonzero so a regression that puts them back
    // has something to say.
    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent(
        /^Stopping after this album\.$/,
      ),
    );
  });

  test("the announcer's throttle is still in force for a sweep nobody paused", async () => {
    // The control the test above needs. Without it, a throttle that held
    // nothing back at all would produce the same pass; here the visible line
    // has already rendered while the spoken one is still the placeholder.
    server.use(
      http.get(SWEEP_JOB_URL, () =>
        HttpResponse.json(
          sweepJob({
            phase: "applying",
            sweep: {
              processed: 6,
              auto_applied: 4,
              banked: 2,
              skipped_known: 0,
              current_folder: "/library/Adele/21",
              paused: false,
            },
          }),
        ),
      ),
    );
    renderAt("/import?job=s1");

    expect(await screen.findByText("Sweeping 21…")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Loading the import.");
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

  test("a finished sweep states each count once — on the tiles, not twice", async () => {
    server.use(
      http.get(SWEEP_JOB_URL, () =>
        HttpResponse.json(
          sweepJob({
            phase: "done",
            elapsed_seconds: 840,
            sweep: {
              processed: 30,
              auto_applied: 20,
              banked: 10,
              skipped_known: 1,
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
    // The backend's sentence is gone from the panel...
    expect(screen.queryByText(/swept 30/)).not.toBeInTheDocument();
    expect(screen.queryByText(/auto-applied/)).not.toBeInTheDocument();
    // ...and every number it carried renders exactly once, on its tile.
    for (const value of ["30", "20", "10", "1"]) {
      expect(screen.getAllByText(value)).toHaveLength(1);
    }
    // What the tiles cannot say survives (the owner's ruling): how long it ran.
    expect(screen.getByText("Ran for 14m.")).toHaveAttribute(
      "aria-hidden",
      "true",
    );
    // "14m" is read as a letter; the spoken twin carries the words.
    expect(screen.getByText("Ran for 14 minutes.")).toHaveClass("sr-only");
    // ...and how to pick a paused sweep back up. That sentence lives on the
    // RUNNING panel, gated on `!done` — so it vanished at the one moment it
    // applies, leaving a paused-and-finished sweep with no resume instruction.
    expect(
      screen.getByText("Resume later by sweeping the same folder again."),
    ).toBeInTheDocument();
    // The pause itself is still said exactly once, in the title above.
    const body = screen.getByText("Ran for 14m.").closest("p");
    expect(body?.textContent).not.toContain("aused");
    // A user-interrupted sweep is not a completion, so it must not wear the
    // success check.
    const glyph = screen
      .getByText("Sweep paused")
      .closest("[data-slot='empty-state']")
      ?.querySelector("svg path");
    expect(glyph?.getAttribute("d")).toBe(pathOf(Pause));
    expect(glyph?.getAttribute("d")).not.toBe(pathOf(Success));
    expect(
      screen.getByRole("link", { name: /review banked albums/i }),
    ).toHaveAttribute("href", "/review");
  });

  test("a fast finished sweep with real counts renders no body line at all", async () => {
    // Below ELAPSED_AFTER_S there is no duration sentence, and a sweep that did
    // something needs no note — so EmptyState must receive `undefined`, not an
    // empty node, or it paints an empty <p> and its gap under the title.
    server.use(
      http.get(SWEEP_JOB_URL, () =>
        HttpResponse.json(
          sweepJob({
            phase: "done",
            elapsed_seconds: ELAPSED_AFTER_S - 1,
            sweep: {
              processed: 3,
              auto_applied: 3,
              banked: 0,
              skipped_known: 0,
              current_folder: null,
              paused: false,
            },
          }),
        ),
      ),
    );
    renderAt("/import?job=s1");

    const panel = (await screen.findByText("Sweep finished")).closest(
      "[data-slot='empty-state']",
    );
    expect(panel).not.toBeNull();
    // The title paragraph, and nothing else — no empty body, no lone middot.
    expect(panel?.querySelectorAll("p")).toHaveLength(1);
    expect(panel?.textContent).not.toContain("·");
  });

  test("a sweep that found nothing says so instead of showing a bare title", async () => {
    // Four zero tiles report that nothing happened without saying why, and the
    // body was empty here because the run finished well under the threshold.
    server.use(
      http.get(SWEEP_JOB_URL, () =>
        HttpResponse.json(
          sweepJob({
            phase: "done",
            elapsed_seconds: ELAPSED_AFTER_S - 1,
          }),
        ),
      ),
    );
    renderAt("/import?job=s1");

    expect(await screen.findByText("Sweep finished")).toBeInTheDocument();
    expect(
      screen.getByText("No albums found in that folder."),
    ).toBeInTheDocument();
  });

  test.each([
    {
      what: "banked 2 for review",
      auto_applied: 3,
      banked: 2,
      skipped_known: 0,
      cta: "Review banked albums",
      href: "/review",
    },
    {
      what: "imported 5 and banked none",
      auto_applied: 5,
      banked: 0,
      skipped_known: 0,
      cta: "See them in the library",
      href: "/browse?sort=added",
    },
    {
      what: "only re-skipped what it already had",
      auto_applied: 0,
      banked: 0,
      skipped_known: 5,
      cta: "Import another folder",
      href: "/import",
    },
  ])(
    "a finished sweep that $what offers $cta",
    async ({ auto_applied, banked, skipped_known, cta, href }) => {
      // With the counts moved to the tiles, this CTA is the only thing under
      // the title, so it has to point at what the run actually produced: banked
      // albums are decisions waiting; an auto-applied run has no feed of its
      // own and nothing to review, so /import just repeated the shell chrome's
      // "Start over" and left 150 fresh albums with no route to them.
      server.use(
        http.get(SWEEP_JOB_URL, () =>
          HttpResponse.json(
            sweepJob({
              phase: "done",
              sweep: {
                processed: auto_applied + banked + skipped_known,
                auto_applied,
                banked,
                skipped_known,
                current_folder: null,
                paused: false,
              },
            }),
          ),
        ),
      );
      renderAt("/import?job=s1");

      const link = await screen.findByRole("link", { name: cta });
      expect(link).toHaveAttribute("href", href);
      // Exactly one action under the title — the other two branches must not
      // also render.
      for (const other of [
        "Review banked albums",
        "See them in the library",
        "Import another folder",
      ].filter((label) => label !== cta)) {
        expect(
          screen.queryByRole("link", { name: other }),
        ).not.toBeInTheDocument();
      }
    },
  );

  test("a failed sweep that only auto-imported still points at the library", async () => {
    // The crash did not move the albums, and the panel above this CTA already
    // says 150 were imported — so "Import another folder" was a dead end.
    server.use(
      http.get(SWEEP_JOB_URL, () =>
        HttpResponse.json(
          sweepJob({
            phase: "failed",
            error: "disk full",
            sweep: {
              processed: 200,
              auto_applied: 150,
              banked: 0,
              skipped_known: 0,
              current_folder: null,
              paused: false,
            },
          }),
        ),
      ),
    );
    renderAt("/import?job=s1");

    const link = await screen.findByRole("link", {
      name: "See them in the library",
    });
    expect(link).toHaveAttribute("href", "/browse?sort=added");
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
