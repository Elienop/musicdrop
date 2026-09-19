import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import type { LyricsBackfillStatus, LyricsCoverage } from "@/api/useLyricsBackfill";
import { LyricsBackfillPanel } from "@/pages/settings/LyricsBackfillPanel";

const startMock = vi.fn();
const baseCoverage: LyricsCoverage = {
  total: 10, with_lyrics: 7, instrumental: 0, checked_no_lyrics: 2, percent: 70,
};
let coverageData: LyricsCoverage = baseCoverage;
const baseStatus: LyricsBackfillStatus = {
  phase: "idle", job_id: null, total: 0, processed: 0, found: 0,
  instrumental: 0, not_found: 0, failed: 0, skipped: 0, current: null,
  writes_enabled: false, error: null, album_id: null, scope_label: "library",
};
let statusData: LyricsBackfillStatus = baseStatus;

vi.mock("@/api/useLyricsBackfill", () => ({
  useLyricsCoverage: () => ({ data: coverageData }),
  useLyricsBackfillStatus: () => ({ data: statusData }),
  useStartLyricsBackfill: () => ({ mutate: startMock, isPending: false }),
  useStopLyricsBackfill: () => ({ mutate: vi.fn(), isPending: false }),
}));

function renderPanel() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <LyricsBackfillPanel />
    </QueryClientProvider>,
  );
}

describe("LyricsBackfillPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    statusData = baseStatus;
    coverageData = baseCoverage;
  });

  it("shows coverage and starts a backfill", async () => {
    const user = userEvent.setup();
    await renderPanel();
    expect(screen.getByText(/70%/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /backfill missing lyrics/i }));
    expect(startMock).toHaveBeenCalledTimes(1);
  });

  it("shows the coverage breakdown", async () => {
    await renderPanel();
    expect(screen.getByText(/70%/)).toBeInTheDocument();
    expect(screen.getByText(/2 with no lyrics found/i)).toBeInTheDocument();
    // 10 - 7 with lyrics - 2 already searched
    expect(screen.getByText(/1 left to check/i)).toBeInTheDocument();
    // A library with no instrumentals doesn't advertise the empty bucket.
    expect(screen.queryByText(/instrumental/i)).not.toBeInTheDocument();
  });

  it("gives instrumentals their own bucket and drops them from left-to-check", async () => {
    coverageData = {
      total: 10, with_lyrics: 4, instrumental: 3, checked_no_lyrics: 2, percent: 40,
    };
    await renderPanel();
    expect(screen.getByText(/3 instrumental/i)).toBeInTheDocument();
    // An instrumental is an answer, not a gap: 10 - 4 - 3 - 2 = 1 still to search.
    expect(screen.getByText(/1 left to check/i)).toBeInTheDocument();
  });

  it("passes recheckMisses when the checkbox is ticked", async () => {
    const user = userEvent.setup();
    await renderPanel();
    await user.click(
      screen.getByRole("checkbox", { name: /re-check tracks already found to have no lyrics/i }),
    );
    await user.click(screen.getByRole("button", { name: /backfill missing lyrics/i }));
    expect(startMock).toHaveBeenCalledWith({ recheckMisses: true });
  });

  // The two things the swap from a native <input type="checkbox"> had to keep:
  // the label text is the control's ONE accessible name, and clicking the words
  // toggles it. The keyboard half is the primitive's own contract, pinned here
  // because this is the call site a user reaches from Settings.
  it("the recheck toggle: label clicks toggle it, Space toggles it", async () => {
    const user = userEvent.setup();
    await renderPanel();
    const label = "Re-check tracks already found to have no lyrics";
    const box = screen.getByRole("checkbox", { name: label });
    expect(screen.getAllByText(label)).toHaveLength(1);
    expect(box).not.toBeChecked();
    // The row still claims a whole line of the wrapping flex container, as it
    // did when `w-full` sat on the <label> that wrapped the native input. jsdom
    // has no layout engine, so the class is what a test can hold; the measured
    // line break is in the branch's browser pass.
    expect(box.parentElement).toHaveClass("w-full");
    // This label wraps to two lines at 360, and `items-center` put the box on
    // the boundary between them — 10px below the first line's centre, measured.
    // `items-start` plus (line-height 20 − size-4 16) / 2 = 2px puts it back on
    // the first line, and carries the 24px tap target with it.
    expect(box.parentElement).toHaveClass("items-start");
    expect(box.parentElement).not.toHaveClass("items-center");
    expect(box).toHaveClass("mt-0.5");

    await user.click(screen.getByText(label));
    expect(box).toBeChecked();

    box.focus();
    await user.keyboard("{ }");
    expect(box).not.toBeChecked();
  });

  it("shows a done result line and keeps the Backfill button", async () => {
    statusData = {
      ...baseStatus, phase: "done", found: 5, instrumental: 3, not_found: 2, failed: 1, skipped: 4,
    };
    await renderPanel();
    expect(screen.getByText(/found 5/i)).toBeInTheDocument();
    expect(screen.getByText(/none 2/i)).toBeInTheDocument();
    // Instrumentals are counted apart from the misses they'd otherwise inflate.
    expect(screen.getByText(/instrumental 3/i)).toBeInTheDocument();
    expect(screen.getByText(/skipped 4/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /backfill missing lyrics/i })).toBeInTheDocument();
  });

  it("counts instrumentals in the live feed while a library sweep runs", async () => {
    statusData = {
      ...baseStatus, phase: "running", job_id: "L1", processed: 6, total: 20,
      found: 2, instrumental: 3, not_found: 1, failed: 0,
    };
    await renderPanel();
    expect(
      screen.getByText(/Backfilling… 6 \/ 20 · found 2 · instrumental 3 · none 1 · failed 0/),
    ).toBeInTheDocument();
  });

  // Tracks flagged instrumental on an earlier run are skipped by every sweep,
  // so without a skipped counter the tally silently loses them: the outcomes
  // have to add back up to `processed` or the work reads as lost.
  it("accounts for every processed track, skips included", async () => {
    statusData = {
      ...baseStatus, phase: "running", job_id: "L1", processed: 10, total: 20,
      found: 2, instrumental: 1, not_found: 1, failed: 0, skipped: 6,
    };
    await renderPanel();
    expect(screen.getByText(/skipped 6/i)).toBeInTheDocument();
  });

  it("shows a stopped result line", async () => {
    statusData = { ...baseStatus, phase: "stopped", found: 3, not_found: 1, failed: 0 };
    await renderPanel();
    expect(screen.getByText(/stopped/i)).toBeInTheDocument();
    expect(screen.getByText(/found 3/i)).toBeInTheDocument();
  });

  it("surfaces the job error on a failed backfill", async () => {
    statusData = { ...baseStatus, phase: "failed", error: "library locked", found: 1, not_found: 0, failed: 4 };
    await renderPanel();
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent(/library locked/i);
    expect(screen.getByRole("button", { name: /backfill missing lyrics/i })).toBeInTheDocument();
  });

  it("disables Backfill and explains while an album fetch is running", async () => {
    statusData = {
      ...baseStatus, phase: "running", album_id: 7, scope_label: "Radiohead — In Rainbows",
      processed: 1, total: 3,
    };
    await renderPanel();
    expect(screen.getByRole("button", { name: /backfill missing lyrics/i })).toBeDisabled();
    expect(screen.getByText(/a lyrics fetch is in progress/i)).toBeInTheDocument();
    // No library live feed for an album-scoped job.
    expect(screen.queryByText(/Backfilling…/)).not.toBeInTheDocument();
  });
});
