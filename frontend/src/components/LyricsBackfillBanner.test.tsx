import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";

import type { LyricsBackfillStatus } from "@/api/useLyricsBackfill";

const baseStatus: LyricsBackfillStatus = {
  phase: "idle", job_id: null, total: 0, processed: 0, found: 0,
  not_found: 0, failed: 0, skipped: 0, current: null,
  writes_enabled: false, error: null,
};
let statusData: LyricsBackfillStatus = baseStatus;

vi.mock("@/api/useLyricsBackfill", () => ({
  useLyricsBackfillStatus: () => ({ data: statusData }),
}));

async function renderBanner() {
  const { LyricsBackfillBanner } = await import("@/components/LyricsBackfillBanner");
  return render(<LyricsBackfillBanner />);
}

describe("LyricsBackfillBanner", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    statusData = baseStatus;
  });

  it("renders nothing when idle", async () => {
    const { container } = await renderBanner();
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing when done or stopped", async () => {
    statusData = { ...baseStatus, phase: "done" };
    const { container } = await renderBanner();
    expect(container).toBeEmptyDOMElement();
  });

  it("shows progress while running", async () => {
    statusData = { ...baseStatus, phase: "running", processed: 3, total: 10 };
    await renderBanner();
    expect(screen.getByText(/3 \/ 10/)).toBeInTheDocument();
    expect(screen.getByRole("status")).toBeInTheDocument();
  });

  it("shows a failed note with role=alert", async () => {
    statusData = { ...baseStatus, phase: "failed", error: "library locked" };
    await renderBanner();
    expect(screen.getByRole("alert")).toHaveTextContent(/lyrics backfill failed/i);
  });
});
