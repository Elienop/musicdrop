import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import type { LyricsBackfillStatus } from "@/api/useLyricsBackfill";

const startMock = vi.fn();
const baseStatus: LyricsBackfillStatus = {
  phase: "idle", job_id: null, total: 0, processed: 0, found: 0,
  not_found: 0, failed: 0, skipped: 0, current: null,
  writes_enabled: false, error: null,
};
let statusData: LyricsBackfillStatus = baseStatus;

vi.mock("@/api/useLyricsBackfill", () => ({
  useLyricsCoverage: () => ({ data: { total: 10, with_lyrics: 7, percent: 70 } }),
  useLyricsBackfillStatus: () => ({ data: statusData }),
  useStartLyricsBackfill: () => ({ mutate: startMock, isPending: false }),
  useStopLyricsBackfill: () => ({ mutate: vi.fn(), isPending: false }),
}));

async function renderPanel() {
  const { LyricsBackfillPanel } = await import("@/pages/settings/LyricsBackfillPanel");
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
  });

  it("shows coverage and starts a backfill", async () => {
    const user = (await import("@testing-library/user-event")).default.setup();
    await renderPanel();
    expect(screen.getByText(/70%/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /backfill missing lyrics/i }));
    expect(startMock).toHaveBeenCalledTimes(1);
  });

  it("shows a done result line and keeps the Backfill button", async () => {
    statusData = { ...baseStatus, phase: "done", found: 5, not_found: 2, failed: 1 };
    await renderPanel();
    expect(screen.getByText(/found 5/i)).toBeInTheDocument();
    expect(screen.getByText(/none 2/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /backfill missing lyrics/i })).toBeInTheDocument();
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
});
