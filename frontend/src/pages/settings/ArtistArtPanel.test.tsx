import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ArtistArtBackfillStatus } from "@/api/useArtistArt";
import { ArtistArtPanel } from "@/pages/settings/ArtistArtPanel";

const setEnabled = vi.fn();
const startBackfill = vi.fn();
const stopBackfill = vi.fn();

let enabledValue = false;
let statusValue: ArtistArtBackfillStatus = {
  phase: "idle",
  job_id: null,
  total: 0,
  processed: 0,
  written: 0,
  skipped: 0,
  failed: 0,
  current: null,
  error: null,
  artist: null,
  scope_label: "library",
};

vi.mock("@/api/useArtistArt", () => ({
  useArtistArtSettings: () => ({ data: { enabled: enabledValue }, isPending: false }),
  useSetArtistArtSettings: () => ({
    mutate: setEnabled,
    isPending: false,
    isError: false,
    error: null,
  }),
  useArtistArtBackfillStatus: () => ({ data: statusValue }),
  useStartArtistArtBackfill: () => ({
    mutate: startBackfill,
    isPending: false,
    isError: false,
    error: null,
  }),
  useStopArtistArtBackfill: () => ({ mutate: stopBackfill, isPending: false }),
}));

afterEach(() => {
  vi.clearAllMocks();
  enabledValue = false;
  statusValue = {
    phase: "idle",
    job_id: null,
    total: 0,
    processed: 0,
    written: 0,
    skipped: 0,
    failed: 0,
    current: null,
    error: null,
    artist: null,
    scope_label: "library",
  };
});

describe("ArtistArtPanel", () => {
  it("renders the toggle and flips it", () => {
    render(<ArtistArtPanel />);
    expect(
      screen.getByRole("region", { name: "Artist art for Plex" }),
    ).toBeInTheDocument();
    const toggle = screen.getByRole("switch", { name: /write artist art to library/i });
    expect(toggle).not.toBeChecked();
    fireEvent.click(toggle);
    expect(setEnabled).toHaveBeenCalledWith(true);
  });

  it("disables the backfill button when the toggle is off", () => {
    enabledValue = false;
    render(<ArtistArtPanel />);
    expect(screen.getByRole("button", { name: /write all to library/i })).toBeDisabled();
  });

  it("enables the backfill button and starts a run when the toggle is on", () => {
    enabledValue = true;
    render(<ArtistArtPanel />);
    const button = screen.getByRole("button", { name: /write all to library/i });
    expect(button).not.toBeDisabled();
    fireEvent.click(button);
    expect(startBackfill).toHaveBeenCalled();
  });

  it("shows running progress with a stop control", () => {
    enabledValue = true;
    statusValue = {
      ...statusValue,
      phase: "running",
      artist: null,
      scope_label: "library",
      total: 10,
      processed: 4,
      written: 3,
      current: "Pink Floyd",
    };
    render(<ArtistArtPanel />);
    expect(screen.getByText(/3 \/ 10/)).toBeInTheDocument();
    const stop = screen.getByRole("button", { name: /stop/i });
    fireEvent.click(stop);
    expect(stopBackfill).toHaveBeenCalled();
  });
});
