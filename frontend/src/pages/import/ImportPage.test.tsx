import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { delay, http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";
import { beforeEach, describe, expect, test, vi } from "vitest";

import type { ImportAlbumSummary, ImportJobState } from "@/api/useImport";
import type { AppIcon } from "@/components/icons";
import { Pause, Stop, Success } from "@/components/icons";
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
// The sweep's Pause posts the same /stop the run page's Stop does — one
// route, two labels.
const SWEEP_STOP_URL = `${window.location.origin}/api/import/s1/stop`;
const IMPORT_OP_URL = `${window.location.origin}/api/config/import-operation`;

// The path box's line reads what imports do with the files. Every entry
// render asks; `move` is the starter's answer.
beforeEach(() => {
  server.use(
    http.get(IMPORT_OP_URL, () => HttpResponse.json({ operation: "move" })),
  );
});

function makeJob(overrides: Partial<ImportJobState> = {}): ImportJobState {
  return {
    job_id: "job-1",
    phase: "reviewing",
    progress: { applied: 1, needs_review: 1, skipped: 0, not_landed: 0, already_known: 0 },
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
    // Default: nobody pressed Stop. A test that means "the run was stopped"
    // says `stopped: true` — the job flag, not a row status.
    stopped: false,
    // The two flags are separate answers and a terminal fixture owes both.
    // `stopped` = the press was accepted; `aborted` = it reached the worker and
    // ended the run early. `stopped: true, aborted: false` at a terminal phase
    // is the real state where the stop arrived after the last album had landed,
    // and the done panel keys on `aborted` for exactly that reason.
    aborted: false,
    ...overrides,
  };
}

/** A sweep-origin job: no per-album feed by design — the sweep block carries
 * the whole progress story. Mirrors makeJob for the banking run view. */
function sweepJob(overrides: Partial<ImportJobState> = {}): ImportJobState {
  return {
    job_id: "s1",
    phase: "scanning",
    progress: { applied: 0, needs_review: 0, skipped: 0, not_landed: 0, already_known: 0 },
    albums: [],
    error: null,
    origin: "sweep",
    set_aside: 0,
    elapsed_seconds: 0,
    // A sweep is unattended by definition — it never blocks on a person.
    awaiting_decision: false,
    stopped: false,
    aborted: false,
    sweep: {
      processed: 0,
      auto_applied: 0,
      banked: 0,
      skipped_known: 0,
      current_folder: null,
      stopped: false,
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
 * against the concept module's own render.
 *
 * It is an IDENTITY check and never weight coverage: this renders the concept
 * bare, and the page under test mounts no `IconContext` either, so both sides
 * take Phosphor's own `regular` — a weight the app never ships. The comparison
 * holds because the concepts it distinguishes differ at every weight. Only
 * `icons.test.ts` mounts `ICON_WEIGHT` and can say anything about weight. */
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

  test("the path field carries the contract's own length bound", () => {
    // `StartImportRequest.path` is max_length=4096. Past it the server answers
    // FastAPI's array-shaped 422, which this page deliberately does NOT show
    // (it is machine copy) — so an overlong paste got a generic refusal that
    // never says the word "long". The field refuses it instead.
    renderAt("/import");
    expect(screen.getByLabelText("Folder path")).toHaveAttribute(
      "maxlength",
      "4096",
    );
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

  test("while a start is in flight Start is aria-disabled, not disabled, and a re-submit posts nothing", async () => {
    // Same rule as the Pause button: the submit button holds focus when it is
    // pressed, so disabling it on that commit strands keyboard focus on <body>.
    // The blank-path and running-import gates stay real `disabled` — those are
    // reasons the control cannot be used at all (pinned by their own tests).
    let posts = 0;
    server.use(
      http.post(IMPORT_URL, async () => {
        posts += 1;
        await delay("infinite");
        return HttpResponse.json({ job_id: "job-1" }, { status: 202 });
      }),
    );
    const user = userEvent.setup();
    renderAt("/import");

    await user.type(screen.getByLabelText("Folder path"), "/music/incoming");
    await user.click(screen.getByRole("button", { name: /start import/i }));

    const button = await screen.findByRole("button", { name: /starting/i });
    expect(button).toHaveAttribute("aria-disabled", "true");
    expect(button).not.toBeDisabled();

    // The form still submits while aria-disabled — the handler swallows it.
    await user.click(button);
    expect(posts).toBe(1);
  });

  test("a 409 with no resumable import carries the server's own reason", async () => {
    // The swap-lock case: no import owns the slot (probe idle -> Start enabled),
    // but a config Apply / duplicate resolve / backfill holds it, so POST
    // /api/import 409s. Three causes share the status, so the screen's own
    // "a library operation is in progress. Try again in a moment." was vague
    // where the server was specific — and "in a moment" is a length only the
    // library knows. The message must not claim a resumable import either.
    server.use(
      http.post(IMPORT_URL, () =>
        HttpResponse.json(
          { detail: "A library backfill is in progress; import available when it finishes" },
          { status: 409 },
        ),
      ),
    );
    const user = userEvent.setup();
    renderAt("/import");

    await user.type(screen.getByLabelText("Folder path"), "/music/incoming");
    await user.click(screen.getByRole("button", { name: /start import/i }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(
      "A library backfill is in progress; import available when it finishes.",
    );
    // These sentences carry repr'd paths, which Chromium will not break at `/`.
    expect(alert).toHaveClass("break-words");
    expect(screen.queryByText(/try again in a moment/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/use resume above/i)).not.toBeInTheDocument();
    // Start keeps focus through a failure (only `aria-disabled` while pending),
    // so the sentence is its description on the way back to it.
    expect(
      screen.getByRole("button", { name: /start import/i }),
    ).toHaveAttribute("aria-describedby", alert.id);
    expect(alert.id).not.toBe("");
    // Still on the entry screen (no ?job=, so the input is still shown).
    expect(screen.getByLabelText("Folder path")).toBeInTheDocument();
  });

  test("a 503 layout refusal reaches the entry screen verbatim", async () => {
    // The main start surface was the last one still throwing this reason away
    // for "Check the path and the backend, then try again" — advice that cannot
    // work: nothing changes until the store layout does.
    server.use(
      http.post(IMPORT_URL, () =>
        HttpResponse.json(
          { detail: "the music folder is not mounted; imports are refused" },
          { status: 503 },
        ),
      ),
    );
    const user = userEvent.setup();
    renderAt("/import");

    await user.type(screen.getByLabelText("Folder path"), "/music/incoming");
    await user.click(screen.getByRole("button", { name: /start import/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "the music folder is not mounted; imports are refused.",
    );
    expect(screen.queryByText(/check the path and the backend/i)).not.toBeInTheDocument();
  });

  test("a transport failure still takes the screen's generic sentence", async () => {
    // The carried-sentence arms must not swallow the case they were added
    // beside: a bodyless 500 has no reason to carry.
    server.use(
      http.post(IMPORT_URL, () => new HttpResponse(null, { status: 500 })),
    );
    const user = userEvent.setup();
    renderAt("/import");

    await user.type(screen.getByLabelText("Folder path"), "/music/incoming");
    await user.click(screen.getByRole("button", { name: /start import/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Couldn’t start the import. Check the path and the backend, then try again.",
    );
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

  test("an inbox run's banner claims no banking and no count", async () => {
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
      // Review all and a per-row Review also start inbox runs, and those
      // wait on the run page instead of banking.
      await screen.findByText("An inbox import is running."),
    ).toBeInTheDocument();
    expect(screen.queryByText(/set aside/i)).not.toBeInTheDocument();
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
    // And THIS 409 keeps the screen's own sentence rather than the server's:
    // it is the one case where the screen can name a control it has.
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "An import is already running; use Resume above.",
    );
  });

  test("editing the path clears the refusal it was about", async () => {
    // `start.error` survives until the next mutate, so the field stayed red and
    // the stale sentence stayed wired into Start's aria-describedby while the
    // user typed the correction.
    server.use(
      http.post(IMPORT_URL, () =>
        HttpResponse.json(
          { detail: "That folder doesn’t exist." },
          { status: 422 },
        ),
      ),
    );
    const user = userEvent.setup();
    renderAt("/import");

    const field = screen.getByLabelText("Folder path");
    await user.type(field, "/music/incmoing");
    await user.click(screen.getByRole("button", { name: /start import/i }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("That folder doesn’t exist.");
    expect(field).toHaveAttribute("aria-invalid", "true");
    expect(
      screen.getByRole("button", { name: /start import/i }),
    ).toHaveAttribute("aria-describedby", alert.id);

    await user.type(field, "x");

    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(field).toHaveAttribute("aria-invalid", "false");
    expect(
      screen.getByRole("button", { name: /start import/i }),
    ).not.toHaveAttribute("aria-describedby");
  });

  test("a browsed folder fills the path box, clears a stale refusal and takes focus", async () => {
    // Use goes through the page's own setter, so it clears what typing clears.
    server.use(
      http.post(IMPORT_URL, () =>
        HttpResponse.json(
          { detail: "That folder doesn’t exist." },
          { status: 422 },
        ),
      ),
      http.get(`${window.location.origin}/api/folders`, () =>
        HttpResponse.json({
          path: "/media/downloads",
          parent: "/media",
          folders: [],
          total: 0,
          refusal: null,
        }),
      ),
    );
    const user = userEvent.setup();
    renderAt("/import");

    const field = screen.getByLabelText("Folder path");
    await user.type(field, "/media/downloads/gone");
    await user.click(screen.getByRole("button", { name: /start import/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "That folder doesn’t exist.",
    );

    await user.click(screen.getByRole("button", { name: "Browse folders" }));
    const dialog = await screen.findByRole("dialog", {
      name: "Choose a folder",
    });
    await within(dialog).findByText("No subfolders.");
    await user.click(
      within(dialog).getByRole("button", { name: "Use this folder" }),
    );

    await waitFor(() => expect(field).toHaveFocus());
    expect(field).toHaveValue("/media/downloads");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(field).toHaveAttribute("aria-invalid", "false");
  });

  test("a 409 shows its sentence without reddening the path field", async () => {
    // The path is fine — the library is busy. `aria-invalid` on this field says
    // "what you typed is wrong", which sends the user off to edit the one thing
    // that was not the problem.
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

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "A library operation is in progress.",
    );
    expect(screen.getByLabelText("Folder path")).toHaveAttribute(
      "aria-invalid",
      "false",
    );
  });
});

describe("ImportPage — what happens to the files", () => {
  beforeEach(() => {
    server.use(
      http.get(ACTIVE_URL, () =>
        HttpResponse.json({ active: false, job_id: null }),
      ),
    );
  });

  const field = () => screen.getByLabelText("Folder path");

  test.each<[string, string]>([
    ["move", "Files move into your library."],
    ["hardlink", "Files stay, hardlinked into your library."],
    ["copy", "Files stay, copied into your library."],
    ["link", "Files stay, symlinked into your library."],
    ["reflink", "Files stay, cloned into your library."],
    ["reflink_auto", "Files stay, cloned into your library."],
    ["in_place", "Files stay where they are."],
  ])("%s: the line under the path box, read with it", async (op, line) => {
    server.use(
      http.get(IMPORT_OP_URL, () => HttpResponse.json({ operation: op })),
    );
    renderAt("/import");
    const change = await screen.findByRole("link", { name: "Change" });
    expect(change).toHaveAttribute("href", "/settings/beets");
    expect(change.closest("p")?.textContent).toBe(`${line} Change`);
    // The box is described by the sentence alone, not by the link's word.
    expect(field()).toHaveAccessibleDescription(line);
  });

  test("no line and no description while the setting loads", async () => {
    let reads = 0;
    server.use(
      http.get(IMPORT_OP_URL, () => {
        reads += 1;
        return new Promise<Response>(() => {});
      }),
    );
    renderAt("/import");
    await waitFor(() => expect(reads).toBe(1));
    expect(screen.queryByRole("link", { name: "Change" })).toBeNull();
    expect(screen.queryByText(/^Files /)).toBeNull();
    expect(field()).not.toHaveAttribute("aria-describedby");
  });

  test("no line and no description when the setting can't be read", async () => {
    let reads = 0;
    server.use(
      http.get(IMPORT_OP_URL, () => {
        reads += 1;
        return HttpResponse.json({ detail: "boom" }, { status: 500 });
      }),
    );
    renderAt("/import");
    await waitFor(() => expect(reads).toBeGreaterThan(0));
    // Let the failed read settle before looking for the line.
    await new Promise((r) => setTimeout(r, 50));
    expect(screen.queryByRole("link", { name: "Change" })).toBeNull();
    expect(screen.queryByText(/^Files /)).toBeNull();
    expect(field()).not.toHaveAttribute("aria-describedby");
  });

  test("the placeholder is a folder outside a default library", () => {
    renderAt("/import");
    expect(field()).toHaveAttribute(
      "placeholder",
      "/media/downloads/Artist - Album",
    );
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
            progress: { applied: 2, needs_review: 1, skipped: 0, not_landed: 0, already_known: 0 },
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
            progress: { applied: 2, needs_review: 0, skipped: 0, not_landed: 0, already_known: 0 },
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
            progress: { applied: 2, needs_review: 1, skipped: 0, not_landed: 0, already_known: 0 },
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
            progress: { applied: 0, needs_review: 0, skipped: 0, not_landed: 0, already_known: 0 },
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

  // decisions 39, extended to this feed by the owner (2026-09-11), at the
  // owner's 28rem — one threshold on all three rows. Under 28rem
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
    // The template too: without it the `col-start` pins still resolve into
    // implicit columns, and the text track loses `minmax(0,1fr)` — it
    // auto-sizes and the title stops truncating, which is the defect.
    expect(wrapper?.className.split(/\s+/)).toContain("grid-cols-[minmax(0,1fr)_auto]");
    for (const token of [
      "col-start-1",
      "row-start-2",
      "@min-[28rem]/feedrow:col-start-2",
      "@min-[28rem]/feedrow:row-start-1",
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
      "@min-[28rem]/feedrow:-ml-1",
      "@min-[28rem]/feedrow:col-start-2",
      "@min-[28rem]/feedrow:mb-0",
      "@min-[28rem]/feedrow:mr-4",
      "@min-[28rem]/feedrow:row-start-1",
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
    // Guarded, so a structural change fails as an assertion rather than a
    // TypeError inside the helper below.
    expect(wrapper).not.toBeNull();
    expect(wrapper?.className.split(/\s+/)).not.toContain("grid");
    expect(wrapper?.className.split(/\s+/)).not.toContain("@container/feedrow");
    expect(containerQueryVariants(wrapper as HTMLElement)).toEqual([
      "@min-[18rem]/rowtext:block",
      "@min-[18rem]/rowtext:flex-row",
      "@min-[18rem]/rowtext:items-center",
    ]);
  });

  // A Replace the user asked for that imported NOTHING. The backend attaches
  // its reason to the row's `note` WITHOUT touching the row's status
  // (`registry._drain_outcomes_locked`), and separately flags it `did_not_land` in
  // every phase — so the badge classifies and the note explains, from the
  // moment of the refusal rather than only once the job ends.
  const REFUSED_NOTE =
    "Replace moved 1 of 2 old copies to Trash, then failed. Nothing was imported.";

  /** The decided row that note belongs to. `note` is left OFF by default so the
   * absence case below is the same row minus one field.
   *
   * Counting rule every fixture here keeps: a noted row is counted ONCE, in
   * `progress.not_landed`, with `did_not_land: true` and `set_aside` untouched —
   * true on an attended run and on a bank-apply one, where the row wears
   * `needs_dup_resolution` and is still not set aside. */
  function refusedRow(
    overrides: Partial<ImportAlbumSummary> = {},
  ): ImportAlbumSummary {
    return {
      index: 0,
      folder: "/music/incoming/Radiohead - OK Computer",
      artist: "Radiohead",
      album: "OK Computer",
      recommendation: "strong",
      confidence: 99,
      status: "decided",
      album_id: null,
      did_not_land: false,
      ...overrides,
    };
  }

  /** A second, ordinary row. A one-row feed cannot tell "the note is on the
   * refused row" from "the note is somewhere on the page". */
  const LANDED_ROW: ImportAlbumSummary = {
    index: 1,
    folder: "/music/incoming/Radiohead - Kid A",
    artist: "Radiohead",
    album: "Kid A",
    recommendation: "strong",
    confidence: 98,
    status: "applied",
    album_id: 42,
    did_not_land: false,
  };

  test.each([
    // The contract is phase-independent, so the row must read the same in the
    // live view and in the finished one — two different panels render the feed.
    ["while the job runs", "reviewing"],
    ["once the job is terminal", "done"],
  ] as const)(
    "a refused Replace says so on its own row %s",
    async (_case, phase) => {
      server.use(
        http.get(JOB_URL, () =>
          HttpResponse.json(
            makeJob({
              phase,
              progress: {
                applied: 1,
                needs_review: 0,
                skipped: 0,
                not_landed: 1,
                already_known: 0,
              },
              albums: [
                refusedRow({ note: REFUSED_NOTE, did_not_land: true }),
                LANDED_ROW,
              ],
            }),
          ),
        ),
      );
      renderAt("/import?job=job-1");

      // The backend's whole sentence, rendered verbatim — the FE never branches
      // on which of the five reasons arrived.
      const text = await screen.findByText(REFUSED_NOTE);
      const line = text.closest("p");
      expect(line).not.toBeNull();
      // ON the refused row: the note's own <li> holds that album and not the
      // other one. A one-row fixture cannot fail this.
      const row = text.closest("li");
      expect(row).toHaveTextContent("OK Computer");
      expect(row).not.toHaveTextContent("Kid A");
      // The badge classifies what the note explains, in BOTH phases.
      expect(row).toHaveTextContent("Didn’t land");
      // In reading order with the row, and a grid item of the row WRAPPER —
      // from inside AlbumRow's box a second line re-centres every `items-center`
      // sibling beside it.
      const wrapper = row?.firstElementChild;
      expect(line?.parentElement).toBe(wrapper);
      expect(wrapper?.firstElementChild?.nextElementSibling).toBe(line);
      expect(wrapper?.className.split(/\s+/)).toContain("grid");
      expect(wrapper?.className.split(/\s+/)).toContain(
        "grid-cols-[minmax(0,1fr)_auto]",
      );
      // Both axes: CSS grid places definite-position items BEFORE auto-placed
      // ones, so without the column the note takes (row 1, col 1) and shoves the
      // row body a track right. `col-span-full` so the line may also run under
      // the action's column in the wide arm.
      for (const token of ["col-span-full", "row-start-2"]) {
        expect(line?.className.split(/\s+/)).toContain(token);
      }
      // A long unbreakable token must WRAP, not be clipped by the list's
      // `overflow-hidden`; `min-w-0` alone only stops the page panning.
      expect(text.className.split(/\s+/)).toEqual(
        expect.arrayContaining(["min-w-0", "break-words"]),
      );
      // Not colour alone: the glyph is decorative and amber, and the line's
      // whole text IS the sentence — no run-in label, nothing the colour has to
      // carry. Equality, not `toHaveTextContent`: `line` was found by walking up
      // from that very text, so a containment check cannot fail.
      const glyph = line?.querySelector("svg");
      expect(glyph).toHaveAttribute("aria-hidden", "true");
      expect(glyph?.getAttribute("class")?.split(/\s+/)).toContain("text-warning");
      expect(line?.textContent).toBe(REFUSED_NOTE);
    },
  );

  test("a row with no note gains no line, and keeps the plain box", async () => {
    // The same decided row minus the note. Asserting only "the sentence is
    // absent" would pass on an empty line rendered unconditionally, so the
    // oracle is the row's shape: AlbumRow and nothing else.
    server.use(
      http.get(JOB_URL, () =>
        HttpResponse.json(
          makeJob({
            progress: {
              applied: 0,
              needs_review: 0,
              skipped: 0,
              not_landed: 0,
              already_known: 0,
            },
            albums: [refusedRow()],
          }),
        ),
      ),
    );
    renderAt("/import?job=job-1");

    expect(await screen.findByText("Decided")).toBeInTheDocument();
    expect(screen.queryByText(REFUSED_NOTE)).toBeNull();
    const wrapper = screen.getByRole("listitem").firstElementChild;
    expect(wrapper?.children).toHaveLength(1);
    expect(wrapper?.className.split(/\s+/)).not.toContain("grid");
    expect(wrapper?.className.split(/\s+/)).not.toContain("@container/feedrow");
  });

  test("a noted row that still has a button stacks them, never sharing a cell", async () => {
    // `_drain_outcomes_locked` attaches a note without touching the status, and
    // on a directive run that status is `needs_dup_resolution` — for which
    // `feedRowAction` returns Resolve. Two explicitly placed grid items in one
    // area paint over each other rather than stacking, so the narrow arm must
    // give them different rows. (`bank_apply` feeds are read-only, so no origin
    // produces this pairing today; the layout must survive it anyway.)
    server.use(
      http.get(JOB_URL, () =>
        HttpResponse.json(
          makeJob({
            progress: {
              applied: 0,
              needs_review: 0,
              skipped: 0,
              not_landed: 1,
              already_known: 0,
            },
            albums: [
              refusedRow({
                status: "needs_dup_resolution",
                note: REFUSED_NOTE,
                did_not_land: true,
              }),
            ],
          }),
        ),
      ),
    );
    renderAt("/import?job=job-1");

    const text = await screen.findByText(REFUSED_NOTE);
    const line = text.closest("p");
    const group = screen.getByRole("link", { name: /resolve/i }).parentElement;
    const wrapper = text.closest("li")?.firstElementChild;
    expect(line?.parentElement).toBe(wrapper);
    expect(group?.parentElement).toBe(wrapper);
    // Row, then note, then action — the order a screen reader walks and the
    // order the rows paint in.
    expect(wrapper?.firstElementChild?.nextElementSibling).toBe(line);
    expect(line?.nextElementSibling).toBe(group);
    // The narrow arm is where they used to collide; they must not name the same
    // row there.
    const rowStart = (el: Element | null | undefined) =>
      el?.className.split(/\s+/).filter((c) => c.startsWith("row-start-")) ?? [];
    expect(rowStart(line)).toEqual(["row-start-2"]);
    expect(rowStart(group)).toEqual(["row-start-3"]);
    // The wide arm is untouched: the action returns to row 1, column 2.
    expect(group?.className.split(/\s+/)).toEqual(
      expect.arrayContaining([
        "@min-[28rem]/feedrow:col-start-2",
        "@min-[28rem]/feedrow:row-start-1",
      ]),
    );
  });

  /** A finished/running bank apply whose one row is a refused Replace. The
   * second run mode: the row wears `needs_dup_resolution` AND the server's
   * `did_not_land`, which is the pair every FE-derived count has to respect. */
  function bankApplyJob(phase: ImportJobState["phase"]): ImportJobState {
    return makeJob({
      origin: "bank_apply",
      phase,
      progress: {
        applied: 0,
        needs_review: 0,
        skipped: 0,
        not_landed: 1,
        already_known: 0,
      },
      albums: [
        refusedRow({
          status: "needs_dup_resolution",
          note: REFUSED_NOTE,
          did_not_land: true,
        }),
      ],
    });
  }

  test.each([
    // `feedIsReadOnly` is passed at TWO call sites and the default phase only
    // reaches the live one; the finished panel is the one a user sees longest.
    ["while the job runs", "applying"],
    ["once the job is terminal", "done"],
  ] as const)(
    "a bank-apply feed offers no decision button %s — nothing would consume it",
    async (_case, phase) => {
      // A directive run answers every hook from the banked decision and never
      // parks (`_directive_choice`), so a Resolve here posts into the void. The
      // row keeps its badge and its note.
      server.use(http.get(JOB_URL, () => HttpResponse.json(bankApplyJob(phase))));
      renderAt("/import?job=job-1");

      expect(await screen.findByText(REFUSED_NOTE)).toBeInTheDocument();
      expect(screen.getByText("Didn’t land")).toBeInTheDocument();
      expect(screen.queryByRole("link", { name: /resolve/i })).toBeNull();
      expect(screen.queryByRole("link", { name: /^review$/i })).toBeNull();
      // And with no control on the row, nothing may claim the row needs one.
      const wrapper = screen.getByRole("listitem").firstElementChild;
      expect(wrapper?.className.split(/\s+/)).not.toContain("bg-primary/5");
    },
  );

  test("a bank-apply run counts its refused album once, not once per surface", async () => {
    // The FE derives the duplicate count itself (the backend `progress` has no
    // duplicate counter), so the server's "counted nowhere else" rule does not
    // reach it: on status alone this row was BOTH "1 didn’t land" and "1 already
    // in library" — the second naming a resolution this read-only feed cannot
    // offer. The flag decides, never the note.
    server.use(
      http.get(JOB_URL, () => HttpResponse.json(bankApplyJob("applying"))),
    );
    renderAt("/import?job=job-1");

    const headline = await screen.findByText("0 albums imported · 1 didn’t land");
    expect(headline.textContent).not.toContain("already in library");
  });

  test("an UN-noted duplicate still earns its cue on the same feed", async () => {
    // The control for the test above: the exclusion is the server's flag, not
    // the status, so a real pending duplicate keeps its clause.
    server.use(
      http.get(JOB_URL, () =>
        HttpResponse.json(
          makeJob({
            origin: "bank_apply",
            phase: "applying",
            progress: {
              applied: 0,
              needs_review: 0,
              skipped: 0,
              not_landed: 0,
              already_known: 0,
            },
            albums: [refusedRow({ status: "needs_dup_resolution" })],
          }),
        ),
      ),
    );
    renderAt("/import?job=job-1");

    expect(
      await screen.findByText("0 albums imported · 1 already in library"),
    ).toBeInTheDocument();
  });

  test("a finished bank apply that owes a decision points at the Review page", async () => {
    // "…Decide again." is a bank-apply-only note, and this feed is read-only:
    // the decision lives on /review's bank row. Without this the panel's only
    // link was the shell's "Start over" → /import, a folder import.
    server.use(http.get(JOB_URL, () => HttpResponse.json(bankApplyJob("done"))));
    renderAt("/import?job=job-1");

    const cta = await screen.findByRole("link", { name: "Review banked albums" });
    expect(cta).toHaveAttribute("href", "/review");
    // The control for the stopped arm in "stop this run": an apply that ran to
    // the end keeps the solid CTA, so the outline there is about the stop.
    expect(cta).toHaveAttribute("data-variant", "default");
  });

  test("a finished bank apply that landed everything gains no Review link", async () => {
    // Gated on what the run left behind, not on the origin: nothing to decide,
    // nothing to offer.
    server.use(
      http.get(JOB_URL, () =>
        HttpResponse.json(
          makeJob({
            origin: "bank_apply",
            phase: "done",
            progress: {
              applied: 1,
              needs_review: 0,
              skipped: 0,
              not_landed: 0,
              already_known: 0,
            },
            albums: [refusedRow({ status: "applied", album_id: 41 })],
          }),
        ),
      ),
    );
    renderAt("/import?job=job-1");

    expect(await screen.findByText("Import finished")).toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: "Review banked albums" }),
    ).toBeNull();
  });

  test("mid-run, the headline owns the album that didn’t land", async () => {
    // The server drops a refused Replace out of `applied` and into `not_landed`
    // the moment it happens. Without a clause of its own the album left the
    // line entirely — the headline read "0 albums imported" over a row saying
    // "Nothing was imported." The spoken twin is pinned in importStatus.test.ts
    // (mid-run the announcer is behind a 4s throttle this test cannot outwait).
    server.use(
      http.get(JOB_URL, () =>
        HttpResponse.json(
          makeJob({
            phase: "reviewing",
            progress: {
              applied: 1,
              needs_review: 0,
              skipped: 2,
              not_landed: 1,
              already_known: 0,
            },
            albums: [
              refusedRow({ note: REFUSED_NOTE, did_not_land: true }),
              LANDED_ROW,
            ],
          }),
        ),
      ),
    );
    renderAt("/import?job=job-1");

    // Same words as the two terminal panels, in the page's middot dialect.
    const headline = await screen.findByText(
      "1 album imported · 2 skipped · 1 didn’t land",
    );
    // EVERY clause is unbreakable, not only the one this slice added: the line
    // may wrap between segments and nowhere else. Same oracle as the finished
    // panel's twin further down — N segments, N-1 ordinary spaces.
    // `textContent`, not the matcher: RTL's normaliser folds NBSP to a space,
    // which is why the query above reads naturally and cannot see this.
    const raw = lineOf(headline).textContent ?? "";
    expect(raw.split(" ")).toHaveLength(3);
    expect(raw).toContain("1\u00a0didn’t\u00a0land");
  });

  test("the live cue surfaces a parked duplicate to resolve", async () => {
    server.use(
      http.get(JOB_URL, () =>
        HttpResponse.json(
          makeJob({
            progress: { applied: 0, needs_review: 0, skipped: 0, not_landed: 0, already_known: 0 },
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
            progress: { applied: 0, needs_review: 0, skipped: 0, not_landed: 0, already_known: 0 },
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
            progress: { applied: 0, needs_review: 0, skipped: 0, not_landed: 0, already_known: 0 },
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
            progress: { applied: 0, needs_review: 0, skipped: 0, not_landed: 0, already_known: 0 },
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
            progress: { applied: 1, needs_review: 0, skipped: 0, not_landed: 0, already_known: 0 },
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
      lineOf(screen.getByText(/an inbox import is running/i)),
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
            progress: { applied: 0, needs_review: 0, skipped: 0, not_landed: 0, already_known: 0 },
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
              progress: { applied: 1, needs_review: 0, skipped: 1, not_landed: 0, already_known: 0 },
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
            progress: { applied: 1, needs_review: 1, skipped: 1, not_landed: 0, already_known: 0 },
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
            progress: { applied: 2, needs_review: 0, skipped: 1, not_landed: 0, already_known: 0 },
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
            progress: { applied: 2, needs_review: 0, skipped: 1, not_landed: 0, already_known: 0 },
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
            progress: { applied: 1, needs_review: 0, skipped: 1, not_landed: 0, already_known: 0 },
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

  test("a row that never landed shows a Didn’t-land badge and the done body counts it", async () => {
    // A decided/applied row whose library album id never arrived on a terminal
    // job carries did_not_land; progress.not_landed mirrors the count. The row
    // must flag the failure and the done body must own up to it.
    server.use(
      http.get(JOB_URL, () =>
        HttpResponse.json(
          makeJob({
            phase: "done",
            progress: { applied: 0, needs_review: 0, skipped: 0, not_landed: 1, already_known: 0 },
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

    // The row badge names the failure (its own element, exact text). The
    // apostrophe is the page's typographic one, like the counts line below it.
    expect(await screen.findByText("Didn’t land")).toBeInTheDocument();
    // The finished body appends the count...
    expect(
      screen.getByText("0 albums imported · 0 skipped · 1 didn’t land"),
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
            progress: { applied: 1, needs_review: 0, skipped: 0, not_landed: 0, already_known: 0 },
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
            progress: { applied: 1, needs_review: 0, skipped: 0, not_landed: 0, already_known: 0 },
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

  test("failed shows the error message + one way on, and no stop control", async () => {
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
    // ONE CTA now. The chrome's ghost "Start over" is gone from every run view:
    // the header slot carries the stop while a run is going and nothing once it
    // is over, and this panel already owns the way on. One label for /import
    // across the three terminal panels — the nav item's and the h1's own words.
    const another = screen.getByRole("link", { name: "Add from folder" });
    expect(another).toHaveAttribute("href", "/import");
    expect(
      screen.queryByRole("link", { name: /import another folder/i }),
    ).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /start over/i })).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /stop this run/i }),
    ).not.toBeInTheDocument();
  });

  test("a two-line job error keeps its line break", async () => {
    const error =
      "Hardlinks can't cross filesystems.\nError linking file: [Errno 18]";
    server.use(
      http.get(JOB_URL, () =>
        HttpResponse.json(makeJob({ phase: "failed", error, albums: [] })),
      ),
    );
    renderAt("/import?job=job-1");

    await screen.findByText("Import failed");
    // jsdom lays nothing out: pin the text with its "\n" intact, on the node
    // whose `white-space` keeps it (an HTML <p> folds it into a space).
    const text = screen.getByText(/^Hardlinks can't cross filesystems\./);
    expect(text.textContent).toBe(error);
    expect(text).toHaveClass("whitespace-pre-line");
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
            progress: { applied: 200, needs_review: 1, skipped: 3, not_landed: 2, already_known: 0 },
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
      "200 albums imported · 3 skipped · 2 didn’t land",
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
      screen.getByRole("link", { name: "Add from folder" }),
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
            progress: { applied: 2, needs_review: 5, skipped: 0, not_landed: 0, already_known: 0 },
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
            progress: { applied: 2, needs_review: 0, skipped: 1, not_landed: 0, already_known: 0 },
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
            progress: { applied: 0, needs_review: 0, skipped: 0, not_landed: 5, already_known: 0 },
          }),
        ),
      ),
    );
    renderAt("/import?job=job-1");

    expect(await screen.findByText("Import failed")).toBeInTheDocument();
    expect(screen.getByText("5 didn’t land")).toBeInTheDocument();
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
            progress: { applied: 0, needs_review: 0, skipped: 0, not_landed: 0, already_known: 0 },
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
            progress: { applied: 1, needs_review: 1, skipped: 0, not_landed: 0, already_known: 0 },
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
              stopped: false,
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
    // Distinct from the transient error: offers a fresh start, with no Retry —
    // under the same label the other two terminal panels use for /import, which
    // the sentence above it names.
    expect(
      screen.getByRole("link", { name: "Add from folder" }),
    ).toHaveAttribute("href", "/import");
    expect(
      screen.getByText(/Add the folder again to continue\./),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /retry/i }),
    ).not.toBeInTheDocument();
  });
});

describe("ImportPage — stop this run", () => {
  const STOP_URL = `${JOB_URL}/stop`;

  /** An active manual run, parked on the album the user wants to walk away
   * from — the owner's case ("i dont want to go throught the whole review"). */
  function parkedJob(overrides: Partial<ImportJobState> = {}) {
    return makeJob({ phase: "reviewing", awaiting_decision: true, ...overrides });
  }

  test("an active manual run offers the stop, and pressing it posts /stop", async () => {
    let posts = 0;
    server.use(
      http.get(JOB_URL, () => HttpResponse.json(parkedJob())),
      http.post(STOP_URL, () => {
        posts += 1;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const user = userEvent.setup();
    renderAt("/import?job=job-1");

    const stop = await screen.findByRole("button", { name: "Stop this run" });
    // The link it replaces is gone — the owner saw no value in it here.
    expect(
      screen.queryByRole("link", { name: /start over/i }),
    ).not.toBeInTheDocument();

    await user.click(stop);
    await waitFor(() => expect(posts).toBe(1));
  });

  test("pending reads Stopping… and is aria-disabled, never disabled", async () => {
    server.use(
      http.get(JOB_URL, () => HttpResponse.json(parkedJob())),
      // Held open: the button's OWN pending state is the subject here, with the
      // job state unchanged, so the two terms behind the label are separable.
      http.post(STOP_URL, async () => {
        await delay("infinite");
      }),
    );
    const user = userEvent.setup();
    renderAt("/import?job=job-1");

    const stop = await screen.findByRole("button", { name: "Stop this run" });
    await user.click(stop);

    await waitFor(() => expect(stop).toHaveTextContent("Stopping…"));
    expect(stop).toHaveAttribute("aria-disabled", "true");
    // THE oracle, and the reason for the posture: `disabled` on the click's own
    // commit strands keyboard focus on <body>. `disabled:` never matches an
    // aria-disabled control, so the dimming has to be spelled out too.
    expect(stop).not.toBeDisabled();
    expect(stop).toHaveClass("aria-disabled:opacity-50");
    // The app's stop register, not the navigation one: every other Stop/Pause
    // button is outline, including the sweep's Pause in this file on this
    // mutation. `ghost sm` here is links.
    expect(stop).toHaveAttribute("data-variant", "outline");
  });

  test("a repeat click while pending is swallowed", async () => {
    let posts = 0;
    server.use(
      http.get(JOB_URL, () => HttpResponse.json(parkedJob())),
      http.post(STOP_URL, async () => {
        posts += 1;
        await delay("infinite");
      }),
    );
    const user = userEvent.setup();
    renderAt("/import?job=job-1");

    const stop = await screen.findByRole("button", { name: "Stop this run" });
    await user.click(stop);
    await waitFor(() => expect(stop).toHaveTextContent("Stopping…"));
    await user.click(stop);
    await user.click(stop);

    expect(posts).toBe(1);
  });

  test("a reload mid-stop reads pending from the job, with nothing clicked", async () => {
    let posts = 0;
    server.use(
      // `stopped` is true and the phase is still active: the stop was accepted
      // in another tab (or before this reload) and the worker has not unwound
      // yet. Nothing on THIS page ever entered a pending mutation.
      http.get(JOB_URL, () => HttpResponse.json(parkedJob({ stopped: true }))),
      http.post(STOP_URL, () => {
        posts += 1;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const user = userEvent.setup();
    renderAt("/import?job=job-1");

    const stop = await screen.findByRole("button", { name: "Stopping…" });
    expect(stop).toHaveAttribute("aria-disabled", "true");
    await user.click(stop);
    expect(posts).toBe(0);
  });

  // Both paths to `isError`, because they are different mechanisms: a 500
  // reaches it through the hook's own `throw`, a dead connection through fetch's
  // rejection before any status exists to read. The 404/409 arm swallows two
  // statuses, so "the request did not happen at all" has to be shown not to fall
  // into it.
  test.each([
    { how: "a server error", stop: () => new HttpResponse(null, { status: 500 }) },
    { how: "a transport failure", stop: () => HttpResponse.error() },
  ])("$how says so and leaves the button pressable", async ({ stop: reply }) => {
    server.use(
      http.get(JOB_URL, () => HttpResponse.json(parkedJob())),
      http.post(STOP_URL, reply),
    );
    const user = userEvent.setup();
    renderAt("/import?job=job-1");

    await user.click(await screen.findByRole("button", { name: "Stop this run" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Couldn’t stop. Try again.");
    // Stacked above the button, the recipe SweepRun uses for the same sentence
    // beside the same mutation — not inline-left of it, which made the header's
    // shrink-0 slot as wide as sentence + gap + button.
    expect(alert.parentElement).toHaveClass("flex", "flex-col");
    expect(alert.nextElementSibling).toBe(
      screen.getByRole("button", { name: "Stop this run" }),
    );
    // Not latched: the run is still going, so the control must still work.
    expect(
      screen.getByRole("button", { name: "Stop this run" }),
    ).toHaveAttribute("aria-disabled", "false");
  });

  test("a finished run offers no stop — there is nothing left to end", async () => {
    server.use(
      http.get(JOB_URL, () => HttpResponse.json(makeJob({ phase: "done" }))),
    );
    renderAt("/import?job=job-1");

    expect(await screen.findByText("Import finished")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /stop this run/i }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: /start over/i }),
    ).not.toBeInTheDocument();
  });

  test("a sweep run keeps Pause sweep and grows no second stop control", async () => {
    server.use(http.get(SWEEP_JOB_URL, () => HttpResponse.json(sweepJob())));
    renderAt("/import?job=s1");

    expect(
      await screen.findByRole("button", { name: /pause sweep/i }),
    ).toBeInTheDocument();
    // One mechanism, one control: the sweep's Pause posts the same /stop.
    expect(
      screen.queryByRole("button", { name: /stop this run/i }),
    ).not.toBeInTheDocument();
  });

  test("a stopped run's done view says so, and offers no action it cannot carry out", async () => {
    server.use(
      http.get(JOB_URL, () =>
        HttpResponse.json(
          makeJob({
            phase: "done",
            stopped: true,
            aborted: true,
            // The album the run stopped on stays `needs_review` on the server
            // (it was never decided and its files never moved), so it counts as
            // set aside.
            set_aside: 1,
            elapsed_seconds: 95,
          }),
        ),
      ),
    );
    renderAt("/import?job=job-1");

    expect(await screen.findByText("Import stopped")).toBeInTheDocument();
    expect(screen.queryByText("Import finished")).not.toBeInTheDocument();
    // What a stop leaves, and the way past it — the counts above cannot say
    // either, because an album the run never reached has no row and no counter.
    expect(
      screen.getByText("The rest stayed in the folder. Add it again to continue."),
    ).toBeInTheDocument();
    // The sentence names a control this screen has.
    expect(
      screen.getByRole("link", { name: "Add from folder" }),
    ).toHaveAttribute("href", "/import");
    // ...and it is the ONLY sentence here: the two body lines are exclusive, so
    // a run cut short never also reads "Nothing was left to stop." over a
    // remedy telling the user the rest is still in the folder.
    expect(
      screen.queryByText("Nothing was left to stop."),
    ).not.toBeInTheDocument();
    // The row is still listed and still says it was never decided...
    expect(screen.getByText("Needs review")).toBeInTheDocument();
    // ...but the worker is gone, so the button that would post into the void
    // is not offered.
    expect(screen.queryByRole("link", { name: "Review" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Resolve" })).not.toBeInTheDocument();
    // A run the user ended is not a completion, so no success check.
    const glyph = screen
      .getByText("Import stopped")
      .closest("[data-slot='empty-state']")
      ?.querySelector("svg path");
    expect(glyph?.getAttribute("d")).toBe(pathOf(Stop));
    expect(glyph?.getAttribute("d")).not.toBe(pathOf(Success));
  });

  test("...and a stop that arrived after the last album reads as finished", async () => {
    // The window the second flag exists for: the press was accepted while the
    // last album was being placed, so it never reached an abort point and the
    // run ran out on its own. `stopped` alone titled this "Import stopped" and
    // sent the user back to a folder with nothing left in it.
    server.use(
      http.get(JOB_URL, () =>
        HttpResponse.json(
          makeJob({
            phase: "done",
            stopped: true,
            aborted: false,
            // Still live work, and the reason the feed must stay live here: a
            // row set aside mid-run (an unattended duplicate, a `search`
            // re-lookup) does not block the worker, so the run reached its own
            // end with that row still owed a decision.
            set_aside: 1,
          }),
        ),
      ),
    );
    renderAt("/import?job=job-1");

    expect(await screen.findByText("Import finished")).toBeInTheDocument();
    expect(screen.queryByText("Import stopped")).not.toBeInTheDocument();
    // The press, answered — and nothing else claimed about it.
    expect(screen.getByText("Nothing was left to stop.")).toBeInTheDocument();
    // No remedy: there is no rest, and no folder to send anyone back to.
    expect(
      screen.queryByText(/The rest stayed in the folder/),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: "Add from folder" }),
    ).not.toBeInTheDocument();
    // The run finished, so it wears the success check.
    const glyph = screen
      .getByText("Import finished")
      .closest("[data-slot='empty-state']")
      ?.querySelector("svg path");
    expect(glyph?.getAttribute("d")).toBe(pathOf(Success));
    expect(glyph?.getAttribute("d")).not.toBe(pathOf(Stop));
    // The feed stays live: the worker left by the front door, so the set-aside
    // row's decision screen still has a server to post to.
    expect(screen.getByRole("link", { name: "Review" })).toBeInTheDocument();
    // ...and this channel agrees with the panel beside it.
    expect(screen.getByRole("status")).toHaveTextContent(
      "Import complete. Imported 1, skipped 0.",
    );
  });

  // The stop is offered where the page can keep its promise. Both queue-driven
  // origins start the next item as soon as this job ends — the inbox drain and
  // the bank drain each pick up the next queued row — so "stop this run" would
  // leave the user reading "the rest stayed in the folder" while the next folder
  // was already importing in a job this page never shows. The API still accepts
  // a stop on any origin; this is the UI's own predicate.
  test.each(["inbox", "bank_apply"] as const)(
    "a %s run is not offered the stop — the queue picks up behind it",
    async (origin) => {
      server.use(
        http.get(JOB_URL, () =>
          HttpResponse.json(parkedJob({ origin, awaiting_decision: false })),
        ),
      );
      renderAt("/import?job=job-1");

      // The run view really rendered — otherwise the absence below is vacuous.
      expect(await screen.findByText("OK Computer")).toBeInTheDocument();
      expect(
        screen.queryByRole("button", { name: /stop this run/i }),
      ).not.toBeInTheDocument();
    },
  );

  // The control for the pair above, on the same active fixture: the exclusion
  // is the origin's, not something that removed the button everywhere.
  test("...and the manual run beside them still has it", async () => {
    server.use(http.get(JOB_URL, () => HttpResponse.json(parkedJob())));
    renderAt("/import?job=job-1");

    expect(
      await screen.findByRole("button", { name: "Stop this run" }),
    ).toBeInTheDocument();
  });

  test("while stopping, the run says so and the feed stops offering decisions", async () => {
    server.use(
      // Accepted, still unwinding: an ACTIVE phase with the flag set.
      http.get(JOB_URL, () => HttpResponse.json(parkedJob({ stopped: true }))),
    );
    renderAt("/import?job=job-1");

    // The header said the run was ending while the line under it counted on and
    // named the decision the run was parked on.
    //
    // Two visible nodes read "Stopping…" now — the button's own label and this
    // line — so the query names the element: StatusLine wraps its text in a
    // span, the Button holds its label as a direct text child.
    const line = await screen.findByText("Stopping…", { selector: "span" });
    expect(
      screen.queryByText(/album needs review/),
    ).not.toBeInTheDocument();
    // Spinning: the server accepted the stop and the worker is unwinding, and a
    // parked run has `working` false — the one moment the line must not look
    // idle.
    expect(spinnerOf(line)).toHaveClass("animate-spin");
    // ...and the row no longer offers a decision the released slot cannot take.
    expect(screen.getByText("Kid A")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Review" })).not.toBeInTheDocument();
  });

  // The control for the test above: the same run, unstopped, keeps its counting
  // line and its live button.
  test("...and an unstopped run keeps both", async () => {
    server.use(http.get(JOB_URL, () => HttpResponse.json(parkedJob())));
    renderAt("/import?job=job-1");

    expect(await screen.findByText(/1 album needs review/)).toBeInTheDocument();
    expect(
      screen.queryByText("Stopping…", { selector: "span" }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Review" })).toBeInTheDocument();
  });

  test("an accepted stop is announced at once, without waiting out the throttle", async () => {
    // The announcer is throttled to 4s so a 1s poll cannot spam it, and at mount
    // that window holds it on the "Loading the import." placeholder. Pressing
    // Stop is user-initiated — the person is owed an answer — so it bypasses the
    // window, as the sweep's Pause and the terminal states already do.
    server.use(
      http.get(JOB_URL, () => HttpResponse.json(parkedJob({ stopped: true }))),
    );
    renderAt("/import?job=job-1");

    // waitFor's default ceiling is 1000ms, a quarter of the throttle window, so
    // a pass cannot be the window simply elapsing. Anchored: the bypass is
    // sticky, so this is what the announcer holds for the rest of the run and
    // role="status" is atomic.
    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent(
        /^Stopping the import\.$/,
      ),
    );
  });

  test("...and the throttle is still in force for a run nobody stopped", async () => {
    // The control the test above needs: without it, a throttle holding nothing
    // back at all would produce the same pass.
    server.use(http.get(JOB_URL, () => HttpResponse.json(parkedJob())));
    renderAt("/import?job=job-1");

    expect(await screen.findByText(/1 album needs review/)).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Loading the import.");
  });

  test("when the run ends under the presser, focus lands on the heading", async () => {
    // The `aria-disabled` posture holds focus for the pending window; this is
    // its end. The button lives in the live branch only, so the poll that
    // reports `done` unmounts the element holding focus and the browser drops it
    // on <body> — the next Tab would restart at "Skip to content".
    let stopped = false;
    server.use(
      http.get(JOB_URL, () =>
        HttpResponse.json(
          stopped
            ? makeJob({ phase: "done", stopped: true, aborted: true, set_aside: 1 })
            : parkedJob(),
        ),
      ),
      http.post(STOP_URL, () => {
        stopped = true;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const user = userEvent.setup();
    renderAt("/import?job=job-1");

    const stop = await screen.findByRole("button", { name: "Stop this run" });
    stop.focus();
    expect(document.activeElement).toBe(stop);
    await user.click(stop);

    // The next poll takes the job terminal and the control goes with it.
    expect(await screen.findByText("Import stopped")).toBeInTheDocument();
    expect(stop.isConnected).toBe(false);
    // By identity, not `!== body`: the weak oracle passes against any patch
    // that happens to leave focus somewhere.
    expect(document.activeElement).toBe(
      screen.getByRole("heading", { level: 1, name: "Add from folder" }),
    );
  });

  test("...and never takes focus from something that holds it", async () => {
    // The control: the hand-off is for a focus the page DROPPED, never one
    // something live is holding. The holder is rendered OUTSIDE the page,
    // because a terminal transition unmounts the whole feed subtree — the
    // chrome survives it but carries no focusable of its own once the run is
    // over.
    let stopped = false;
    server.use(
      http.get(JOB_URL, () =>
        HttpResponse.json(
          stopped
            ? makeJob({ phase: "done", stopped: true, aborted: true, set_aside: 1 })
            : parkedJob(),
        ),
      ),
      http.post(STOP_URL, () => {
        stopped = true;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    renderWithProviders(
      <>
        <a href="/artists">Elsewhere</a>
        <ImportPage />
      </>,
      { route: "/import?job=job-1", path: "/import" },
    );

    // fireEvent, not userEvent: it does not move focus, so the stop is in flight
    // while the user tabs away.
    fireEvent.click(await screen.findByRole("button", { name: "Stop this run" }));
    const elsewhere = screen.getByRole("link", { name: "Elsewhere" });
    elsewhere.focus();

    expect(await screen.findByText("Import stopped")).toBeInTheDocument();
    expect(document.activeElement).toBe(elsewhere);
  });

  test("...and a run that ends on its own hands off without scrolling", async () => {
    // The trigger is wider than the presser: a reader with focus on <body>,
    // scrolled down a long feed, watching a run finish by itself gets the same
    // hand-off. `focus()` scrolls its target into view by default, so that
    // reader would be pulled to the top of the page at the moment the outcome
    // panel appears. jsdom does no layout, so the oracle is the option passed,
    // not a scroll position — the in-page convention (SearchPage,
    // ArtistAlbumsPage, BrowsePage) is `{ preventScroll: true }`.
    let finished = false;
    server.use(
      http.get(JOB_URL, () => {
        const body = finished
          ? makeJob({ phase: "done", set_aside: 1 })
          : // Working, not parked: this run polls at 1s, so the second read is
            // the end. `stopped` stays false — nobody pressed anything.
            parkedJob({ awaiting_decision: false });
        finished = true;
        return HttpResponse.json(body);
      }),
    );
    renderAt("/import?job=job-1");

    // The same element before and after: ImportRun is mounted without a key, so
    // a terminal poll reconciles the chrome in place and only the panel below
    // it remounts.
    const heading = await screen.findByRole("heading", {
      level: 1,
      name: "Add from folder",
    });
    const focus = vi.spyOn(heading, "focus");
    expect(document.activeElement).toBe(document.body);

    expect(
      await screen.findByText("Import finished", undefined, { timeout: 3000 }),
    ).toBeInTheDocument();
    expect(document.activeElement).toBe(heading);
    expect(focus).toHaveBeenCalledWith({ preventScroll: true });
    focus.mockRestore();
  });

  test("...and a 404 end takes the focus the vanished control dropped", async () => {
    // The other end this page knows and the phase does not: the job expired or
    // the server restarted, so the poll 404s, JobNotFound replaces the whole run
    // view and the Stop button goes with it. `phase` never reaches a terminal
    // value here — it is the page's `terminal` boolean that covers this.
    let gone = false;
    server.use(
      http.get(JOB_URL, () => {
        if (gone) return HttpResponse.json({ detail: "gone" }, { status: 404 });
        gone = true;
        return HttpResponse.json(parkedJob({ awaiting_decision: false }));
      }),
    );
    renderAt("/import?job=job-1");

    const stop = await screen.findByRole("button", { name: "Stop this run" });
    stop.focus();
    expect(document.activeElement).toBe(stop);

    expect(
      await screen.findByText(/no longer available/i, undefined, {
        timeout: 3000,
      }),
    ).toBeInTheDocument();
    expect(stop.isConnected).toBe(false);
    // By identity, not `!== body`: the weak oracle passes against any patch
    // that happens to leave focus somewhere.
    expect(document.activeElement).toBe(
      screen.getByRole("heading", { level: 1, name: "Add from folder" }),
    );
  });

  test("...and a stopped bank apply's Review link drops the solid fill", async () => {
    // The reading the stopped manual panel already takes: a solid CTA under
    // "Import stopped" reads as a success panel. The link stays — the banked
    // rows are owed to Review either way. Reachable through the API only, since
    // this page offers no stop on a bank apply.
    server.use(
      http.get(JOB_URL, () =>
        HttpResponse.json(
          makeJob({
            phase: "done",
            origin: "bank_apply",
            stopped: true,
            aborted: true,
            set_aside: 1,
          }),
        ),
      ),
    );
    renderAt("/import?job=job-1");

    expect(
      await screen.findByRole("link", { name: "Review banked albums" }),
    ).toHaveAttribute("data-variant", "outline");
  });

  test("...and an apply whose stop arrived too late keeps the solid one", async () => {
    // The third reading for this arm: accepted, never acted on, so the panel is
    // the finished one and the CTA keeps the weight a finished panel earns.
    server.use(
      http.get(JOB_URL, () =>
        HttpResponse.json(
          makeJob({
            phase: "done",
            origin: "bank_apply",
            stopped: true,
            aborted: false,
            set_aside: 1,
          }),
        ),
      ),
    );
    renderAt("/import?job=job-1");

    expect(
      await screen.findByRole("link", { name: "Review banked albums" }),
    ).toHaveAttribute("data-variant", "default");
    // The acknowledging line is origin-gated with the remedy: nobody pressed a
    // button on this page for a bank apply, so there is no press to answer.
    expect(
      screen.queryByText("Nothing was left to stop."),
    ).not.toBeInTheDocument();
  });

  test("a stopped all-known run offers the folder, not beets' -I", async () => {
    // A stop in the first seconds, while beets is still skipping folders its
    // history already has: the counters read "nothing new" and the run has not
    // established that — it did not get that far. `set_aside` is 0 because the
    // stop landed at the top of `choose_match`, before any row exists.
    server.use(
      http.get(JOB_URL, () =>
        HttpResponse.json(
          makeJob({
            phase: "done",
            stopped: true,
            aborted: true,
            albums: [],
            set_aside: 0,
            path: "/music/incoming",
            progress: {
              applied: 0,
              needs_review: 0,
              skipped: 0,
              not_landed: 0,
              already_known: 2,
            },
          }),
        ),
      ),
    );
    renderAt("/import?job=job-1");

    expect(await screen.findByText("Import stopped")).toBeInTheDocument();
    // "Import them again" is beets' `-I`: it re-imports the albums already in
    // the library, directly under a sentence saying to add the folder again and
    // leave them be.
    expect(
      screen.queryByRole("button", { name: /import them again/i }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Add from folder" }),
    ).toHaveAttribute("href", "/import");
  });

  // The control for the arm above: the same all-known run, unstopped, still
  // reaches the `-I` button.
  test("...and an all-known run that FINISHED still offers it", async () => {
    server.use(
      http.get(JOB_URL, () =>
        HttpResponse.json(
          makeJob({
            phase: "done",
            albums: [],
            set_aside: 0,
            path: "/music/incoming",
            progress: {
              applied: 0,
              needs_review: 0,
              skipped: 0,
              not_landed: 0,
              already_known: 2,
            },
          }),
        ),
      ),
    );
    renderAt("/import?job=job-1");

    expect(await screen.findByText("Nothing new to import")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Import them again" }),
    ).toBeInTheDocument();
  });

  test("a stopped run of another origin keeps the title, not the folder remedy", async () => {
    // The API accepts a stop on any origin, so this panel is reachable without
    // the button. What is true of any stop stays — the title, the glyph, the
    // read-only feed. The remedy does not: an inbox run's rest is not a folder
    // the user adds back, and the drain has already moved on.
    server.use(
      http.get(JOB_URL, () =>
        HttpResponse.json(
          makeJob({
            phase: "done",
            origin: "inbox",
            stopped: true,
            aborted: true,
            set_aside: 1,
          }),
        ),
      ),
    );
    renderAt("/import?job=job-1");

    expect(await screen.findByText("Import stopped")).toBeInTheDocument();
    expect(
      screen.queryByText(/The rest stayed in the folder/),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: "Add from folder" }),
    ).not.toBeInTheDocument();
    // Still no button that would post into a released slot.
    expect(screen.queryByRole("link", { name: "Review" })).not.toBeInTheDocument();
  });

  test("a read-only feed's badge drops the fill that claims an action", async () => {
    server.use(
      http.get(JOB_URL, () =>
        HttpResponse.json(
          makeJob({ phase: "done", stopped: true, aborted: true, set_aside: 1 }),
        ),
      ),
    );
    renderAt("/import?job=job-1");

    // The words stay true — that album really was never decided — but the
    // primary fill is the app's "needs you" signal and the row has nothing left
    // to press.
    expect(await screen.findByText("Needs review")).toHaveAttribute(
      "data-variant",
      "outline",
    );
  });

  test("...and a live feed's badge keeps it", async () => {
    // The control: the fill pairs with the Review button, so it goes only where
    // the button does.
    server.use(http.get(JOB_URL, () => HttpResponse.json(parkedJob())));
    renderAt("/import?job=job-1");

    expect(await screen.findByText("Needs review")).toHaveAttribute(
      "data-variant",
      "default",
    );
    expect(screen.getByRole("link", { name: "Review" })).toBeInTheDocument();
  });

  test("an unattended run that finished on its own keeps its live Review button", async () => {
    // The control for the read-only rule above: `stopped`, not "done", is what
    // strips the buttons. An inbox run banks and finishes while still holding an
    // album for review, and that decision is still live.
    server.use(
      http.get(JOB_URL, () =>
        HttpResponse.json(
          makeJob({ phase: "done", origin: "inbox", set_aside: 1 }),
        ),
      ),
    );
    renderAt("/import?job=job-1");

    expect(await screen.findByText("Import finished")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Review" })).toBeInTheDocument();
  });
});

describe("ImportPage — already known folders", () => {
  const RETRY_JOB_URL = `${window.location.origin}/api/import/job-2`;

  /** A feed row waiting for a decision — the category that sits in none of the
   * imported / skipped / lost counters (`state.set_aside` is its count). On a
   * finished unattended run it is still on screen, with its Review button. */
  const PARKED_ROW: ImportAlbumSummary = {
    index: 0,
    folder: "/music/incoming/Radiohead - Kid A",
    artist: "Radiohead",
    album: "Kid A",
    recommendation: "medium",
    confidence: 76,
    status: "needs_review",
    album_id: null,
    did_not_land: false,
  };

  /** That row as a feed. Its own binding, so the `as const` rows below carry a
   * mutable `ImportAlbumSummary[]` rather than a readonly tuple literal. */
  const PARKED_FEED: ImportAlbumSummary[] = [PARKED_ROW];

  /** A finished review run that did nothing but skip folders beets' import
   * history already has — the keep-downloads dead end `incremental: false`
   * (beets' `-I`) exists for. `path` is the folder the run was started with. */
  function knownOnlyJob(overrides: Partial<ImportJobState> = {}): ImportJobState {
    return makeJob({
      phase: "done",
      progress: {
        applied: 0,
        needs_review: 0,
        skipped: 0,
        not_landed: 0,
        already_known: 2,
      },
      albums: [],
      path: "/music/incoming/Boards of Canada",
      ...overrides,
    });
  }

  test("the finished counts line and the announcement both name the history skips", async () => {
    server.use(
      http.get(JOB_URL, () =>
        HttpResponse.json(
          knownOnlyJob({
            progress: {
              applied: 1,
              needs_review: 0,
              skipped: 0,
              not_landed: 0,
              already_known: 2,
            },
          }),
        ),
      ),
    );
    renderAt("/import?job=job-1");

    expect(
      await screen.findByText("1 album imported · 0 skipped · 2 already known"),
    ).toBeInTheDocument();
    // Same word in both channels — the sweep tile's "Already known".
    expect(screen.getByRole("status")).toHaveTextContent(
      "Import complete. Imported 1, skipped 0. 2 already known.",
    );
  });

  // At 360px and 320px the line wraps, and it was breaking INSIDE a segment —
  // "… · 1" / "already known" — orphaning a number from the words it counts.
  // jsdom lays nothing out, so the oracle is the raw text: every space inside a
  // segment is non-breaking, leaving the ordinary space before each middot as
  // the only break opportunity (and a wrapped line then opens with the middot,
  // which is this page's dialect). `textContent`, not the matcher — testing-
  // library's normaliser folds NBSP to a space, which is exactly why the
  // assertions elsewhere in this file still read naturally.
  test.each([
    [
      "the finished",
      "done",
      { applied: 2, needs_review: 0, skipped: 1, not_landed: 1, already_known: 3 },
      "2 albums imported · 1 skipped · 1 didn’t land · 3 already known",
    ],
    // The failed panel's own join branch (nothing landed or skipped), so both
    // builders are covered rather than one calling the other.
    [
      "the failed",
      "failed",
      { applied: 0, needs_review: 0, skipped: 0, not_landed: 1, already_known: 3 },
      "1 didn’t land · 3 already known",
    ],
  ] as const)(
    "%s counts line can only wrap between segments",
    async (_case, phase, progress, text) => {
      server.use(
        http.get(JOB_URL, () =>
          HttpResponse.json(
            knownOnlyJob({
              phase,
              error: phase === "failed" ? "the disk went away" : null,
              progress,
            }),
          ),
        ),
      );
      renderAt("/import?job=job-1");

      const raw = (await screen.findByText(text)).textContent ?? "";
      expect(raw).toContain(`${progress.already_known} already known`);
      expect(raw).toContain("1 didn’t land");
      // One ordinary space per separator and nowhere else: N segments, N-1
      // break opportunities, N chunks.
      expect(raw.split(" ")).toHaveLength(text.split("·").length);
    },
  );

  test("a run with no history skips gains no clause, in either channel", async () => {
    server.use(
      http.get(JOB_URL, () =>
        HttpResponse.json(
          knownOnlyJob({
            progress: {
              applied: 2,
              needs_review: 0,
              skipped: 1,
              not_landed: 0,
              already_known: 0,
            },
          }),
        ),
      ),
    );
    renderAt("/import?job=job-1");

    expect(
      await screen.findByText("2 albums imported · 1 skipped"),
    ).toBeInTheDocument();
    // Covers the announcer too: it is text in the document, sr-only or not.
    expect(screen.queryByText(/already known/i)).not.toBeInTheDocument();
  });

  test("a failed run whose only news is a history skip still says so", async () => {
    // The panel's line and the failed announcement gate on the same number, so
    // the one live region cannot report a count the panel never shows.
    server.use(
      http.get(JOB_URL, () =>
        HttpResponse.json(
          knownOnlyJob({ phase: "failed", error: "disk full" }),
        ),
      ),
    );
    renderAt("/import?job=job-1");

    expect(await screen.findByText("Import failed")).toBeInTheDocument();
    expect(screen.getByText("2 already known")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent(
      "The import failed. 2 already known.",
    );
  });

  test("Import them again posts incremental false for the run's folder and moves into the new job", async () => {
    let seenBody: unknown = null;
    server.use(
      http.get(JOB_URL, () => HttpResponse.json(knownOnlyJob())),
      http.post(IMPORT_URL, async ({ request }) => {
        seenBody = await request.json();
        return HttpResponse.json({ job_id: "job-2" }, { status: 202 });
      }),
      http.get(RETRY_JOB_URL, () =>
        HttpResponse.json(
          makeJob({ job_id: "job-2", phase: "scanning", albums: [] }),
        ),
      ),
    );
    const user = userEvent.setup();
    renderAt("/import?job=job-1");

    await user.click(
      await screen.findByRole("button", { name: /import them again/i }),
    );

    // The new job drives the page — the same `?job=` hand-off a fresh start
    // makes, so the finished panel is gone.
    expect(await screen.findByText(/scanning your folder/i)).toBeInTheDocument();
    expect(seenBody).toEqual({
      path: "/music/incoming/Boards of Canada",
      options: {
        operation: "default",
        unattended: false,
        sweep: false,
        incremental: false,
      },
    });
  });

  test("while the re-import is starting the button is aria-disabled, keeps focus, and swallows the repeat", async () => {
    let posts = 0;
    server.use(
      http.get(JOB_URL, () => HttpResponse.json(knownOnlyJob())),
      http.post(IMPORT_URL, async () => {
        posts += 1;
        await delay("infinite");
        return HttpResponse.json({ job_id: "job-2" }, { status: 202 });
      }),
    );
    const user = userEvent.setup();
    renderAt("/import?job=job-1");

    await user.click(
      await screen.findByRole("button", { name: /import them again/i }),
    );
    const button = await screen.findByRole("button", { name: /starting/i });
    // THE oracle: `disabled` on the click's own commit strands keyboard focus
    // on <body>, and the failure this button is most likely to hit says "try
    // again". jsdom does not blur on disable, so an activeElement assertion
    // would pass with the bug present — these two attributes are the proof.
    expect(button).toHaveAttribute("aria-disabled", "true");
    expect(button).not.toBeDisabled();

    // ...and inert all the same.
    await user.click(button);
    expect(posts).toBe(1);
  });

  test("a 409 carries the server's own reason, and links it to the button", async () => {
    // Three different refusals share this status (a running import, a held
    // beets swap lock, a backfill/disk sync), so a hard-coded sentence is false
    // for two of them. This fixture is the one the old copy named wrongly.
    server.use(
      http.get(JOB_URL, () => HttpResponse.json(knownOnlyJob())),
      http.post(IMPORT_URL, () =>
        HttpResponse.json(
          {
            detail:
              "A library backfill is in progress; import available when it finishes",
          },
          { status: 409 },
        ),
      ),
    );
    const user = userEvent.setup();
    renderAt("/import?job=job-1");

    await user.click(
      await screen.findByRole("button", { name: /import them again/i }),
    );
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(
      "A library backfill is in progress; import available when it finishes",
    );
    // `items-center` sizes this <p> fit-content, so it needs a definite width
    // as well as the break rule or its min-content width becomes the page's.
    expect(alert).toHaveClass("w-full", "break-words");
    expect(screen.queryByText(/an import is already running/i)).not.toBeInTheDocument();
    // The alert describes the control it belongs to, so a keyboard user who
    // comes back to the button hears why the last press failed.
    const button = screen.getByRole("button", { name: /import them again/i });
    expect(button).toHaveAttribute("aria-describedby", alert.id);
    expect(alert.id).not.toBe("");
    // Reading order: counts, then why it failed, then the control — the same
    // shape as the page's three other error sites.
    expect(
      alert.compareDocumentPosition(button) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    // Not a dead end: the panel stays and the button is live for a retry.
    expect(button).toBeEnabled();
    expect(button).not.toHaveAttribute("aria-disabled", "true");
  });

  test("a 409 with no detail falls back to a sentence true for every reason", async () => {
    server.use(
      http.get(JOB_URL, () => HttpResponse.json(knownOnlyJob())),
      http.post(IMPORT_URL, () => new HttpResponse(null, { status: 409 })),
    );
    const user = userEvent.setup();
    renderAt("/import?job=job-1");

    await user.click(
      await screen.findByRole("button", { name: /import them again/i }),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The library is busy. Try again shortly.",
    );
  });

  test("a 503 carries the layout refusal instead of inviting a retry", async () => {
    // Nothing changes until the store layout does, so "Couldn't start. Try
    // again." is an instruction that cannot work.
    server.use(
      http.get(JOB_URL, () => HttpResponse.json(knownOnlyJob())),
      http.post(IMPORT_URL, () =>
        HttpResponse.json(
          { detail: "the music folder is not mounted; imports are refused" },
          { status: 503 },
        ),
      ),
    );
    const user = userEvent.setup();
    renderAt("/import?job=job-1");

    await user.click(
      await screen.findByRole("button", { name: /import them again/i }),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "the music folder is not mounted; imports are refused",
    );
    expect(screen.queryByText(/try again/i)).not.toBeInTheDocument();
  });

  test("a 422 shows the backend's own reason", async () => {
    server.use(
      http.get(JOB_URL, () => HttpResponse.json(knownOnlyJob())),
      http.post(IMPORT_URL, () =>
        HttpResponse.json(
          { detail: "that folder is inside your library" },
          { status: 422 },
        ),
      ),
    );
    const user = userEvent.setup();
    renderAt("/import?job=job-1");

    await user.click(
      await screen.findByRole("button", { name: /import them again/i }),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "that folder is inside your library",
    );
  });

  test("an all-known run is titled Nothing new to import, not a check over two zeros", async () => {
    // A success check above "0 albums imported · 0 skipped" reads as a silent
    // failure; the skips are the whole story, so the title tells it.
    server.use(http.get(JOB_URL, () => HttpResponse.json(knownOnlyJob())));
    renderAt("/import?job=job-1");

    expect(await screen.findByText("Nothing new to import")).toBeInTheDocument();
    expect(screen.queryByText("Import finished")).not.toBeInTheDocument();
    expect(
      screen.getByText("0 albums imported · 0 skipped · 2 already known"),
    ).toBeInTheDocument();
  });

  // Each row has known folders AND something else to report, so the run did do
  // something. The overrides are whole job shapes, not just progress: the
  // set-aside row lives outside `progress` entirely.
  test.each([
    [
      "a run that imported something",
      {
        progress: { applied: 1, needs_review: 0, skipped: 0, not_landed: 0, already_known: 2 },
      },
    ],
    [
      "a run that skipped an album on its own merits",
      {
        progress: { applied: 0, needs_review: 0, skipped: 1, not_landed: 0, already_known: 2 },
      },
    ],
    [
      "a run that lost an album",
      {
        progress: { applied: 0, needs_review: 0, skipped: 0, not_landed: 1, already_known: 2 },
      },
    ],
    [
      "a run with no history skips at all",
      {
        progress: { applied: 0, needs_review: 0, skipped: 0, not_landed: 0, already_known: 0 },
      },
    ],
    // The counters do not partition the run: a set-aside row is in none of the
    // three, so "nothing happened" read true over a feed listing an album with
    // a live Review button under it. An unattended inbox run is how a finished
    // job still holds one.
    [
      "an unattended run holding an album for review",
      { origin: "inbox" as const, set_aside: 1, albums: PARKED_FEED },
    ],
  ] as const)("%s is still titled Import finished", async (_case, overrides) => {
    server.use(
      http.get(JOB_URL, () => HttpResponse.json(knownOnlyJob(overrides))),
    );
    renderAt("/import?job=job-1");

    expect(await screen.findByText("Import finished")).toBeInTheDocument();
    expect(screen.queryByText("Nothing new to import")).not.toBeInTheDocument();
  });

  test("one known folder: the label agrees in number", async () => {
    server.use(
      http.get(JOB_URL, () =>
        HttpResponse.json(
          knownOnlyJob({
            progress: {
              applied: 0,
              needs_review: 0,
              skipped: 0,
              not_landed: 0,
              already_known: 1,
            },
          }),
        ),
      ),
    );
    renderAt("/import?job=job-1");

    expect(
      await screen.findByRole("button", { name: "Import it again" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Import them again" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByText("0 albums imported · 0 skipped · 1 already known"),
    ).toBeInTheDocument();
  });

  test("the new run takes focus to the page heading, not <body>", async () => {
    // `?job=A -> ?job=B` is a search-param change, so RouteAnnouncer's
    // pathname-keyed focus move never fires and the clicked button unmounts
    // with the panel (measured in Chromium (Orca), 2026-09-18: activeElement is
    // BODY).
    server.use(
      http.get(JOB_URL, () => HttpResponse.json(knownOnlyJob())),
      http.post(IMPORT_URL, () =>
        HttpResponse.json({ job_id: "job-2" }, { status: 202 }),
      ),
      http.get(RETRY_JOB_URL, () =>
        HttpResponse.json(
          makeJob({ job_id: "job-2", phase: "scanning", albums: [] }),
        ),
      ),
    );
    const user = userEvent.setup();
    renderAt("/import?job=job-1");

    await user.click(
      await screen.findByRole("button", { name: /import them again/i }),
    );
    expect(await screen.findByText(/scanning your folder/i)).toBeInTheDocument();
    expect(document.activeElement).toBe(
      screen.getByRole("heading", { level: 1, name: "Add from folder" }),
    );
  });

  test("...and a cold load keeps the browser's own focus", async () => {
    // The first effect run only records the pointer: landing on `?job=` by URL
    // is a page load, and RouteAnnouncer leaves those alone too.
    server.use(http.get(JOB_URL, () => HttpResponse.json(knownOnlyJob())));
    renderAt("/import?job=job-1");

    expect(await screen.findByText("Nothing new to import")).toBeInTheDocument();
    expect(document.activeElement).toBe(document.body);
  });

  test("...but never takes focus from something that holds it", async () => {
    server.use(
      http.get(JOB_URL, () => HttpResponse.json(knownOnlyJob())),
      http.post(IMPORT_URL, () =>
        HttpResponse.json({ job_id: "job-2" }, { status: 202 }),
      ),
      http.get(RETRY_JOB_URL, () =>
        HttpResponse.json(
          makeJob({ job_id: "job-2", phase: "scanning", albums: [] }),
        ),
      ),
    );
    // The element holding focus is rendered OUTSIDE the page. `ImportRun` is
    // mounted without a key, so a `?job=` swap reconciles the chrome in place
    // and the h1 is the SAME element before and after — what changed is that the
    // chrome no longer carries a focusable of its own: the old "Start over" link
    // served here, and the header slot is empty on every terminal panel. The
    // panel below it does remount. The guard is about focus held anywhere on the
    // document, so an element outside the page states that directly.
    renderWithProviders(
      <>
        <a href="/artists">Elsewhere</a>
        <ImportPage />
      </>,
      { route: "/import?job=job-1", path: "/import" },
    );

    // fireEvent, not userEvent: it does not move focus, so the start is in
    // flight while the user tabs away.
    fireEvent.click(
      await screen.findByRole("button", { name: /import them again/i }),
    );
    const elsewhere = screen.getByRole("link", { name: "Elsewhere" });
    elsewhere.focus();

    expect(await screen.findByText(/scanning your folder/i)).toBeInTheDocument();
    expect(document.activeElement).toBe(elsewhere);
  });

  // Offered ONLY for a review run that did nothing but skip known folders.
  // A mixed run has a better next step — the album's own folder (design note
  // 13) — and re-importing the parent would re-offer what just landed.
  // The third column is the panel's title: it follows the COUNTS while the
  // button follows `importAgainPath`, so the two all-known rows here are a
  // "Nothing new to import" panel with nothing to press.
  test.each([
    [
      "a mixed run",
      {
        progress: {
          applied: 1,
          needs_review: 0,
          skipped: 0,
          not_landed: 0,
          already_known: 2,
        },
      },
      "Import finished",
    ],
    [
      "a run that skipped an album on its own merits",
      {
        progress: {
          applied: 0,
          needs_review: 0,
          skipped: 1,
          not_landed: 0,
          already_known: 2,
        },
      },
      "Import finished",
    ],
    [
      "a run with no history skips",
      {
        progress: {
          applied: 0,
          needs_review: 0,
          skipped: 0,
          not_landed: 0,
          already_known: 0,
        },
      },
      "Import finished",
    ],
    // A lost album is something that happened, so the title and the button now
    // agree it is not an all-known run — one predicate feeds both. Before, the
    // title counted `not_landed` and the button did not, and this row got
    // "Import finished" with "Import them again" under it.
    [
      "a run that lost an album",
      {
        progress: {
          applied: 0,
          needs_review: 0,
          skipped: 0,
          not_landed: 1,
          already_known: 2,
        },
      },
      "Import finished",
    ],
    // The same predicate's set-aside term: this run is still holding an album,
    // and the feed below lists it with a live Review button.
    [
      "a run holding an album for review",
      { origin: "inbox" as const, set_aside: 1, albums: PARKED_FEED },
      "Import finished",
    ],
    ["a multi-folder start (no single path)", { path: null }, "Nothing new to import"],
    ["an unattended inbox run", { origin: "inbox" as const }, "Nothing new to import"],
  ] as const)("no Import them again for %s", async (_case, overrides, title) => {
    server.use(
      http.get(JOB_URL, () => HttpResponse.json(knownOnlyJob(overrides))),
    );
    renderAt("/import?job=job-1");

    expect(await screen.findByText(title)).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /import (them|it) again/i }),
    ).not.toBeInTheDocument();
  });

  test("a finished sweep offers no Import them again", async () => {
    // Its own panel: a sweep is routed before the done branch, so this pins the
    // routing rather than the button's own origin term (the inbox case above
    // pins that). A sweep re-run is how a sweep gets past its own history.
    server.use(
      http.get(SWEEP_JOB_URL, () =>
        HttpResponse.json(
          sweepJob({
            phase: "done",
            path: "/music/incoming",
            progress: {
              applied: 0,
              needs_review: 0,
              skipped: 0,
              not_landed: 0,
              already_known: 3,
            },
          }),
        ),
      ),
    );
    renderAt("/import?job=s1");

    expect(await screen.findByText("Sweep finished")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /import them again/i }),
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
            progress: { applied: 0, needs_review: 0, skipped: 0, not_landed: 0, already_known: 0 },
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
              stopped: false,
            },
          }),
        ),
      ),
      http.post(SWEEP_STOP_URL, () => {
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
              stopped: paused,
            },
          }),
        ),
      ),
      http.post(SWEEP_STOP_URL, () => {
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
    // the aria state — keyed on `sweep.stopped` alone it still read "Pause
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
              stopped: false,
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
            // Both flags: one request sets the job's `stopped` AND the sweep's
            // (`registry.py` `request_stop`), and the throttle bypass reads the
            // job's, so a fixture carrying only the sweep's would be testing a
            // state the server cannot produce.
            stopped: true,
            sweep: {
              processed: 6,
              auto_applied: 4,
              banked: 2,
              skipped_known: 0,
              current_folder: "/library/Adele/21",
              stopped: true,
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
              stopped: false,
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
            // The job flag too — one request sets both.
            stopped: true,
            sweep: {
              processed: 30,
              auto_applied: 20,
              banked: 10,
              skipped_known: 1,
              current_folder: null,
              stopped: true,
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
              stopped: false,
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
                stopped: false,
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
              stopped: false,
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
            stopped: false,
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
