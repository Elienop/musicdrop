import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";
import { beforeEach, describe, expect, test, vi } from "vitest";

import { ReviewPage } from "@/pages/review/ReviewPage";
import { renderWithProviders } from "@/test/render";
import { server } from "@/test/msw-server";

// Spy on navigation while keeping the real MemoryRouter (the render harness
// imports it from the same module) — only `useNavigate` is swapped.
const { mockNavigate } = vi.hoisted(() => ({ mockNavigate: vi.fn() }));
vi.mock("react-router", async (importOriginal) => ({
  ...(await importOriginal<typeof import("react-router")>()),
  useNavigate: () => mockNavigate,
}));

const O = window.location.origin;
const ACTIVE = `${O}/api/imports/active`;
const JOB = `${O}/api/import/:jobId`;
const ITEMS = `${O}/api/acquisition/inbox/items`;
const STATUS = `${O}/api/acquisition/status`;
const DUPES = `${O}/api/duplicates`;
const IMPORT_ITEM = `${O}/api/acquisition/inbox/items/import`;
const REVIEW_ALL = `${O}/api/acquisition/review-inbox`;

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
    // Idle defaults for every probe the page polls, so each test only overrides
    // what it asserts (the MSW server errors on an unhandled request).
    server.use(
      http.get(ACTIVE, () => HttpResponse.json(idleActive)),
      http.get(ITEMS, () => HttpResponse.json({ items: [] })),
      http.get(STATUS, () => HttpResponse.json(idleStatus)),
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
  });

  test("inbox rows render name, set-aside tag, and track count", async () => {
    server.use(
      http.get(ITEMS, () =>
        HttpResponse.json({
          items: [
            { name: "Lost Tapes", mtime: 2, size: 10, track_count: 9, outcome: "set_aside", source: "slskd" },
            { name: "Demo 99", mtime: 1, size: 5, track_count: 1, outcome: null, source: "slskd" },
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
          items: [{ name: "Lost Tapes", mtime: 1, size: 10, track_count: 9, outcome: null, source: "slskd" }],
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
          items: [{ name: "Lost Tapes", mtime: 1, size: 10, track_count: 9, outcome: null, source: "slskd" }],
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

  test("inbox actions are disabled while an import is running", async () => {
    server.use(
      http.get(ACTIVE, () =>
        HttpResponse.json({ active: true, job_id: "j1", origin: "manual", needs_review_count: 0 }),
      ),
      http.get(ITEMS, () =>
        HttpResponse.json({
          items: [{ name: "Lost Tapes", mtime: 1, size: 10, track_count: 9, outcome: null, source: "slskd" }],
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
});
