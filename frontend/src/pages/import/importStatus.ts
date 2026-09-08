import type { ImportJobState, SweepStatus } from "@/api/useImport";

/** The single spoken status for the whole run. Verb-first and intentionally
 * worded differently from the visible cue/panels so it never substring-collides
 * with them in the DOM — it is the one `aria-live` source.
 *
 * Data-bearing phrasings carry the elapsed clause: the visible number sits
 * outside any live region, so without this a screen-reader user hears the same
 * string on every poll and is told nothing for the whole ten minutes. The one
 * exception is a run waiting on the operator — see {@link elapsedClause}. */
export function announceMessage(args: {
  isPending: boolean;
  isError: boolean;
  notFound: boolean;
  data: ImportJobState | undefined;
}): string {
  const { isPending, isError, notFound, data } = args;
  // Terminal phrasings are deliberately distinct from the visible panels'
  // headings ("This import is no longer available" / "Couldn't load the
  // import") so the sr-only announcer never substring-collides with them.
  if (notFound) return "That import is gone. It may have expired.";
  if (isError) return "The import could not be loaded.";
  if (isPending || !data) return "Loading the import.";
  const done = data.phase === "done";
  // A failure IS a finish: the clock stops at both terminal transitions, so the
  // clause is valid and past tense there too.
  const finished = done || data.phase === "failed";
  const clause = elapsedClause(
    data.elapsed_seconds,
    finished,
    data.awaiting_decision,
  );
  if (data.phase === "failed") return "The import failed." + clause;
  if (data.origin === "sweep" && data.sweep) {
    return sweepMessage(data.sweep, data.phase) + clause;
  }
  if (done) {
    const { applied, skipped } = data.progress;
    return `Import complete. Imported ${applied}, skipped ${skipped}.` + clause;
  }
  if (data.phase === "scanning" && data.albums.length === 0) {
    return "Scanning the folder for albums." + clause;
  }
  return progressMessage(data) + clause;
}

/** The spoken elapsed sentence appended to a data-bearing announcement — empty
 * under a minute, past tense once the run is over.
 *
 * Dropped entirely while the run waits on the operator. `role="status"` is
 * implicitly atomic, so each minute tick re-reads the WHOLE string, and while a
 * decision is owed nothing else can change — a 20-minute decision became 20
 * full re-reads carrying no new information, and asserting activity. With the
 * clause gone the string is static and the announcer's identical-string de-dup
 * suppresses the repeat. The visible line keeps its value; that number counts
 * the whole run and must not vanish. `progressMessage` still says a decision is
 * owed. */
function elapsedClause(
  seconds: number,
  finished: boolean,
  blocked: boolean,
): string {
  if (blocked && !finished) return "";
  const spoken = spokenElapsed(seconds, finished);
  if (spoken === null) return "";
  return finished ? ` Took ${spoken}.` : ` Running for ${spoken}.`;
}

/** Sweep-origin jobs announce their monotone counters rather than the feed. */
function sweepMessage(sweep: SweepStatus, phase: ImportJobState["phase"]): string {
  const counts = `Processed ${sweep.processed}, imported ${sweep.auto_applied}, banked ${sweep.banked}.`;
  if (phase === "done") {
    return sweep.paused ? `Sweep paused. ${counts}` : `Sweep complete. ${counts}`;
  }
  return `Sweeping. ${counts}`;
}

/** Active (non-terminal) non-sweep runs: count what's applied + flag pending
 * decisions (review, duplicate) so a screen-reader user hears the import is
 * waiting on them. */
