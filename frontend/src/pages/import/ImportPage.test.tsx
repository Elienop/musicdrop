import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, test } from "vitest";

import type { ImportJobState } from "@/api/useImport";
import { ImportPage } from "@/pages/import/ImportPage";
import { renderWithProviders } from "@/test/render";
import { server } from "@/test/msw-server";

const IMPORT_URL = `${window.location.origin}/api/import`;
const JOB_URL = `${window.location.origin}/api/import/job-1`;

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
      },
      {
        index: 1,
        folder: "/music/incoming/Unknown Album",
        artist: "Radiohead",
        album: "Kid A",
        recommendation: "medium",
        confidence: 76,
        status: "needs_review",
      },
    ],
    summary: null,
    error: null,
    ...overrides,
  };
}

/** Render at a given URL so `useSearchParams` (the `?job=` seam) resolves. */
function renderAt(route: string) {
  return renderWithProviders(<ImportPage />, { route, path: "/import" });
}

describe("ImportPage — entry", () => {
  test("shows the path input + Start when there is no active job", () => {
    renderAt("/import");
    expect(
      screen.getByRole("heading", { name: "Import music" }),
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

    // The job query now drives the page (heading switches to "Import").
    expect(
      await screen.findByRole("heading", { name: "Import" }),
    ).toBeInTheDocument();
    expect(seenBody).toEqual({ path: "/music/incoming" });
  });

  test("a 409 surfaces 'already running' without flipping away", async () => {
    server.use(
      http.post(IMPORT_URL, () =>
        HttpResponse.json(
          { detail: "An import is already running" },
          { status: 409 },
        ),
      ),
    );
    const user = userEvent.setup();
    renderAt("/import");

    await user.type(screen.getByLabelText("Folder path"), "/music/incoming");
    await user.click(screen.getByRole("button", { name: /start import/i }));

    expect(
      await screen.findByText(/an import is already running/i),
    ).toBeInTheDocument();
    // Still on the entry screen (no ?job=, so the input is still shown).
    expect(screen.getByLabelText("Folder path")).toBeInTheDocument();
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

  test("the Review affordance links to the right album index", async () => {
    server.use(http.get(JOB_URL, () => HttpResponse.json(makeJob())));
    renderAt("/import?job=job-1");

    const review = await screen.findByRole("link", { name: /review/i });
    // index 1 is the needs_review album; the link carries the job id across the
    // chunk-4 seam.
    expect(review).toHaveAttribute("href", "/import/albums/1?job=job-1");
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
});

describe("ImportPage — terminal states", () => {
  test("done shows the imported/skipped outcome + a View-in-library link", async () => {
    server.use(
      http.get(JOB_URL, () =>
        HttpResponse.json(
          makeJob({
            phase: "done",
            summary: "2 imported, 0 skipped",
            progress: { applied: 2, needs_review: 0, skipped: 0 },
          }),
        ),
      ),
    );
    renderAt("/import?job=job-1");

    expect(await screen.findByText("Import finished")).toBeInTheDocument();
    // The outcome is derived from progress (structured), counting auto-applied
    // strong albums — not the raw summary string.
    expect(screen.getByText(/2 albums imported/i)).toBeInTheDocument();
    expect(screen.getByText(/0 skipped/i)).toBeInTheDocument();
    const link = screen.getByRole("link", { name: /view in library/i });
    expect(link).toHaveAttribute("href", "/");
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
});
