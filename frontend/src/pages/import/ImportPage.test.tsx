import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";
import { beforeEach, describe, expect, test, vi } from "vitest";

import type { ImportJobState } from "@/api/useImport";
import { ImportPage } from "@/pages/import/ImportPage";
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
    progress: { applied: 1, needs_review: 1, skipped: 0 },
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
      },
    ],
    summary: null,
    error: null,
    origin: "manual",
    set_aside: 0,
    ...overrides,
  };
}

/** A sweep-origin job: no per-album feed by design — the sweep block carries
 * the whole progress story. Mirrors makeJob for the banking run view. */
function sweepJob(overrides: Partial<ImportJobState> = {}): ImportJobState {
  return {
    job_id: "s1",
    phase: "scanning",
    progress: { applied: 0, needs_review: 0, skipped: 0 },
    albums: [],
    summary: null,
    error: null,
    origin: "sweep",
    set_aside: 0,
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
            progress: { applied: 0, needs_review: 0, skipped: 0 },
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
            progress: { applied: 0, needs_review: 0, skipped: 0 },
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
            progress: { applied: 0, needs_review: 0, skipped: 0 },
            albums: [],
          }),
        ),
      ),
    );
    renderAt("/import?job=job-1");

    expect(await screen.findByText(/scanning your folder/i)).toBeInTheDocument();
  });

  test("the live cue surfaces a skipped count when any album was skipped", async () => {
    server.use(
      http.get(JOB_URL, () =>
        HttpResponse.json(
          makeJob({
            phase: "reviewing",
            progress: { applied: 1, needs_review: 1, skipped: 1 },
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
  test("done links each applied album to its library page — no blanket view-in-library", async () => {
    server.use(
      http.get(JOB_URL, () =>
        HttpResponse.json(
          makeJob({
            phase: "done",
            summary: "1 imported, 1 skipped",
            progress: { applied: 1, needs_review: 0, skipped: 1 },
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

  test("reaching done refreshes the cached library surfaces (albums landed)", async () => {
    // Imported albums are in the library now — within the 30s staleTime the
    // grids/roster/browse/search/stats would otherwise keep serving lists
    // without them.
    server.use(
      http.get(JOB_URL, () =>
        HttpResponse.json(
          makeJob({
            phase: "done",
            progress: { applied: 1, needs_review: 0, skipped: 0 },
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
            progress: { applied: 0, needs_review: 0, skipped: 0 },
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

  test("a finished sweep summarizes and links to Review; paused names the pause", async () => {
    server.use(
      http.get(SWEEP_JOB_URL, () =>
        HttpResponse.json(
          sweepJob({
            phase: "done",
            summary: "Swept 30 albums - paused",
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
