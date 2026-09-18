import { describe, expect, test } from "vitest";

import type { ImportJobState, SweepStatus } from "@/api/useImport";
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
    progress: { applied: 0, needs_review: 0, skipped: 0, not_landed: 0, already_known: 0 },
    albums: [],
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
    progress: { applied: 0, needs_review: 0, skipped: 0, not_landed: 0, already_known: 0 },
    albums: [],
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
        progress: { applied: 2, needs_review: 1, skipped: 1, not_landed: 0, already_known: 0 },
      }),
    });
    expect(active).toMatch(/imported 2/i);
    expect(active).toMatch(/skipped 1/i);
    expect(active).toMatch(/awaiting review/i);
  });

  test("a live run owns the album that didn't land, in the terminal wording", () => {
    // A refused Replace leaves `applied` and joins `not_landed` while the run is
    // still going. Without this clause the one live region said "Imported 1."
    // over a feed row reading "Nothing was imported." — the whole announcement
    // is pinned, so a clause going missing or moving fails.
    expect(
      announceMessage({
        isPending: false,
        isError: false,
        notFound: false,
        data: job({
          progress: { applied: 1, needs_review: 0, skipped: 2, not_landed: 3, already_known: 0 },
        }),
      }),
    ).toBe("Imported 1. Skipped 2. 3 didn't land.");
    // Gated on itself, like every other clause: a clean run gains nothing.
    expect(
      announceMessage({
        isPending: false,
        isError: false,
        notFound: false,
        data: job({
          progress: { applied: 1, needs_review: 0, skipped: 0, not_landed: 0, already_known: 0 },
        }),
      }),
    ).toBe("Imported 1.");
  });

  test("announces a parked duplicate (a blocking prompt the user must clear)", () => {
    const m = announceMessage({
      isPending: false,
      isError: false,
      notFound: false,
      // `progress` has no duplicate counter — the announcer derives it from the
      // feed row, so a screen-reader user hears the worker is waiting on them.
      data: job({
        progress: { applied: 1, needs_review: 0, skipped: 0, not_landed: 0, already_known: 0 },
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
          progress: { applied: 3, needs_review: 0, skipped: 1, not_landed: 0, already_known: 0 },
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
    // But NOT while the announcement NAMES what the run waits on.
    // `role="status"` is atomic, so each minute tick re-reads the whole string,
    // and nothing else can change — a 20-minute decision became 20 full
    // re-reads asserting activity. The string goes static instead, and the
    // announcer's de-dup swallows the poll.
    const parked = (seconds: number) =>
      speak(
        job({
          progress: { applied: 1, needs_review: 1, skipped: 0, not_landed: 0, already_known: 0 },
          elapsed_seconds: seconds,
          awaiting_decision: true,
        }),
      );
    expect(parked(600)).toBe("Imported 1. 1 album awaiting review.");
    expect(parked(1200)).toBe(parked(600));
    // And the finished announcement, in the past tense.
    expect(
      speak(
        job({
          phase: "done",
          progress: { applied: 3, needs_review: 0, skipped: 1, not_landed: 0, already_known: 0 },
          elapsed_seconds: 840,
        }),
      ),
    ).toBe("Import complete. Imported 3, skipped 1. Took 14 minutes.");
    // A failure is a finish too: three seconds versus forty minutes is a bad
    // path versus a late crash.
    expect(
      speak(job({ phase: "failed", error: "x", elapsed_seconds: 840 })),
    ).toBe("The import failed. Took 14 minutes.");
    // A terminal announcement fires exactly once, so seconds cost nothing —
    // and the visible line already showed `· 45s` here.
    expect(
      speak(
        job({
          phase: "done",
          progress: { applied: 1, needs_review: 0, skipped: 0, not_landed: 0, already_known: 0 },
          elapsed_seconds: 45,
        }),
      ),
    ).toBe("Import complete. Imported 1, skipped 0. Took 45 seconds.");
  });

  // The other three corners of the suppression. Keying it on `awaiting_decision`
  // alone silenced a run that named nothing, and the flag could then stick for
  // the rest of a run, so that silence was permanent.
  test("only a NAMED wait drops the clause", () => {
    const speak = (data: ImportJobState) =>
      announceMessage({ isPending: false, isError: false, notFound: false, data });
    // Blocked with nothing to name — a park buffered before its row exists.
    // Without the clause this is the bare "Imported 0." that says neither what
    // is happening nor for how long.
    expect(
      speak(
        job({
          phase: "scanning",
          albums: [],
          elapsed_seconds: 600,
          awaiting_decision: true,
        }),
      ),
    ).toBe("Scanning the folder for albums. Running for 10 minutes.");
    expect(
      speak(
        job({
          progress: { applied: 2, needs_review: 0, skipped: 0, not_landed: 0, already_known: 0 },
          elapsed_seconds: 600,
          awaiting_decision: true,
        }),
      ),
    ).toBe("Imported 2. Running for 10 minutes.");
    // Named but NOT blocked: a `search` re-lookup keeps its row needs_review
    // while beets queries MusicBrainz. Nothing else changes there either, so
    // the clock is the only signal the page is alive.
    expect(
      speak(
        job({
          progress: { applied: 1, needs_review: 1, skipped: 0, not_landed: 0, already_known: 0 },
          elapsed_seconds: 600,
          awaiting_decision: false,
        }),
      ),
    ).toBe("Imported 1. 1 album awaiting review. Running for 10 minutes.");
    // A blocked duplicate names its wait through the feed row, not `progress`.
    expect(
      speak(
        job({
          albums: [
            {
              index: 0,
              folder: "/in/a",
              album: "A",
              artist: "B",
              confidence: 90,
              recommendation: "strong",
              status: "needs_dup_resolution",
              did_not_land: false,
            },
          ],
          elapsed_seconds: 600,
          awaiting_decision: true,
        }),
      ),
    ).toBe("Imported 0. 1 duplicate awaiting resolution.");
  });

  // Belt AND braces, the same shape useImport.test.tsx pins on the poll
  // cadence: today's server zeroes `awaiting_decision` off an active phase, so
  // a terminal-and-blocked state never reaches the client — but the announcer
  // must not depend on the other side's guard for the past-tense clause.
  // Without `!finished`, a stale flag would swallow the one announcement a
  // finished run gets.
  test("a terminal job still says how long it took, flag or not", () => {
    const speak = (data: ImportJobState) =>
      announceMessage({ isPending: false, isError: false, notFound: false, data });
    expect(
      speak(
        job({
          phase: "failed",
          error: "the session died",
          progress: { applied: 1, needs_review: 1, skipped: 0, not_landed: 0, already_known: 0 },
          elapsed_seconds: 840,
          awaiting_decision: true,
        }),
      ),
      // The counts come from the failure's own counters (this fixture landed
      // one album before it died); the clause is what this test is about.
    ).toBe("The import failed. Imported 1, skipped 0. Took 14 minutes.");
    expect(
      speak(
        job({
          phase: "done",
          progress: { applied: 3, needs_review: 1, skipped: 0, not_landed: 0, already_known: 0 },
          elapsed_seconds: 840,
          awaiting_decision: true,
        }),
      ),
    ).toBe("Import complete. Imported 3, skipped 0. Took 14 minutes.");
  });

  // `not_landed` is computed for BOTH terminal phases and the done PANEL has
  // always shown it; only this channel dropped it on the done side.
  test("a finished run announces what never landed", () => {
    expect(
      announceMessage({
        isPending: false,
        isError: false,
        notFound: false,
        data: job({
          phase: "done",
          progress: { applied: 3, needs_review: 0, skipped: 1, not_landed: 2, already_known: 0 },
          elapsed_seconds: 840,
        }),
      }),
    ).toBe(
      "Import complete. Imported 3, skipped 1. 2 didn't land. Took 14 minutes.",
    );
  });

  // The history skips reach no outcome record, so none of the three counters
  // holds them: without its own clause the one live region says "Imported 0,
  // skipped 0." for a run whose whole story is that it knew every folder.
  // Pinned per channel here, not only through the page's role="status".
  test("a finished run announces the history skips, and a clean one does not", () => {
    const speak = (data: ImportJobState) =>
      announceMessage({ isPending: false, isError: false, notFound: false, data });
    expect(
      speak(
        job({
          phase: "done",
          progress: { applied: 0, needs_review: 0, skipped: 0, not_landed: 0, already_known: 2 },
          elapsed_seconds: 840,
        }),
      ),
    ).toBe("Import complete. Imported 0, skipped 0. 2 already known. Took 14 minutes.");
    // The clause is gated on itself — a run with none of them gains no words.
    expect(
      speak(
        job({
          phase: "done",
          progress: { applied: 1, needs_review: 0, skipped: 0, not_landed: 0, already_known: 0 },
          elapsed_seconds: 840,
        }),
      ),
    ).toBe("Import complete. Imported 1, skipped 0. Took 14 minutes.");
  });

  // The failed side of the same clause, on the run the gate is about: nothing
  // landed, nothing was skipped on its merits, so the imported/skipped pair is
  // gated out and the history skips are the only news there is.
  test("a failed run whose only news is a history skip announces it", () => {
    expect(
      announceMessage({
        isPending: false,
        isError: false,
        notFound: false,
        data: job({
          phase: "failed",
          error: "the session died",
          progress: { applied: 0, needs_review: 0, skipped: 0, not_landed: 0, already_known: 4 },
          elapsed_seconds: 840,
        }),
      }),
    ).toBe("The import failed. 4 already known. Took 14 minutes.");
  });

  // A crash mid-apply is exactly when albums land or fail to land, and the
  // panel now reports what the run earned — so the one live region must too, or
  // a screen-reader user hears a bare failure for a run that imported 200.
  test("a failure announces the counters it earned before it died", () => {
    const speak = (data: ImportJobState) =>
      announceMessage({ isPending: false, isError: false, notFound: false, data });
    expect(
      speak(
        job({
          phase: "failed",
          error: "the session died",
          progress: { applied: 200, needs_review: 0, skipped: 3, not_landed: 2, already_known: 0 },
          elapsed_seconds: 840,
        }),
      ),
    ).toBe(
      "The import failed. Imported 200, skipped 3. 2 didn't land. Took 14 minutes.",
    );
    // A sweep counts on `sweep`, never on `progress` (its `albums` stays empty
    // by design, so `progress` is all zeros however much it swept).
    expect(
      speak(
        sweepState({
          phase: "failed",
          error: "disk full",
          elapsed_seconds: 840,
          sweep: {
            processed: 200,
            auto_applied: 150,
            banked: 40,
            skipped_known: 10,
            current_folder: null,
            paused: false,
          },
        }),
      ),
    ).toBe(
      "The sweep failed. Processed 200, imported 150, banked 40. 10 already known. Took 14 minutes.",
    );
    // A re-run over an already-imported folder: `skipped_known` was the only
    // nonzero counter, and it was the one the gate summed over but never said —
    // so this announced a bare "The sweep failed." while a tile read 20. The
    // three zeros in front of it are gated out for the same reason.
    expect(
      speak(
        sweepState({
          phase: "failed",
          error: "disk full",
          elapsed_seconds: 840,
          sweep: {
            processed: 0,
            auto_applied: 0,
            banked: 0,
            skipped_known: 20,
            current_folder: null,
            paused: false,
          },
        }),
      ),
    ).toBe("The sweep failed. 20 already known. Took 14 minutes.");
    // Terminal-only clause. A live sweep re-reads its whole announcement every
    // poll (`role="status"` is atomic — the repetition the elapsed clause was
    // gated to stop), and `skipped_known` decides nothing mid-run, so the
    // running sweep keeps the moving triple and the finished one is complete.
    const running = {
      processed: 30,
      auto_applied: 20,
      banked: 10,
      skipped_known: 5,
      current_folder: null,
      paused: false,
    };
    expect(
      speak(sweepState({ phase: "scanning", elapsed_seconds: 840, sweep: running })),
    ).toBe("Sweeping. Processed 30, imported 20, banked 10. Running for 14 minutes.");
    expect(
      speak(sweepState({ phase: "done", elapsed_seconds: 840, sweep: running })),
    ).toBe(
      "Sweep complete. Processed 30, imported 20, banked 10. 5 already known. Took 14 minutes.",
    );
    // Nothing landed: the crash-during-scan case says only that it failed,
    // either side of the origin split. "Imported 0, skipped 0." is noise.
    expect(
      speak(sweepState({ phase: "failed", error: "disk full", elapsed_seconds: 840 })),
    ).toBe("The sweep failed. Took 14 minutes.");
    expect(
      speak(job({ phase: "failed", error: "the session died", elapsed_seconds: 840 })),
    ).toBe("The import failed. Took 14 minutes.");
    // The imported/skipped pair is gated on ITSELF: a run whose only news is
    // that five albums never landed must not open on two zeros.
    expect(
      speak(
        job({
          phase: "failed",
          error: "the session died",
          progress: { applied: 0, needs_review: 0, skipped: 0, not_landed: 5, already_known: 0 },
          elapsed_seconds: 840,
        }),
      ),
    ).toBe("The import failed. 5 didn't land. Took 14 minutes.");
    // And a set-aside album is in NONE of the three buckets, so without its own
    // clause a crashed unattended run drops a whole category it still holds.
    expect(
      speak(
        job({
          phase: "failed",
          error: "the session died",
          origin: "inbox",
          set_aside: 5,
          progress: { applied: 2, needs_review: 5, skipped: 0, not_landed: 0, already_known: 0 },
          elapsed_seconds: 840,
        }),
      ),
    ).toBe(
      "The import failed. Imported 2, skipped 0. 5 albums set aside. Took 14 minutes.",
    );
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

  // Pause is accepted long before the sweep stops (it finishes the current
  // album first), and the announcer used to keep saying "Sweeping." for the
  // whole gap — an activity the state had left. The Pause button self-disables
  // on click, so nothing else spoke.
  test("a pausing sweep is not announced as sweeping", () => {
    const data = sweepState({
      phase: "scanning",
      sweep: {
        processed: 12,
        auto_applied: 8,
        banked: 4,
        skipped_known: 0,
        current_folder: "/in/x",
        paused: true,
      },
    });
    const spoken = announceMessage({
      isPending: false,
      isError: false,
      notFound: false,
      data,
    });
    // Distinct from the visible line ("Pausing; finishing the current album…"),
    // which is the file's own rule for the one live region — the exact text
    // below is what enforces it. No counters and no elapsed clause: the bypass
    // in ImportRun is sticky, so this string is what the announcer holds for the
    // rest of the run and anything moving in it is re-read in full.
    expect(spoken).toBe("Stopping after this album.");
  });

  // The invariant behind that exact text, stated as a comparison so a wording
  // change cannot quietly reintroduce a moving part. Every field that keeps
  // moving after Pause is accepted is varied at once: the last album emits two
  // outcome records (the second carries `album_id` and bumps `auto_applied`),
  // and the clock runs on across a minute boundary, which is where the elapsed
  // clause used to tick. Browser-measured on the branch tip: four announcements
  // in ~5s, closest pair 974ms.
  test("a paused sweep says one fixed thing while its counters and clock move", () => {
    const spoken = (over: Partial<SweepStatus>, elapsed: number) =>
      announceMessage({
        isPending: false,
        isError: false,
        notFound: false,
        data: sweepState({
          phase: "applying",
          elapsed_seconds: elapsed,
          sweep: {
            processed: 6,
            auto_applied: 4,
            banked: 2,
            skipped_known: 0,
            current_folder: "/in/x",
            paused: true,
            ...over,
          },
        }),
      });
    const atPause = spoken({}, 118);
    expect(atPause).toBe("Stopping after this album.");
    // The initial outcome: processed + banked move.
    expect(spoken({ processed: 7, banked: 3 }, 135)).toBe(atPause);
    // The follow-up carrying `album_id`: auto_applied moves.
    expect(spoken({ processed: 7, banked: 3, auto_applied: 5 }, 136)).toBe(atPause);
    // A minute boundary crossed while the album finishes.
    expect(spoken({ processed: 7, banked: 3, auto_applied: 5 }, 190)).toBe(atPause);
    // A pause long enough to cross an HOUR, where the clause changes unit.
    expect(spoken({ skipped_known: 9 }, 3700)).toBe(atPause);
    // Control: the same varying inputs DO move an unpaused sweep, so the
    // assertions above are about the pause and not about a dead code path.
    const sweeping = (over: Partial<SweepStatus>, elapsed: number) =>
      announceMessage({
        isPending: false,
        isError: false,
        notFound: false,
        data: sweepState({
          phase: "applying",
          elapsed_seconds: elapsed,
          sweep: {
            processed: 6,
            auto_applied: 4,
            banked: 2,
            skipped_known: 0,
            current_folder: "/in/x",
            paused: false,
            ...over,
          },
        }),
      });
    expect(sweeping({}, 118)).toBe("Sweeping. Processed 6, imported 4, banked 2. Running for 1 minute.");
    expect(sweeping({ processed: 7, banked: 3 }, 135)).not.toBe(sweeping({}, 118));
    expect(sweeping({}, 190)).not.toBe(sweeping({}, 118));
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

  // At the visible line's own floor, seconds are allowed: nothing repeats a
  // terminal announcement or an sr-only twin.
  test.each([
    [ELAPSED_AFTER_S - 1, null],
    [ELAPSED_AFTER_S, "30 seconds"],
    [45, "45 seconds"],
    [60, "1 minute"],
  ])("at the label's floor, speaks %i seconds as %s", (seconds, expected) => {
    expect(spokenElapsed(seconds, ELAPSED_AFTER_S)).toBe(expected);
  });

  // The twin exists exactly where the visible label does — otherwise a segment
  // renders on screen with nothing spoken behind it. This pins the two FUNCTIONS
  // agreeing; it supplies the floor itself, so it says nothing about the
  // component passing it. That is pinned by the rendered 30-59s fixtures in
  // ImportPage.test.tsx, which is what kills the drop-the-argument mutant.
  test.each([ELAPSED_AFTER_S, 45, 132, 3700])(
    "has a twin wherever elapsedLabel does (%i)",
    (seconds) => {
      expect(elapsedLabel(seconds)).not.toBeNull();
      expect(spokenElapsed(seconds, ELAPSED_AFTER_S)).not.toBeNull();
    },
  );
});
