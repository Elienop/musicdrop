import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";

const startMock = vi.fn();
vi.mock("@/api/useLyricsBackfill", () => ({
  useLyricsCoverage: () => ({ data: { total: 10, with_lyrics: 7, percent: 70 } }),
  useLyricsBackfillStatus: () => ({ data: { phase: "idle", job_id: null, total: 0, processed: 0, found: 0, not_found: 0, failed: 0, skipped: 0, current: null, writes_enabled: false, error: null } }),
  useStartLyricsBackfill: () => ({ mutate: startMock, isPending: false }),
  useStopLyricsBackfill: () => ({ mutate: vi.fn(), isPending: false }),
}));

async function renderPanel() {
  const { LyricsBackfillPanel } = await import("@/pages/settings/LyricsBackfillPanel");
  return render(<LyricsBackfillPanel />);
}

describe("LyricsBackfillPanel", () => {
  beforeEach(() => vi.clearAllMocks());

  it("shows coverage and starts a backfill", async () => {
    const user = (await import("@testing-library/user-event")).default.setup();
    await renderPanel();
    expect(screen.getByText(/70%/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /backfill missing lyrics/i }));
    expect(startMock).toHaveBeenCalledTimes(1);
  });
});
