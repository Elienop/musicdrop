import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";
import { beforeEach, describe, expect, test, vi } from "vitest";

import { SEGMENT_SEP } from "@/lib/format";
import { ReviewPage } from "@/pages/review/ReviewPage";
import {
  containerQueryVariants,
  unwiredContainerQueries,
} from "@/test/containerQuery";
import { renderWithProviders } from "@/test/render";
import { server } from "@/test/msw-server";

// Spy on navigation while keeping the real MemoryRouter (the render harness
// imports it from the same module) — only `useNavigate` is swapped.
const { mockNavigate } = vi.hoisted(() => ({ mockNavigate: vi.fn() }));
vi.mock("react-router", async (importOriginal) => ({
  ...(await importOriginal<typeof import("react-router")>()),
  useNavigate: () => mockNavigate,
}));

// The bank section's 409s surface through sonner (the activityToasts dialect).
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));
import { toast } from "sonner";
const toastError = vi.mocked(toast.error);

const O = window.location.origin;
const ACTIVE = `${O}/api/imports/active`;
const JOB = `${O}/api/import/:jobId`;
const ITEMS = `${O}/api/acquisition/inbox/items`;
const STATUS = `${O}/api/acquisition/status`;
const DUPES = `${O}/api/duplicates`;
const IMPORT_ITEM = `${O}/api/acquisition/inbox/items/import`;
const REVIEW_ALL = `${O}/api/acquisition/review-inbox`;
const BANK = `${O}/api/bank`;

const idleActive = { active: false, origin: "manual", needs_review_count: 0 };
const idleStatus = {
  phase: "idle",
  queued: 0,
  current: null,
  processed: 0,
  set_aside: 0,
  failed: 0,
  error: null,
  inbox_pending: 0,
};
const noDupes = { mode: "strict", group_count: 0, album_count: 0, groups: [] };

function album(over: Record<string, unknown>) {
  return {
    index: 0,
    folder: "/inbox/x",
    artist: "Artist",
    album: "Album",
    recommendation: "low",
    confidence: 50,
    status: "needs_review",
    ...over,
  };
}

/** Renders the AlbumOrigin router state a decision link arrives with. */
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

