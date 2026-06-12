import { describe, expect, test } from "vitest";

import type { ImportJobState } from "@/api/useImport";
import { announceMessage } from "@/pages/import/importStatus";

function job(overrides: Partial<ImportJobState> = {}): ImportJobState {
  return {
    job_id: "j",
    phase: "reviewing",
    progress: { applied: 0, needs_review: 0, skipped: 0 },
    albums: [],
    summary: null,
    error: null,
    origin: "manual",
    set_aside: 0,
    ...overrides,
  };
}

/** A sweep-origin job: no feed rows (sweeps never build `albums`), zeroed
 * progress — the `sweep` counters are the entire progress surface. */
function sweepState(overrides: Partial<ImportJobState> = {}): ImportJobState {
  return {
    job_id: "s",
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

describe("announceMessage", () => {
  test("loading / not-found / error / failed read distinctly", () => {
    expect(
      announceMessage({
        isPending: true,
        isError: false,
        notFound: false,
        data: undefined,
      }),
    ).toMatch(/loading/i);
    expect(
      announceMessage({
        isPending: false,
        isError: false,
        notFound: true,
        data: undefined,
      }),
    ).toMatch(/gone/i);
    expect(
      announceMessage({
        isPending: false,
        isError: true,
        notFound: false,
        data: undefined,
      }),
    ).toMatch(/could not be loaded/i);
    expect(
      announceMessage({
        isPending: false,
        isError: false,
        notFound: false,
        data: job({ phase: "failed", error: "x" }),
      }),
    ).toMatch(/failed/i);
  });

  test("scanning-empty vs active count is verb-first", () => {
    expect(
      announceMessage({
        isPending: false,
        isError: false,
        notFound: false,
        data: job({ phase: "scanning", albums: [] }),
      }),
    ).toMatch(/scanning the folder/i);
    const active = announceMessage({
      isPending: false,
      isError: false,
      notFound: false,
      data: job({ progress: { applied: 2, needs_review: 1, skipped: 1 } }),
    });
    expect(active).toMatch(/imported 2/i);
    expect(active).toMatch(/skipped 1/i);
    expect(active).toMatch(/awaiting review/i);
  });

  test("announces a parked duplicate (a blocking prompt the user must clear)", () => {
    const m = announceMessage({
      isPending: false,
      isError: false,
      notFound: false,
      // `progress` has no duplicate counter — the announcer derives it from the
      // feed row, so a screen-reader user hears the worker is waiting on them.
      data: job({
        progress: { applied: 1, needs_review: 0, skipped: 0 },
        albums: [
          {
            index: 0,
            folder: "/music/incoming/dup",
            artist: "X",
            album: "Y",
            recommendation: "strong",
            confidence: 99,
            status: "needs_dup_resolution",
          },
        ],
      }),
    });
    expect(m).toMatch(/duplicate.*awaiting resolution/i);
  });

  test("done states the totals", () => {
    expect(
      announceMessage({
        isPending: false,
        isError: false,
        notFound: false,
        data: job({
          phase: "done",
          progress: { applied: 3, needs_review: 0, skipped: 1 },
        }),
      }),
    ).toMatch(/import complete.*imported 3.*skipped 1/i);
  });

  test("sweep jobs announce counters, not the feed", () => {
    const data = sweepState({
      phase: "scanning",
      sweep: {
        processed: 12,
        auto_applied: 8,
        banked: 4,
        skipped_known: 0,
        current_folder: "/in/x",
        paused: false,
      },
    });
    expect(
      announceMessage({ isPending: false, isError: false, notFound: false, data }),
    ).toBe("Sweeping. Processed 12, imported 8, banked 4.");
  });

  test("a finished sweep announces complete vs paused", () => {
    const done = sweepState({
      phase: "done",
      sweep: {
        processed: 30,
        auto_applied: 20,
        banked: 10,
        skipped_known: 0,
        current_folder: null,
        paused: false,
      },
    });
    expect(
      announceMessage({ isPending: false, isError: false, notFound: false, data: done }),
    ).toBe("Sweep complete. Processed 30, imported 20, banked 10.");
    const paused = sweepState({
      phase: "done",
      sweep: {
        processed: 5,
        auto_applied: 3,
        banked: 2,
        skipped_known: 0,
        current_folder: null,
        paused: true,
      },
    });
    expect(
      announceMessage({ isPending: false, isError: false, notFound: false, data: paused }),
    ).toBe("Sweep paused. Processed 5, imported 3, banked 2.");
  });
});