function progressMessage(data: ImportJobState): string {
  const { applied, skipped, needs_review } = data.progress;
  // The backend `progress` has no duplicate counter, so derive the
  // duplicate-pending count from the feed rows. The row says a decision is
  // OFFERED, not that the worker is blocked on it (an unattended duplicate sets
  // this status and skips on) — either way the user has something to clear, so
  // a screen-reader user must hear it. Whether the worker is blocked is
  // `awaiting_decision`; the spinner, the cadence and the elapsed clause
  // (which drops out while blocked) read that instead.
  const needs_dup = data.albums.filter(
    (a) => a.status === "needs_dup_resolution",
  ).length;
  let m = `Imported ${applied}.`;
  if (skipped > 0) m += ` Skipped ${skipped}.`;
  if (needs_review > 0) {
    m += ` ${needs_review} album${needs_review === 1 ? "" : "s"} awaiting review.`;
  }
  if (needs_dup > 0) {
    m += ` ${needs_dup} duplicate${needs_dup === 1 ? "" : "s"} awaiting resolution.`;
  }
  return m;
}

/** Seconds a run must pass before its status line carries the elapsed value.
 * Below it the wait is not worth asking about, so a fast import gains no text. */
export const ELAPSED_AFTER_S = 30;

/** Separator between a status line's segments. The space AFTER the middot is
 * non-breaking, so a wrap can never strand a dangling "·" at the end of a line;
 * the ordinary space before it is where the line is allowed to break. */
export const SEGMENT_SEP = " ·\u00a0";

/** `head` plus a second unit, dropping it when zero — "1h", not "1h 0m". The
 * inner space is non-breaking so the two halves never wrap apart. */
function pair(head: string, rest: number, unit: string): string {
  return rest === 0 ? head : `${head}\u00a0${rest}${unit}`;
}

/** The status line's elapsed segment — `45s`, `5m 12s`, `2h 5m` — or null below
 * {@link ELAPSED_AFTER_S}, where the line reads exactly as it did before.
 * Truncates to whole units, like the server's own whole-second count.
 *
 * Two units below the hour on purpose: a bare `5m` changes once a minute, so
 * for 59 of every 60 seconds the line is frozen — indistinguishable from the
 * wedged page this number exists to rule out. Two units also read as a
 * duration rather than as one more of the line's `2 albums`-shaped counts.
 *
 * Above the hour the second unit is minutes, so the label freezes for 59 of
 * every 60 seconds again. Accepted, not solved: `1h 2m 30s` reads as a clock,
 * and the run that gets there is the unattended sweep.
 *
 * Guarded on `Number.isFinite`: unreachable through the typed contract, but
 * `elapsedLabel(undefined)` rendered "NaNh NaNm", and untyped fixtures that
 * omit the field can reach it. */
export function elapsedLabel(seconds: number): string | null {
  if (!Number.isFinite(seconds) || seconds < ELAPSED_AFTER_S) return null;
  const whole = Math.floor(seconds);
  if (whole < 60) return `${whole}s`;
  const minutes = Math.floor(whole / 60);
  if (minutes < 60) return pair(`${minutes}m`, whole % 60, "s");
  return pair(`${Math.floor(minutes / 60)}h`, minutes % 60, "m");
}

const plural = (n: number, unit: string) => `${n} ${unit}${n === 1 ? "" : "s"}`;

/** The SPOKEN elapsed value — "45 seconds", "12 minutes", "1 hour 5 minutes" —
 * or null below the floor. Words, not `12m`, which a screen reader reads as a
 * letter.
 *
 * While the run is live the floor is a minute: minute granularity is what lets
 * the announcer's identical-string de-dup cap this at one announcement a minute
 * rather than one a second. Once `finished`, the announcement fires exactly
 * once (it bypasses the throttle), so seconds cost no repetition — and the
 * floor drops to {@link ELAPSED_AFTER_S}, matching the visible line, which
 * showed `· 45s` while the announcer said nothing. */
export function spokenElapsed(seconds: number, finished = false): string | null {
  if (!Number.isFinite(seconds)) return null;
  if (seconds < (finished ? ELAPSED_AFTER_S : 60)) return null;
  const whole = Math.floor(seconds);
  if (whole < 60) return plural(whole, "second");
  const minutes = Math.floor(whole / 60);
  if (minutes < 60) return plural(minutes, "minute");
  const hours = plural(Math.floor(minutes / 60), "hour");
  const rest = minutes % 60;
  return rest === 0 ? hours : `${hours} ${plural(rest, "minute")}`;
}
