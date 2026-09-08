import { describe, expect, test } from "vitest";

import type { ImportJobState } from "@/api/useImport";
import {
  ELAPSED_AFTER_S,
  announceMessage,
  elapsedLabel,
  spokenElapsed,
} from "@/pages/import/importStatus";

function job(overrides: Partial<ImportJobState> = {}): ImportJobState {
  return {
    job_id: "j",
    phase: "reviewing",
    progress: { applied: 0, needs_review: 0, skipped: 0, not_landed: 0 },
    albums: [],
    summary: null,
    error: null,
    origin: "manual",
    set_aside: 0,
    elapsed_seconds: 0,
    awaiting_decision: false,
    ...overrides,
  };
}

/** A sweep-origin job: no feed rows (sweeps never build `albums`), zeroed
 * progress — the `sweep` counters are the entire progress surface. */
function sweepState(overrides: Partial<ImportJobState> = {}): ImportJobState {
  return {
    job_id: "s",
    phase: "scanning",
    progress: { applied: 0, needs_review: 0, skipped: 0, not_landed: 0 },
    albums: [],
    summary: null,
    error: null,
    origin: "sweep",
    set_aside: 0,
    elapsed_seconds: 0,
    // A sweep is unattended by definition — it never blocks on a person.
    awaiting_decision: false,
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
      data: job({
        progress: { applied: 2, needs_review: 1, skipped: 1, not_landed: 0 },
      }),
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
        progress: { applied: 1, needs_review: 0, skipped: 0, not_landed: 0 },
        albums: [
          {
            index: 0,
            folder: "/music/incoming/dup",
            artist: "X",
            album: "Y",
            recommendation: "strong",
            confidence: 99,
            status: "needs_dup_resolution",
            did_not_land: false,
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
          progress: { applied: 3, needs_review: 0, skipped: 1, not_landed: 0 },
        }),
      }),
    ).toMatch(/import complete.*imported 3.*skipped 1/i);
  });

  // B3: the visible elapsed value is not in a live region, so without this the
  // announced string is byte-identical on every poll and a screen-reader user
  // is told nothing for the whole ten minutes.
  test("a long run announces how long it has been going", () => {
    const speak = (data: ImportJobState) =>
      announceMessage({ isPending: false, isError: false, notFound: false, data });
    // Under a minute: nothing added — the string is exactly what it was.
    expect(speak(job({ phase: "scanning", albums: [], elapsed_seconds: 59 }))).toBe(
      "Scanning the folder for albums.",
    );
    expect(speak(job({ phase: "scanning", albums: [], elapsed_seconds: 132 }))).toBe(
      "Scanning the folder for albums. Running for 2 minutes.",
    );
    // Minute granularity is what makes the announcer's identical-string de-dup
    // cap this at one announcement a minute: a second's worth of poll does not
    // change the string, a minute's does.
    expect(speak(job({ elapsed_seconds: 132 }))).toBe(
      speak(job({ elapsed_seconds: 145 })),
    );
    expect(speak(job({ elapsed_seconds: 132 }))).not.toBe(
      speak(job({ elapsed_seconds: 195 })),
    );
    // But NOT while the run waits on a person. `role="status"` is atomic, so
    // each minute tick re-reads the whole string, and nothing else can change
    // — a 20-minute decision became 20 full re-reads asserting activity. The
    // string goes static instead, and the announcer's de-dup swallows the poll.
    const parked = (seconds: number) =>
      speak(
        job({
          progress: { applied: 1, needs_review: 1, skipped: 0, not_landed: 0 },
          elapsed_seconds: seconds,
          awaiting_decision: true,
        }),
      );
    expect(parked(600)).toBe("Imported 1. 1 album awaiting review.");
    expect(parked(1200)).toBe(parked(600));
    // And the finished summary, in the past tense.
    expect(
      speak(
        job({
          phase: "done",
          progress: { applied: 3, needs_review: 0, skipped: 1, not_landed: 0 },
          elapsed_seconds: 840,
        }),
      ),
    ).toBe("Import complete. Imported 3, skipped 1. Took 14 minutes.");
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

describe("elapsedLabel", () => {
  // Below the threshold the status line must read exactly as it did before,
  // so a fast import gains no extra text at all.
  test.each([0, 1, ELAPSED_AFTER_S - 1])("is null at %i seconds", (seconds) => {
    expect(elapsedLabel(seconds)).toBeNull();
  });

  // Numbers and units, no sentence — the owner's "no long sentences unecessary".
  // The inner space is a non-breaking one, so the two halves never wrap apart.
  test.each([
    [ELAPSED_AFTER_S, "30s"],
    [59, "59s"],
    [60, "1m"],
    [119, "1m\u00a059s"],
    [600, "10m"],
    [612, "10m\u00a012s"],
    [3599, "59m\u00a059s"],
    [3600, "1h"],
    [3700, "1h\u00a01m"],
    [7500, "2h\u00a05m"],
  ])("renders %i seconds as %s", (seconds, expected) => {
    expect(elapsedLabel(seconds)).toBe(expected);
  });

  // Below the hour it must VISIBLY move, or a frozen line is indistinguishable
  // from the wedged page the number exists to rule out.
  test("changes every second below the hour", () => {
    expect(elapsedLabel(612)).not.toBe(elapsedLabel(613));
  });

  // Unreachable through the typed contract, but untyped fixtures omit the field
  // and this used to render "NaNh NaNm" on screen.
  test.each([undefined, Number.NaN, Number.POSITIVE_INFINITY])(
    "is null for the non-finite %s rather than NaN text",
    (seconds) => {
      expect(elapsedLabel(seconds as unknown as number)).toBeNull();
    },
  );
});

describe("spokenElapsed", () => {
  // Minute granularity: the announcer de-dups identical strings, so this caps
  // the spoken update at one a minute instead of one a second.
  test.each([0, 59])("says nothing under a minute (%i)", (seconds) => {
    expect(spokenElapsed(seconds)).toBeNull();
  });

  // Words, not "12m" — a screen reader reads that as a letter.
  test.each([
    [60, "1 minute"],
    [125, "2 minutes"],
    [3600, "1 hour"],
    [3660, "1 hour 1 minute"],
    [7500, "2 hours 5 minutes"],
  ])("speaks %i seconds as %s", (seconds, expected) => {
    expect(spokenElapsed(seconds)).toBe(expected);
  });
});