describe("ReviewPage", () => {
  beforeEach(() => {
    mockNavigate.mockClear();
    toastError.mockClear();
    // Idle defaults for every probe the page polls, so each test only overrides
    // what it asserts (the MSW server errors on an unhandled request).
    server.use(
      http.get(ACTIVE, () => HttpResponse.json(idleActive)),
      http.get(ITEMS, () => HttpResponse.json({ items: [] })),
      http.get(STATUS, () => HttpResponse.json(idleStatus)),
      http.get(BANK, () =>
        HttpResponse.json({ items: [], total: 0, total_all: 0, offset: 0, limit: 48 }),
      ),
      http.get(JOB, () =>
        HttpResponse.json({
          job_id: "j1",
          phase: "reviewing",
          progress: { applied: 0, needs_review: 0, skipped: 0 },
          albums: [],
          summary: null,
          error: null,
          origin: "manual",
          set_aside: 0,
        }),
      ),
    );
  });

  test("empty state when nothing is pending", async () => {
    renderWithProviders(<ReviewPage />);
    expect(await screen.findByText(/nothing to review/i)).toBeInTheDocument();
  });

  test("live decision rows route to the candidate vs duplicate screen by status", async () => {
    server.use(
      http.get(ACTIVE, () =>
        HttpResponse.json({ active: true, job_id: "j1", origin: "inbox", needs_review_count: 2 }),
      ),
      http.get(JOB, () =>
        HttpResponse.json({
          job_id: "j1",
          phase: "reviewing",
          progress: { applied: 0, needs_review: 1, skipped: 0 },
          albums: [
            album({ index: 0, album: "Echoes", status: "needs_review" }),
            album({ index: 1, album: "OK Computer", status: "needs_dup_resolution" }),
          ],
          summary: null,
          error: null,
          origin: "inbox",
          set_aside: 2,
        }),
      ),
    );
    renderWithProviders(<ReviewPage />);

    const review = await screen.findByRole("link", { name: /^review$/i });
    expect(review).toHaveAttribute("href", "/import/albums/0?job=j1");
    const resolve = screen.getByRole("link", { name: /resolve/i });
    expect(resolve).toHaveAttribute("href", "/import/albums/1/duplicate?job=j1");

    // decisions 39 reaches this row too: the defect reproduced here, narrower
    // — the title measured 0px at 320→344 and at 320→328 the "Already in
    // library" badge's ink sat inside Resolve's hit rectangle. Same shape as
    // the bank row, and since 2026-09-11 the bank row's threshold too — the
    // owner set 28rem on all three rows. jsdom holds the structure: the action
    // is a grid item of the <li>, with both arms named.
    const row = resolve.closest("li");
    expect(row).toHaveClass("grid");
    expect(row?.className.split(/\s+/)).toContain("@container/decisionrow");
    // The template too: without it the `col-start` pins still resolve into
    // implicit columns, and the text track loses `minmax(0,1fr)` — it
    // auto-sizes and the title stops truncating.
    expect(row?.className.split(/\s+/)).toContain("grid-cols-[minmax(0,1fr)_auto]");
    const group = resolve.parentElement;
    expect(group?.parentElement).toBe(row);
    // Both halves of both arms: with the column half dropped the action is
    // placed at (row 1, col 1) — definite-position items go down before
    // auto-placed ones — and the AlbumRow is pushed a track right.
    for (const token of [
      "col-start-1",
      "row-start-2",
      "@min-[28rem]/decisionrow:col-start-2",
      "@min-[28rem]/decisionrow:row-start-1",
    ]) {
      expect(group?.className.split(/\s+/)).toContain(token);
    }
    expect(containerQueryVariants(row as HTMLElement)).toEqual([
      // No `:block` here: that one is on AlbumRow's middot, which renders only
      // when the row has BOTH a subtitle and a meta line.
      "@min-[18rem]/rowtext:flex-row",
      "@min-[18rem]/rowtext:items-center",
      "@min-[28rem]/decisionrow:-ml-1",
      "@min-[28rem]/decisionrow:col-start-2",
      "@min-[28rem]/decisionrow:mb-0",
      "@min-[28rem]/decisionrow:mr-4",
      "@min-[28rem]/decisionrow:row-start-1",
    ]);
    expect(unwiredContainerQueries(row as HTMLElement)).toEqual([]);
  });

  test("the decision row's confidence line uses the app's segment separator", async () => {
    server.use(
      http.get(ACTIVE, () =>
        HttpResponse.json({ active: true, job_id: "j1", origin: "inbox", needs_review_count: 1 }),
      ),
      http.get(JOB, () =>
        HttpResponse.json({
          job_id: "j1",
          phase: "reviewing",
          progress: { applied: 0, needs_review: 1, skipped: 0 },
          albums: [
            album({
              index: 0,
              album: "Echoes",
              status: "needs_review",
              confidence: 76,
              recommendation: "medium",
            }),
          ],
          summary: null,
          error: null,
          origin: "inbox",
          set_aside: 0,
        }),
      ),
    );
    renderWithProviders(<ReviewPage />);
    await screen.findByRole("link", { name: /^review$/i });

    // Located on its shape (the SPAN whose text ends in the label), then
    // compared as a literal string. `getByText` cannot do the second half: the
    // default RTL normalizer collapses U+00A0 to a plain space, so a text query
    // reads " \u00b7\u00a0" and " \u00b7 " as the same thing. The NBSP is
    // written as an escape because a literal one is invisible in review.
    const meta = screen.getByText(
      (_, el) => el?.tagName === "SPAN" && (el.textContent ?? "").endsWith("Medium match"),
    );
    expect(meta.textContent).toBe("76% \u00b7\u00a0Medium match");
  });

  test("inbox rows render name, set-aside tag, and track count", async () => {
    server.use(
      http.get(ITEMS, () =>
        HttpResponse.json({
          items: [
            { name: "Lost Tapes", mtime: 2, size: 10, track_count: 9, outcome: "set_aside" },
            { name: "Demo 99", mtime: 1, size: 5, track_count: 1, outcome: null },
          ],
        }),
      ),
    );
    renderWithProviders(<ReviewPage />);

    expect(await screen.findByText("Lost Tapes")).toBeInTheDocument();
    expect(screen.getByText(/set aside/i)).toBeInTheDocument();
    expect(screen.getByText(/9 tracks/)).toBeInTheDocument();
    expect(screen.getByText(/1 track\b/)).toBeInTheDocument();
  });

  test("per-item Review starts that folder and navigates into the job", async () => {
    server.use(
      http.get(ITEMS, () =>
        HttpResponse.json({
          items: [{ name: "Lost Tapes", mtime: 1, size: 10, track_count: 9, outcome: null }],
        }),
      ),
      http.post(IMPORT_ITEM, () =>
        HttpResponse.json({ started: true, job_id: "jx", pending: 1 }),
      ),
    );
    renderWithProviders(<ReviewPage />);

    await userEvent.click(await screen.findByRole("button", { name: /^review$/i }));
    await waitFor(() => expect(mockNavigate).toHaveBeenCalledWith("/import?job=jx"));
  });

  test("Review all imports the whole inbox and navigates", async () => {
    server.use(
      http.get(ITEMS, () =>
        HttpResponse.json({
          items: [{ name: "Lost Tapes", mtime: 1, size: 10, track_count: 9, outcome: null }],
        }),
      ),
      http.post(REVIEW_ALL, () =>
        HttpResponse.json({ started: true, job_id: "jall", pending: 1 }),
      ),
    );
    renderWithProviders(<ReviewPage />);

    await userEvent.click(await screen.findByRole("button", { name: /review all/i }));
    await waitFor(() => expect(mockNavigate).toHaveBeenCalledWith("/import?job=jall"));
  });

  test("an in-flight inbox row warns before its per-row Review override", async () => {
    // The per-row button has no settle guard — it is an explicit "import THIS
    // one now". The row must therefore SAY the folder is still arriving, or the
    // user overrides blind and files a partial album.
    server.use(
      http.get(ITEMS, () =>
        HttpResponse.json({
          items: [
            { name: "Half Arrived", mtime: 1, size: 10, track_count: 2, outcome: null, in_flight: true },
            { name: "All Here", mtime: 2, size: 20, track_count: 9, outcome: null, in_flight: false },
          ],
        }),
      ),
    );
    renderWithProviders(<ReviewPage />);

    expect(await screen.findByText(/still downloading — importing now may catch only part/i))
      .toBeInTheDocument();
    // the settled row keeps the plain affordance; only the in-flight one is hedged
    expect(screen.getByRole("button", { name: "Review anyway" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Review" })).toBeInTheDocument();
  });

  test("a no-op Review all says STILL DOWNLOADING when folders are in flight", async () => {
    // The backend refuses folders that are still receiving files. Saying "the
    // inbox just cleared" there would be a flat lie — the rows are still on
    // screen, and the user would have no idea why the button did nothing.
    server.use(
      http.get(ITEMS, () =>
        HttpResponse.json({
          items: [{ name: "Half Arrived", mtime: 1, size: 10, track_count: 2, outcome: null }],
        }),
      ),
      http.post(REVIEW_ALL, () =>
        HttpResponse.json({ started: false, job_id: null, pending: 0, in_flight: 1 }),
      ),
    );
    renderWithProviders(<ReviewPage />);

    await userEvent.click(await screen.findByRole("button", { name: /review all/i }));
    expect(await screen.findByText(/still downloading/i)).toBeInTheDocument();
    expect(screen.queryByText(/inbox just cleared/i)).not.toBeInTheDocument();
    expect(mockNavigate).not.toHaveBeenCalled();
    // the row it refused is still listed — the message must agree with the list
    expect(screen.getByText("Half Arrived")).toBeInTheDocument();
  });

  test("a no-op Review all with nothing in flight still says the inbox cleared", async () => {
    // The genuine race the message was written for: the inbox emptied between
    // the last poll and the click.
    server.use(
      http.get(ITEMS, () =>
        HttpResponse.json({
          items: [{ name: "Lost Tapes", mtime: 1, size: 10, track_count: 9, outcome: null }],
        }),
      ),
      http.post(REVIEW_ALL, () =>
        HttpResponse.json({ started: false, job_id: null, pending: 0, in_flight: 0 }),
      ),
    );
    renderWithProviders(<ReviewPage />);

    await userEvent.click(await screen.findByRole("button", { name: /review all/i }));
    expect(await screen.findByText(/inbox just cleared/i)).toBeInTheDocument();
  });

  test("inbox actions are disabled while an import is running", async () => {
    server.use(
      http.get(ACTIVE, () =>
        HttpResponse.json({ active: true, job_id: "j1", origin: "manual", needs_review_count: 0 }),
      ),
      http.get(ITEMS, () =>
        HttpResponse.json({
          items: [{ name: "Lost Tapes", mtime: 1, size: 10, track_count: 9, outcome: null }],
        }),
      ),
    );
    renderWithProviders(<ReviewPage />);

    await waitFor(() =>
      expect(screen.getByRole("button", { name: /review all/i })).toBeDisabled(),
    );
    expect(screen.getByRole("button", { name: /^review$/i })).toBeDisabled();
  });

  test("recent tally and last error surface from the status probe", async () => {
    server.use(
      http.get(STATUS, () =>
        HttpResponse.json({
          ...idleStatus,
          processed: 3,
          set_aside: 2,
          failed: 0,
          error: "disk full",
        }),
      ),
    );
    renderWithProviders(<ReviewPage />);

    const recent = await screen.findByRole("region", { name: /recent/i });
    expect(within(recent).getByText(/1 imported · 2 set aside · 0 failed/)).toBeInTheDocument();
    expect(screen.getByText(/last error: disk full/i)).toBeInTheDocument();
  });

  test("the duplicates pointer is a plain link — no library scan on mount", async () => {
    let scans = 0;
    server.use(
      http.get(DUPES, () => {
        scans += 1;
        return HttpResponse.json(noDupes);
      }),
    );
    renderWithProviders(<ReviewPage />);

    const link = await screen.findByRole("link", {
      name: /find duplicate albums in your library/i,
    });
    expect(link).toHaveAttribute("href", "/duplicates");
    // Page fully settled (empty state shown) and still zero scans fired.
    await screen.findByText(/nothing to review/i);
    expect(scans).toBe(0);
  });

  test("Importing now renders the current folder + queued count while the queue drains", async () => {
    server.use(
      http.get(STATUS, () =>
        HttpResponse.json({
          ...idleStatus,
          phase: "running",
          queued: 2,
          current: "/inbox/Neat Album",
        }),
      ),
    );
    renderWithProviders(<ReviewPage />);

    const section = await screen.findByRole("region", { name: /importing now/i });
    expect(within(section).getByText(/Importing Neat Album/)).toBeInTheDocument();
    expect(within(section).getByText(/2 queued/)).toBeInTheDocument();
  });

  test("Importing now is absent while the queue is idle", async () => {
    renderWithProviders(<ReviewPage />);
    await screen.findByText(/nothing to review/i);
    expect(
      screen.queryByRole("region", { name: /importing now/i }),
    ).not.toBeInTheDocument();
  });

  test("decision links thread the Review origin into the decision screens", async () => {
    server.use(
      http.get(ACTIVE, () =>
        HttpResponse.json({ active: true, job_id: "j1", origin: "inbox", needs_review_count: 1 }),
      ),
      http.get(JOB, () =>
        HttpResponse.json({
          job_id: "j1",
          phase: "reviewing",
          progress: { applied: 0, needs_review: 1, skipped: 0 },
          albums: [album({ index: 0, album: "Echoes", status: "needs_review" })],
          summary: null,
          error: null,
          origin: "inbox",
          set_aside: 1,
        }),
      ),
    );
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={["/review"]}>
          <Routes>
            <Route path="/review" element={<ReviewPage />} />
            <Route path="/import/albums/:index" element={<OriginProbe />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    await userEvent.click(await screen.findByRole("link", { name: /^review$/i }));
    expect(await screen.findByText("origin: Review /review")).toBeInTheDocument();
  });

  const bankRow = (over: Record<string, unknown> = {}) => ({
    id: "b1",
    folder: "/inbox/Album X",
    source: "sweep",
    reason: "needs_review",
    artist: "Artist",
    album: "Album X",
    recommendation: "medium",
    confidence: 71.2,
    status: "needs_review",
    error: null,
    album_id: null,
    banked_at: "2026-06-12T08:00:00Z",
    ...over,
  });

  test("bank rows render guess, reason chip, confidence and an Open link", async () => {
    server.use(
      http.get(BANK, () =>
        HttpResponse.json({ items: [bankRow()], total: 1, total_all: 1, offset: 0, limit: 48 }),
      ),
    );
    renderWithProviders(<ReviewPage />);
    const section = await screen.findByRole("region", { name: /waiting for review/i });
    expect(within(section).getByText("Album X")).toBeInTheDocument();
    // Scoped to the row — "Uncertain match" is also an option in the reason
    // filter, so the chip assertion must not reach into the dropdown.
    const row = within(section).getByRole("listitem");
    expect(within(row).getByText(/uncertain match/i)).toBeInTheDocument();
    expect(within(section).getByText(/71%/)).toBeInTheDocument();
    // One dialect for the `%` · tier line across the three AlbumRow surfaces
    // that render it — the import feed, the decision row and this bank row:
    // the humanized tier, not the raw `medium` enum, joined with SEGMENT_SEP
    // and not a plain " · ". The other two callers are out of scope, not
    // exceptions: the inbox row's meta is a bare track count with no
    // separator, and `/duplicates` joins its own line with a plain " · ".
    // Compared on `textContent` because SEGMENT_SEP's trailing space is a
    // NBSP, which RTL's default normalizer would collapse.
    expect(row.textContent).toContain(`71%${SEGMENT_SEP}Medium match`);
    expect(within(section).getByRole("link", { name: /open/i })).toHaveAttribute(
      "href",
      "/review/bank/b1",
    );
  });

  // `error` is `str(exc)` from the apply runner, so it is unbounded, and
  // AlbumRow's `meta` slot is `shrink-0` — used width max-content in the row
  // arm, so it can neither shrink nor wrap and an unbounded string in it runs
  // over the row's own controls. Widths in BACKLOG, "A failed bank row's error
  // overran the row". jsdom computes no layout, so what a test can hold is that
  // the error is OUT of that slot and that the classes bounding it are on the
  // elements that must carry them.
  const failedRowSetup = (error: string | null) => {
    server.use(
      http.get(BANK, () =>
        HttpResponse.json({
          items: [bankRow({ id: "b2", album: "Album Y", status: "failed", error })],
          total: 1,
          total_all: 1,
          offset: 0,
          limit: 48,
        }),
      ),
    );
    renderWithProviders(<ReviewPage />);
    return screen.findByRole("region", { name: /waiting for review/i });
  };

  // decisions 39. Under 28rem of ROW the Ignore/Remove/Open group takes its own
  // line under the row; above it the row is byte-identical to what it was.
  // Only two things make that possible, and jsdom can hold both: the group is a
  // grid ITEM of the <li> (from inside AlbumRow's `action` slot it could not
  // move without growing AlbumRow's box — the 16/26px checkbox drift #220
  // fixed), and the <li> is a grid, so `items-center` centres each item in its
  // own row track instead of against the tallest thing in one flex line. The
  // widths, the threshold's derivation and the drift being 0 are in the
  // branch's browser pass; BACKLOG carries the numbers.
  const needsReviewRow = () => {
    server.use(
      http.get(BANK, () =>
        HttpResponse.json({
          items: [bankRow({ id: "b3", album: "Album Z" })],
          total: 1,
          total_all: 1,
          offset: 0,
          limit: 48,
        }),
      ),
    );
    renderWithProviders(<ReviewPage />);
    return screen.findByRole("region", { name: /waiting for review/i });
  };

  test("the action group is a grid item of the row, not a child of AlbumRow", async () => {
    const section = await needsReviewRow();
    const ignore = await within(section).findByRole("button", { name: /ignore album z/i });
    const group = ignore.parentElement;
    const row = ignore.closest("li");
    // The group's parent IS the row. Inside AlbumRow's action slot this is the
    // slot's own wrapper, and the group cannot take a line of its own.
    expect(group?.parentElement).toBe(row);
    // Every control is in that one group: one set, not a visible copy plus a
    // hidden one.
    expect(group).toContainElement(within(section).getByRole("button", { name: /remove album z/i }));
    expect(group).toContainElement(within(section).getByRole("link", { name: /open album z/i }));
    expect(within(section).getAllByRole("button", { name: /ignore album z/i })).toHaveLength(1);
  });

  test("the row is a grid, so each line centres in its own row track", async () => {
    const section = await needsReviewRow();
    const row = (await within(section).findByRole("listitem")) as HTMLElement;
    expect(row).toHaveClass("grid");
    expect(row).toHaveClass("items-center");
    // The container the threshold is measured against is the row itself — a
    // viewport breakpoint would be wrong: at 768px the sidebar opens and the
    // row is NARROWER than at 520px. A token, not a substring: a row declaring
    // `@container/bankrowX` satisfies the regex and wires nothing.
    expect(row.className.split(/\s+/)).toContain("@container/bankrow");
    // The template too, not only the `col-start` pins: drop it and every pin
    // below still resolves (a grid makes implicit columns for them), while the
    // AlbumRow's track loses `minmax(0,1fr)`, becomes auto-sized, and the
    // title stops truncating — the defect this row was fixed for.
    expect(row.className.split(/\s+/)).toContain("grid-cols-[auto_minmax(0,1fr)_auto]");
    const group = within(section).getByRole("button", { name: /ignore album z/i }).parentElement;
    // Its own line below the row by default; back on the row's line above the
    // threshold. BOTH halves of BOTH arms: a grid places definite-position
    // items before auto-placed ones, so with the column half dropped the
    // action takes (row 1, col 1) and pushes the checkbox and the AlbumRow a
    // track right — total visual destruction, with the suite still green.
    for (const token of [
      "col-start-2",
      "row-start-2",
      "@min-[28rem]/bankrow:col-start-3",
      "@min-[28rem]/bankrow:row-start-1",
    ]) {
      expect(group?.className.split(/\s+/)).toContain(token);
    }
    // Every container query on the row, each wired to a container an ancestor
    // declares — a renamed container applies nothing at all, silently.
    expect(containerQueryVariants(row)).toEqual([
      "@min-[18rem]/rowtext:block",
      "@min-[18rem]/rowtext:flex-row",
      "@min-[18rem]/rowtext:items-center",
      "@min-[28rem]/bankrow:-ml-1",
      "@min-[28rem]/bankrow:col-start-3",
      "@min-[28rem]/bankrow:mb-0",
      "@min-[28rem]/bankrow:mr-4",
      "@min-[28rem]/bankrow:row-start-1",
    ]);
    expect(unwiredContainerQueries(row)).toEqual([]);
  });

  test("a failed row's error line sits below the dropped action line", async () => {
    const failure = "beets refused the import: /srv/music/incoming is not writable";
    const section = await failedRowSetup(failure);
    const line = await within(section).findByTitle(failure);
    const row = line.closest("li");
    const wrapper = line.parentElement;
    // Also a grid item of the row (never nested in the part that holds the
    // checkbox), spanning the full width, in the row AFTER the actions in both
    // arms — the reading order the one-line arm has.
    expect(wrapper?.parentElement).toBe(row);
    expect(wrapper?.className.split(/\s+/)).toContain("col-span-3");
    expect(wrapper?.className.split(/\s+/)).toContain("row-start-3");
    expect(wrapper?.className.split(/\s+/)).toContain("@min-[28rem]/bankrow:row-start-2");
    const controls = [...(row?.querySelectorAll("button, a[href]") ?? [])];
    // DOM order: the controls come before the error, in both arms.
    expect(controls.every((c) => c.compareDocumentPosition(line) & Node.DOCUMENT_POSITION_FOLLOWING)).toBe(true);
  });

  test("a failed row carries its error on its own line, not in the meta slot", async () => {
    const failure =
      "beets refused the import: /srv/music/incoming/Radiohead_-_Amnesiac/disc1 is not writable";
    const section = await failedRowSetup(failure);
    const line = await within(section).findByTitle(failure);
    // Whole text, and NOTHING else in that element: joining it into the meta
    // line would put the confidence and recommendation in here with it.
    expect(line.textContent).toBe(failure);
    // The meta bits survive the move — they are what the slot is sized for.
    expect(within(section).getByText(/71%/)).toBeInTheDocument();
    expect(line).not.toContainElement(within(section).getByText(/71%/));
  });

  // The regression the component comment warns about, pinned instead of only
  // described. `line-clamp` clips at the PADDING box, so padding on the clamped
  // element itself shows a sliced third line — measured 10px of one, at 768 as
  // well as 360. The padding therefore lives on the wrapper. `break-words` and
  // `min-w-0` are a pair: the clamped span is a flex item of the <p>, so
  // without the floor an unbroken path sets its own min-content width and the
  // wrap cannot lower it.
  test("the clamp and the padding sit on different elements", async () => {
    const failure = "/srv/music/incoming/" + "a".repeat(120) + "/track01.flac";
    const section = await failedRowSetup(failure);
    const line = await within(section).findByTitle(failure);
    const clamped = within(line).getByText(failure);
    expect(clamped.className).toContain("line-clamp-2");
    expect(clamped.className).toContain("break-words");
    expect(clamped.className).toContain("min-w-0");
    // No padding on the clamped element, in any direction.
    expect(clamped.className).not.toMatch(/(^|\s)p[btlrxy]?-/);
    const wrapper = line.parentElement;
    expect(wrapper?.className).toMatch(/(^|\s)p[btlrxy]?-/);
  });

  // The guard's other direction, both spellings the runner can produce. An
  // unguarded render leaves an empty padded line carrying a blank `title`.
  test.each([
    ["null", null],
    ["whitespace-only", "  \n "],
  ])("a failed row with a %s error renders no error line", async (_name, error) => {
    const section = await failedRowSetup(error);
    await within(section).findByText("Album Y");
    expect(within(section).queryByTitle("")).not.toBeInTheDocument();
    const row = within(section).getByRole("listitem");
    expect(row.querySelector(".line-clamp-2")).toBeNull();
  });

  test("an all-resolved bank keeps the section and its history hint reachable", async () => {
    // The active view is empty (everything's resolved) but rows still exist —
    // the section, both filters, and a hint back to history must stay visible.
    server.use(
      http.get(BANK, () =>
        HttpResponse.json({ items: [], total: 0, total_all: 5, offset: 0, limit: 48 }),
      ),
    );
    renderWithProviders(<ReviewPage />);
    const section = await screen.findByRole("region", { name: /waiting for review/i });
    expect(within(section).getByLabelText(/filter by status/i)).toBeInTheDocument();
    expect(within(section).getByLabelText(/filter by reason/i)).toBeInTheDocument();
    expect(
      within(section).getByText(/nothing needs attention\. switch the filter/i),
    ).toBeInTheDocument();
  });

  test("a truly empty bank (no rows at all) hides the whole section", async () => {
    server.use(
      http.get(BANK, () =>
        HttpResponse.json({ items: [], total: 0, total_all: 0, offset: 0, limit: 48 }),
      ),
    );
    renderWithProviders(<ReviewPage />);
    // Wait for the page itself to settle before asserting the section's absence.
    await screen.findByText(/need your decision/i);
    expect(
      screen.queryByRole("region", { name: /waiting for review/i }),
    ).not.toBeInTheDocument();
  });

  test("a bank load error surfaces an error + retry, never the false empty state", async () => {
    // A 5xx (backend restarting on a single home box) must NOT read as a resolved
    // backlog: the section shows the failure with a retry, and the page must not
    // claim "Nothing to review" while banked rows may still await a decision.
    server.use(http.get(BANK, () => new HttpResponse(null, { status: 500 })));
    renderWithProviders(<ReviewPage />);
    const section = await screen.findByRole("region", { name: /waiting for review/i });
    expect(within(section).getByRole("alert")).toBeInTheDocument();
    expect(
      within(section).getByRole("button", { name: /retry/i }),
    ).toBeInTheDocument();
    expect(screen.queryByText(/nothing to review/i)).not.toBeInTheDocument();
    // The header must not fabricate a "0 awaiting a decision" count off an error.
    expect(screen.queryByText(/awaiting a decision/i)).not.toBeInTheDocument();
  });

  test("Retry after a bank error refetches and shows the rows", async () => {
    let fail = true;
    server.use(
      http.get(BANK, () =>
        fail
          ? new HttpResponse(null, { status: 500 })
          : HttpResponse.json({ items: [bankRow()], total: 1, total_all: 1, offset: 0, limit: 48 }),
      ),
    );
    renderWithProviders(<ReviewPage />);
    const section = await screen.findByRole("region", { name: /waiting for review/i });
    fail = false;
    await userEvent.click(within(section).getByRole("button", { name: /retry/i }));
    expect(await screen.findByText("Album X")).toBeInTheDocument();
  });

  test("the reason filter narrows the query, sets bank_reason, and resets offset", async () => {
    const reasons: Array<string | null> = [];
    const offsets: Array<string | null> = [];
    server.use(
      http.get(BANK, ({ request }) => {
        const params = new URL(request.url).searchParams;
        if (params.get("limit") === "48") {
          reasons.push(params.get("reason"));
          offsets.push(params.get("offset"));
        }
        return HttpResponse.json({
          items: [bankRow()],
          total: 1,
          total_all: 1,
          offset: 0,
          limit: 48,
        });
      }),
    );
    renderWithProviders(<ReviewPage />, { route: "/review?bank_offset=48" });
    await screen.findByRole("region", { name: /waiting for review/i });
    await userEvent.selectOptions(
      screen.getByLabelText(/filter by reason/i),
      "needs_dup_resolution",
    );
    // The request carries the reason and pagination has reset to the first page.
    await waitFor(() => expect(reasons).toContain("needs_dup_resolution"));
    await waitFor(() => expect(offsets.at(-1)).toBe("0"));
  });

  test("the bank section paginates — Next requests the next offset", async () => {
    const offsets: string[] = [];
    server.use(
      http.get(BANK, ({ request }) => {
        const url = new URL(request.url);
        offsets.push(url.searchParams.get("offset") ?? "0");
        return HttpResponse.json({
          items: [bankRow()],
          total: 60,
          total_all: 60,
          offset: Number(url.searchParams.get("offset") ?? "0"),
          limit: 48,
        });
      }),
    );
    renderWithProviders(<ReviewPage />);
    await screen.findByRole("region", { name: /waiting for review/i });
    await userEvent.click(screen.getByRole("button", { name: /next/i }));
    await waitFor(() => expect(offsets).toContain("48"));
  });

  test("an out-of-range bank_offset offers Back to first page, not a dead end", async () => {
    // The backlog shrank to under a page (rows ignored/applied away) while the
    // URL still says bank_offset=48 — the refetched page is empty and the
    // Pagination control hides, so the empty state must carry the way back.
    server.use(
      http.get(BANK, ({ request }) => {
        const url = new URL(request.url);
        const offset = Number(url.searchParams.get("offset") ?? "0");
        return HttpResponse.json(
          offset >= 48
            ? { items: [], total: 1, total_all: 1, offset, limit: 48 }
            : { items: [bankRow()], total: 1, total_all: 1, offset, limit: 48 },
        );
      }),
    );
    renderWithProviders(<ReviewPage />, { route: "/review?bank_offset=48" });
    const section = await screen.findByRole("region", { name: /waiting for review/i });
    expect(within(section).getByText(/this page is empty/i)).toBeInTheDocument();
    expect(
      within(section).queryByText(/no rows match this filter/i),
    ).not.toBeInTheDocument();
    await userEvent.click(
      within(section).getByRole("button", { name: /back to first page/i }),
    );
    expect(await within(section).findByText("Album X")).toBeInTheDocument();
  });

  test("a row ignored via its own button is pruned from the bulk-selection count", async () => {
    let ignored = false;
    server.use(
      http.get(BANK, () =>
        HttpResponse.json(
          ignored
            ? { items: [bankRow({ id: "b2", album: "Album Y" })], total: 1, total_all: 1, offset: 0, limit: 48 }
            : {
                items: [bankRow(), bankRow({ id: "b2", album: "Album Y" })],
                total: 2, total_all: 2, offset: 0, limit: 48,
              },
        ),
      ),
      http.post(`${O}/api/bank/:itemId/decision`, () => {
        ignored = true;
        return HttpResponse.json(bankRow({ status: "ignored" }));
      }),
    );
    renderWithProviders(<ReviewPage />);
    const section = await screen.findByRole("region", { name: /waiting for review/i });
    await userEvent.click(within(section).getByRole("checkbox", { name: /select album x/i }));
    await userEvent.click(within(section).getByRole("checkbox", { name: /select album y/i }));
    expect(
      within(section).getByRole("button", { name: /ignore selected \(2\)/i }),
    ).toBeInTheDocument();
    // Row-level ignore settles b1 and the refetch drops it — the bulk count
    // must follow (no over-count, no already-settled id left to POST).
    await userEvent.click(within(section).getByRole("button", { name: /ignore album x/i }));
    expect(
      await within(section).findByRole("button", { name: /ignore selected \(1\)/i }),
    ).toBeInTheDocument();
  });

  test("the status filter narrows the list query — status only, no view", async () => {
    const queries: Array<{ status: string | null; view: string | null }> = [];
    server.use(
      http.get(BANK, ({ request }) => {
        const params = new URL(request.url).searchParams;
        if (params.get("limit") === "48") {
          queries.push({ status: params.get("status"), view: params.get("view") });
        }
        return HttpResponse.json({ items: [bankRow()], total: 1, total_all: 1, offset: 0, limit: 48 });
      }),
    );
    renderWithProviders(<ReviewPage />);
    await screen.findByRole("region", { name: /waiting for review/i });
    await userEvent.selectOptions(screen.getByLabelText(/filter by status/i), "failed");
    await waitFor(() =>
      expect(queries).toContainEqual({ status: "failed", view: null }),
    );
  });

  test("the bank section defaults to the needs-attention view", async () => {
    const queries: Array<{ status: string | null; view: string | null }> = [];
    server.use(
      http.get(BANK, ({ request }) => {
        const params = new URL(request.url).searchParams;
        // Only the section's page-sized query (the header probe shares the URL).
        if (params.get("limit") === "48") {
          queries.push({ status: params.get("status"), view: params.get("view") });
        }
        return HttpResponse.json({ items: [bankRow()], total: 1, total_all: 1, offset: 0, limit: 48 });
      }),
    );
    renderWithProviders(<ReviewPage />);
    await screen.findByRole("region", { name: /waiting for review/i });
    expect(queries[0]).toEqual({ status: null, view: "active" });
    // The default option is selected — resolved rows are out of the default view.
    expect(screen.getByLabelText(/filter by status/i)).toHaveValue("");
    expect(
      screen.getByRole("option", { name: /needs attention/i, selected: true }),
    ).toBeInTheDocument();
  });

  test("selecting All fetches every status — neither status nor active view", async () => {
    const queries: Array<{ status: string | null; view: string | null }> = [];
    server.use(
      http.get(BANK, ({ request }) => {
        const params = new URL(request.url).searchParams;
        if (params.get("limit") === "48") {
          queries.push({ status: params.get("status"), view: params.get("view") });
        }
        return HttpResponse.json({ items: [bankRow()], total: 1, total_all: 1, offset: 0, limit: 48 });
      }),
    );
    renderWithProviders(<ReviewPage />);
    await screen.findByRole("region", { name: /waiting for review/i });
    await userEvent.selectOptions(screen.getByLabelText(/filter by status/i), "all");
    await waitFor(() => expect(queries).toContainEqual({ status: null, view: null }));
  });

  test("bank_status=all in the URL restores the All view", async () => {
    const queries: Array<{ status: string | null; view: string | null }> = [];
    server.use(
      http.get(BANK, ({ request }) => {
        const params = new URL(request.url).searchParams;
        if (params.get("limit") === "48") {
          queries.push({ status: params.get("status"), view: params.get("view") });
        }
        return HttpResponse.json({ items: [bankRow()], total: 1, total_all: 1, offset: 0, limit: 48 });
      }),
    );
    renderWithProviders(<ReviewPage />, { route: "/review?bank_status=all" });
    // The section renames itself for the wider view — resolved rows are not
    // "waiting for review", and the heading must not claim they are.
    const section = await screen.findByRole("region", { name: /all imports/i });
    expect(
      within(section).getByRole("heading", { name: /all imports · 1/i }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("region", { name: /waiting for review/i }),
    ).not.toBeInTheDocument();
    expect(queries[0]).toEqual({ status: null, view: null });
    expect(screen.getByLabelText(/filter by status/i)).toHaveValue("all");
  });

  test("a specific status filter renames the section to that status", async () => {
    server.use(
      http.get(BANK, () =>
        HttpResponse.json({
          items: [bankRow({ status: "done", album_id: 7 })],
          total: 1,
          total_all: 1,
          offset: 0,
          limit: 48,
        }),
      ),
    );
    renderWithProviders(<ReviewPage />, { route: "/review?bank_status=done" });
    const section = await screen.findByRole("region", { name: /^imported$/i });
    expect(
      within(section).getByRole("heading", { name: /imported · 1/i }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("region", { name: /waiting for review/i }),
    ).not.toBeInTheDocument();
  });

  test("row Ignore posts the decision; a 409 raises the conflict toast AND refetches the list", async () => {
    let body: unknown = null;
    let listGets = 0;
    server.use(
      http.get(BANK, ({ request }) => {
        // Count only the section's page-sized list query (the header's limit-1
        // probe shares the endpoint).
        if (new URL(request.url).searchParams.get("limit") === "48") listGets += 1;
        return HttpResponse.json({ items: [bankRow()], total: 1, total_all: 1, offset: 0, limit: 48 });
      }),
      http.post(`${O}/api/bank/:itemId/decision`, async ({ request }) => {
        body = await request.json();
        return HttpResponse.json({ detail: "row is queued; decisions need one of [...]" }, { status: 409 });
      }),
    );
    renderWithProviders(<ReviewPage />);
    const section = await screen.findByRole("region", { name: /waiting for review/i });
    const getsBeforeIgnore = listGets;
    await userEvent.click(within(section).getByRole("button", { name: /ignore album x/i }));
    await waitFor(() => expect(toastError).toHaveBeenCalledWith(expect.stringMatching(/row is queued/)));
    expect(body).toEqual({ action: "ignore" });
    // The user-visible recovery: a 409 means the row changed state elsewhere,
    // so the stale list must refetch itself (the hooks' onSettled invalidation).
    await waitFor(() => expect(listGets).toBeGreaterThan(getsBeforeIgnore));
  });

  test("bulk-ignore posts the selected ids", async () => {
    let body: unknown = null;
    server.use(
      http.get(BANK, () =>
        HttpResponse.json({
          items: [bankRow(), bankRow({ id: "b2", album: "Album Y" })],
          total: 2, total_all: 2, offset: 0, limit: 48,
        }),
      ),
      http.post(`${O}/api/bank/bulk-ignore`, async ({ request }) => {
        body = await request.json();
        return HttpResponse.json({ ignored: 2 });
      }),
    );
    renderWithProviders(<ReviewPage />);
    const section = await screen.findByRole("region", { name: /waiting for review/i });
    await userEvent.click(within(section).getByRole("checkbox", { name: /select album x/i }));
    await userEvent.click(within(section).getByRole("checkbox", { name: /select album y/i }));
    await userEvent.click(within(section).getByRole("button", { name: /ignore selected \(2\)/i }));
    await waitFor(() => expect(body).toEqual({ ids: ["b1", "b2"] }));
  });

  test("the header Select all ticks every eligible row, skipping applying", async () => {
    server.use(
      http.get(BANK, () =>
        HttpResponse.json({
          items: [
            bankRow(),
            bankRow({ id: "b2", album: "Album Y", status: "failed", error: "boom" }),
            bankRow({ id: "b3", album: "Album Z", status: "applying" }),
          ],
          total: 3,
          total_all: 3,
          offset: 0,
          limit: 48,
        }),
      ),
    );
    renderWithProviders(<ReviewPage />);
    const section = await screen.findByRole("region", { name: /waiting for review/i });
    await userEvent.click(within(section).getByRole("checkbox", { name: /select all/i }));
    expect(within(section).getByRole("checkbox", { name: /select album x/i })).toBeChecked();
    expect(within(section).getByRole("checkbox", { name: /select album y/i })).toBeChecked();
    // The applying row carries no checkbox — it can't be acted on.
    expect(
      within(section).queryByRole("checkbox", { name: /select album z/i }),
    ).not.toBeInTheDocument();
    // Only the two eligible rows count toward the bulk actions.
    expect(
      within(section).getByRole("button", { name: /delete selected \(2\)/i }),
    ).toBeInTheDocument();
  });

  test("Delete selected confirms, then bulk-deletes the selected ids", async () => {
    let body: unknown = null;
    server.use(
      http.get(BANK, () =>
        HttpResponse.json({
          items: [bankRow(), bankRow({ id: "b2", album: "Album Y" })],
          total: 2,
          total_all: 2,
          offset: 0,
          limit: 48,
        }),
      ),
      http.post(`${O}/api/bank/bulk-delete`, async ({ request }) => {
        body = await request.json();
        return HttpResponse.json({ deleted: 2 });
      }),
    );
    renderWithProviders(<ReviewPage />);
    const section = await screen.findByRole("region", { name: /waiting for review/i });
    await userEvent.click(within(section).getByRole("checkbox", { name: /select album x/i }));
    await userEvent.click(within(section).getByRole("checkbox", { name: /select album y/i }));
    await userEvent.click(
      within(section).getByRole("button", { name: /delete selected \(2\)/i }),
    );
    // AlertDialog confirm step — removing forfeits the banked candidates.
    await userEvent.click(await screen.findByRole("button", { name: /^remove$/i }));
    await waitFor(() => expect(body).toEqual({ ids: ["b1", "b2"] }));
  });

  test("an applying bank row has no checkbox", async () => {
    server.use(
      http.get(BANK, () =>
        HttpResponse.json({
          items: [bankRow({ id: "b3", album: "Album Z", status: "applying" })],
          total: 1,
          total_all: 1,
          offset: 0,
          limit: 48,
        }),
      ),
    );
    renderWithProviders(<ReviewPage />);
    const section = await screen.findByRole("region", { name: /waiting for review/i });
    expect(within(section).getByText("Album Z")).toBeInTheDocument();
    expect(
      within(section).queryByRole("checkbox", { name: /select album z/i }),
    ).not.toBeInTheDocument();
  });

  test("Remove confirms, then deletes the row", async () => {
    let deleted = false;
    server.use(
      http.get(BANK, () => HttpResponse.json({ items: [bankRow()], total: 1, total_all: 1, offset: 0, limit: 48 })),
      http.delete(`${O}/api/bank/:itemId`, () => {
        deleted = true;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    renderWithProviders(<ReviewPage />);
    const section = await screen.findByRole("region", { name: /waiting for review/i });
    await userEvent.click(within(section).getByRole("button", { name: /remove album x/i }));
    // AlertDialog confirm step — removing forfeits the banked candidates.
    await userEvent.click(await screen.findByRole("button", { name: /^remove$/i }));
    await waitFor(() => expect(deleted).toBe(true));
  });

  test("bank actions stay ENABLED while an import runs (store writes need no slot)", async () => {
    server.use(
      http.get(ACTIVE, () =>
        HttpResponse.json({ active: true, job_id: "j1", origin: "manual", needs_review_count: 0 }),
      ),
      http.get(BANK, () => HttpResponse.json({ items: [bankRow()], total: 1, total_all: 1, offset: 0, limit: 48 })),
    );
    renderWithProviders(<ReviewPage />);
    const section = await screen.findByRole("region", { name: /waiting for review/i });
    expect(within(section).getByRole("button", { name: /ignore album x/i })).toBeEnabled();
  });

  test("the sweep banner shows counters and Pause posts the endpoint", async () => {
    let paused = false;
    server.use(
      http.get(ACTIVE, () =>
        HttpResponse.json({
          active: true, job_id: "s1", origin: "sweep", needs_review_count: 0,
          sweep: { processed: 412, auto_applied: 268, banked: 144, skipped_known: 9, current_folder: "/library/Adele/21", paused: false },
        }),
      ),
      http.post(`${O}/api/import/s1/pause`, () => {
        paused = true;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    renderWithProviders(<ReviewPage />);
    // Other ambient role="status" regions exist (the empty state) — anchor on
    // the banner's counter text, then assert on its enclosing status region.
    await screen.findByText(/412 processed/);
    const banner = screen
      .getAllByRole("status")
      .find((el) => (el.textContent ?? "").includes("412 processed"));
    expect(banner).toBeDefined();
    expect(banner).toHaveTextContent(/412 processed/);
    expect(banner).toHaveTextContent(/144 banked/);
    expect(banner).toHaveTextContent(/Now: 21/); // current folder's last segment
    const pause = screen.getByRole("button", { name: /pause/i });
    // It swallows the repeat click while `aria-disabled` instead of disabling
    // (keyboard focus stays on it), so it needs the app's dimming recipe —
    // `disabled:opacity-50` never matches an aria-disabled control, and this
    // was the one site in the app that looked pressable while inert.
    expect(pause).toHaveClass("aria-disabled:opacity-50");
    await userEvent.click(pause);
    await waitFor(() => expect(paused).toBe(true));
  });

  test("a sweep never engages the 1s job poll — the banner rides the active probe", async () => {
    // A sweep's `albums` feed stays empty by design (decisions are banked),
    // so polling full job state every second for the whole sweep would only
    // ever compute decisions=[]. The job query must stay disabled.
    let jobGets = 0;
    server.use(
      http.get(ACTIVE, () =>
        HttpResponse.json({
          active: true, job_id: "s1", origin: "sweep", needs_review_count: 0,
          sweep: { processed: 7, auto_applied: 5, banked: 2, skipped_known: 0, current_folder: null, paused: false },
        }),
      ),
      http.get(JOB, () => {
        jobGets += 1;
        return HttpResponse.json({
          job_id: "s1",
          phase: "scanning",
          progress: { applied: 5, needs_review: 0, skipped: 0 },
          albums: [],
          summary: null,
          error: null,
          origin: "sweep",
          set_aside: 0,
        });
      }),
    );
    renderWithProviders(<ReviewPage />);
    await screen.findByText(/7 processed/);
    // Give a would-be job fetch a beat to reach MSW before asserting silence.
    await new Promise((r) => setTimeout(r, 50));
    expect(jobGets).toBe(0);
  });

  test("a bank_apply import never lights up the decision section (ghost banner)", async () => {
    // A background bank apply is fully unattended — it never parks a live
    // decision, so its job feed must not surface as "Needs your decision".
    let jobGets = 0;
    server.use(
      http.get(ACTIVE, () =>
        HttpResponse.json({ active: true, job_id: "j1", origin: "bank_apply", needs_review_count: 0 }),
      ),
      http.get(JOB, () => {
        jobGets += 1;
        return HttpResponse.json({
          job_id: "j1",
          phase: "reviewing",
          progress: { applied: 0, needs_review: 0, skipped: 0 },
          albums: [album({ index: 0, album: "Echoes", status: "needs_dup_resolution" })],
          summary: null,
          error: null,
          origin: "bank_apply",
          set_aside: 0,
        });
      }),
    );
    renderWithProviders(<ReviewPage />);
    await screen.findByText(/nothing to review/i);
    // Give a would-be job fetch a beat to reach MSW before asserting silence.
    await new Promise((r) => setTimeout(r, 50));
    expect(
      screen.queryByRole("region", { name: /needs your decision/i }),
    ).not.toBeInTheDocument();
    expect(jobGets).toBe(0);
  });

  test("the header meta counts bank rows awaiting review", async () => {
    server.use(
      http.get(BANK, ({ request }) => {
        const status = new URL(request.url).searchParams.get("status");
        return HttpResponse.json(
          status === "needs_review"
            ? { items: [bankRow()], total: 3, total_all: 3, offset: 0, limit: 1 }
            : { items: [bankRow()], total: 5, total_all: 5, offset: 0, limit: 48 },
        );
      }),
    );
    renderWithProviders(<ReviewPage />);
    expect(await screen.findByText(/3 awaiting a decision/i)).toBeInTheDocument();
  });

  describe("sweep recap", () => {
    const doneSweep = {
      active: false,
      origin: "manual",
      needs_review_count: 0,
      last_sweep: {
        job_id: "s9",
        processed: 12,
        auto_applied: 9,
        banked: 3,
        skipped_known: 2,
        paused: false,
      },
    };

    beforeEach(() => {
      localStorage.clear();
    });

    test("a finished sweep leaves a recap strip with the imported count", async () => {
      server.use(http.get(ACTIVE, () => HttpResponse.json(doneSweep)));
      renderWithProviders(<ReviewPage />);
      const strip = await screen.findByText(/sweep finished/i);
      expect(strip).toHaveTextContent(
        "Sweep finished: 12 processed · 9 imported · 3 banked · 2 already known.",
      );
      expect(screen.getByRole("link", { name: /view run/i })).toHaveAttribute(
        "href",
        "/import?job=s9",
      );
    });

    test("zero already-known drops that segment; a paused sweep says so", async () => {
      server.use(
        http.get(ACTIVE, () =>
          HttpResponse.json({
            ...doneSweep,
            last_sweep: { ...doneSweep.last_sweep, skipped_known: 0, paused: true },
          }),
        ),
      );
      renderWithProviders(<ReviewPage />);
      const strip = await screen.findByText(/sweep paused/i);
      expect(strip).toHaveTextContent("Sweep paused: 12 processed · 9 imported · 3 banked.");
      expect(strip).not.toHaveTextContent(/already known/i);
      expect(strip).toHaveTextContent(/resume by sweeping the same folder again/i);
    });

    test("dismiss hides the recap and persists across remounts", async () => {
      server.use(http.get(ACTIVE, () => HttpResponse.json(doneSweep)));
      const first = renderWithProviders(<ReviewPage />);
      await screen.findByText(/sweep finished/i);
      await userEvent.click(screen.getByRole("button", { name: /dismiss sweep recap/i }));
      expect(screen.queryByText(/sweep finished/i)).not.toBeInTheDocument();
      first.unmount();
      renderWithProviders(<ReviewPage />);
      // The page settles without the recap returning (same job id).
      await screen.findByText(/awaiting a decision/i);
      expect(screen.queryByText(/sweep finished/i)).not.toBeInTheDocument();
    });

    test("a NEW sweep's recap shows despite an older dismissal", async () => {
      localStorage.setItem("musicdrop.sweepRecapDismissed", "s8");
      server.use(http.get(ACTIVE, () => HttpResponse.json(doneSweep)));
      renderWithProviders(<ReviewPage />);
      expect(await screen.findByText(/sweep finished/i)).toBeInTheDocument();
    });

    test("while a sweep RUNS the live banner owns the slot — the predecessor's recap stays hidden", async () => {
      // The probe carries BOTH: a live sweep and the previous run's recap still
      // parked in the registry slot. Only the live banner may render — a leaked
      // recap would show the PREDECESSOR's counters (20/14/6/4) beside the live
      // 7/5/2, so the fixture keeps the two sets disjoint to make a leak visible.
      server.use(
        http.get(ACTIVE, () =>
          HttpResponse.json({
            active: true,
            job_id: "s1",
            origin: "sweep",
            needs_review_count: 0,
            sweep: {
              processed: 7,
              auto_applied: 5,
              banked: 2,
              skipped_known: 0,
              current_folder: null,
              paused: false,
            },
            last_sweep: {
              job_id: "s0",
              processed: 20,
              auto_applied: 14,
              banked: 6,
              skipped_known: 4,
              paused: true,
            },
          }),
        ),
      );
      renderWithProviders(<ReviewPage />);
      await screen.findByText(/7 processed/);
      // Both recap dialects — the ternary emits "Sweep finished" or, for a
      // paused predecessor like this one, "Sweep paused".
      expect(screen.queryByText(/sweep (finished|paused)/i)).not.toBeInTheDocument();
      expect(screen.queryByText(/20 processed/)).not.toBeInTheDocument();
    });
  });
});
