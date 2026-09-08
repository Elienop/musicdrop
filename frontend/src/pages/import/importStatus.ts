import type { ImportJobState, SweepStatus } from "@/api/useImport";

/** The single spoken status for the whole run — it is the one `aria-live`
 * source. Verb-first, and worded away from the visible cue/panels so a test can
 * single one out by its whole text.
 *
 * That is a goal, not a guarantee: `sweepMessage` returns "Sweep paused. …"
 * under a panel titled "Sweep paused", and the sweep test works around it with
 * an exact-text query. Check a new phrasing against the panels it shares a page
 * with rather than assuming the separation holds.
 *
 * Data-bearing phrasings carry the elapsed clause: the visible number sits
 * outside any live region, so without this a screen-reader user hears the same
 * string on every poll and is told nothing for the whole ten minutes. The one
 * exception is a run whose announcement already names what it waits for — see
 * {@link elapsedClause}. */
export function announceMessage(args: {
  isPending: boolean;
  isError: boolean;
  notFound: boolean;
  data: ImportJobState | undefined;
}): string {
  const { isPending, isError, notFound, data } = args;
  // Terminal phrasings are deliberately distinct from the visible panels'
  // headings ("This import is no longer available" / "Couldn't load the
  // import"), so a query for either heading finds one node.
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
    data.awaiting_decision && namesAWait(data),
  );
  if (data.phase === "failed") return failedMessage(data) + clause;
  if (data.origin === "sweep" && data.sweep) {
    return sweepMessage(data.sweep, data.phase) + clause;
  }
  if (done) {
    const { applied, skipped, not_landed } = data.progress;
    // The done PANEL has always shown the lost count and the failed
    // announcement gained it; this channel was the one place it went missing.
    return (
      `Import complete. Imported ${applied}, skipped ${skipped}.` +
      notLandedClause(not_landed) +
      clause
    );
  }
  if (data.phase === "scanning" && data.albums.length === 0) {
    return "Scanning the folder for albums." + clause;
  }
  return progressMessage(data) + clause;
}

/** The spoken elapsed sentence appended to a data-bearing announcement — empty
 * under a minute, past tense once the run is over.
 *
 * Dropped only when the announcement already NAMES the wait. `role="status"` is
 * implicitly atomic, so each minute tick re-reads the WHOLE string, and while a
 * named decision is owed nothing else can change — a 20-minute decision became
 * 20 full re-reads carrying no new information, and asserting activity. With
 * the clause gone the string is static and the announcer's identical-string
 * de-dup suppresses the repeat. The visible line keeps its value; that number
 * counts the whole run and must not vanish.
 *
 * Keying on `awaiting_decision` alone was wrong twice over. A park buffered
 * before its row exists sets the flag with nothing to name, so the whole
 * announcement collapsed to `"Imported 0."` — the "working or wedged?"
 * ambiguity this clause exists to remove. And the flag could then stick for the
 * rest of a run, which made that silence permanent — closed since, by asking the
 * bridge directly (`ImportBridge.has_unanswered_park`) instead of mirroring it
 * consumer-side. The flag is still an AND term: a `search` re-lookup keeps its row
 * `needs_review` while beets queries MusicBrainz, and there the clock is the
 * only thing that changes. */
function elapsedClause(
  seconds: number,
  finished: boolean,
  namedWait: boolean,
): string {
  if (namedWait && !finished) return "";
  const spoken = spokenElapsed(
    seconds,
    finished ? ELAPSED_AFTER_S : SPOKEN_LIVE_FLOOR_S,
  );
  if (spoken === null) return "";
  return finished ? ` Took ${spoken}.` : ` Running for ${spoken}.`;
}

/** Sweep-origin jobs announce their monotone counters rather than the feed.
 *
 * `paused` flips the moment Pause is accepted, and the sweep then runs on until
 * the current album is done — a live phase. Without the branch below the one
 * live region kept saying "Sweeping." there, asserting an activity the state
 * had left, and the Pause button self-disables on click so nothing else spoke.
 * Worded away from the visible line ("Pausing; finishing the current album…")
 * so the two never substring-collide. */
function sweepMessage(sweep: SweepStatus, phase: ImportJobState["phase"]): string {
  if (phase !== "done") {
    // The moving triple only. `role="status"` is atomic, so a live sweep
    // re-reads this whole string every poll for as long as it runs — the same
    // repetition the elapsed clause was gated to stop. `skipped_known` decides
    // nothing while the run is in flight, and the tile carries it on screen.
    const counts = sweepCounts(sweep);
    return sweep.paused
      ? `Stopping after this album. ${counts}`
      : `Sweeping. ${counts}`;
  }
  // Spoken once, and a claim about the whole run — so every category it holds.
  const counts = sweepCounts(sweep) + knownClause(sweep);
  return sweep.paused ? `Sweep paused. ${counts}` : `Sweep complete. ${counts}`;
}

/** The sweep's spoken counters. `skipped_known` joins only when it is nonzero:
 * the visible tiles are still where the fourth number normally lives, but the
 * failure branch GATES on it, and a channel must not gate on a number it never
 * says — a re-run sweep that skipped twenty known folders and then crashed
 * announced a bare "The sweep failed." while a tile read 20. */
function sweepCounts(sweep: SweepStatus): string {
  return `Processed ${sweep.processed}, imported ${sweep.auto_applied}, banked ${sweep.banked}.`;
}

/** The fourth tile's number, for the announcements spoken once. Folders skipped
 * before tagging never reach `processed`, so a re-run that skipped twenty and
 * then crashed reported nothing while a tile read 20. */
function knownClause(sweep: SweepStatus): string {
  return sweep.skipped_known > 0 ? ` ${sweep.skipped_known} already known.` : "";
}

/** A failed sweep's counters, empty when it did nothing at all.
 *
 * Each half is gated on itself, so the gate and the sentence are one
 * computation rather than a sum that can disagree with what gets said. A live
 * sweep keeps the unconditional triple: zeros there mean "not yet", but on a
 * terminal panel they are a claim about the whole run. */
function failedSweepCounts(sweep: SweepStatus): string {
  const known = knownClause(sweep).trimStart();
  if (sweep.processed + sweep.auto_applied + sweep.banked === 0) return known;
  return sweepCounts(sweep) + knownClause(sweep);
}

/** The lost-album clause both terminal announcements owe. `not_landed` is only
 * ever nonzero on a terminal job, so it drops out of a clean run. */
function notLandedClause(notLanded: number): string {
  return notLanded > 0 ? ` ${notLanded} didn't land.` : "";
}

/** A crashed run still earned its counters, and the panel now reports them — so
 * the one live region must too, or a screen-reader user hears a bare failure
 * for a run that imported two hundred albums. Sweeps count on `sweep`, every
 * other origin on `progress`; a run that died holding nothing says only that it
 * failed.
 *
 * Each clause is gated on ITSELF, not on a sum of all three. Gating the
 * imported/skipped pair on `applied + skipped + not_landed` announced
 * "Imported 0, skipped 0." for a run whose only news was that five albums never
 * landed — the noise the bare-failure branch exists to avoid.
 *
 * A sweep's counters get the same treatment through
 * {@link failedSweepCounts}: gating on `processed + auto_applied + banked` and
 * ignoring `skipped_known` said only "The sweep failed." for a re-run that
 * skipped twenty known folders, while a tile read 20.
 *
 * `set_aside` gets a clause because it is in NONE of the three buckets: the
 * server's `_is_imported` and `_is_skipped` both refuse a `needs_review` /
 * `needs_dup_resolution` row, and it never landed either. Without it a crashed
 * unattended run drops a whole category of album it is still holding. The done
 * announcement does not need it — that panel keeps the rows' live Review
 * buttons, so the albums are not stranded there. */
function failedMessage(data: ImportJobState): string {
  const sweep = data.origin === "sweep" ? data.sweep : null;
  if (sweep) {
    const counts = failedSweepCounts(sweep);
    return counts === "" ? "The sweep failed." : `The sweep failed. ${counts}`;
  }
  const { applied, skipped, not_landed } = data.progress;
  let m = "The import failed.";
  if (applied + skipped > 0) m += ` Imported ${applied}, skipped ${skipped}.`;
  m += notLandedClause(not_landed);
  if (data.set_aside > 0) {
    m += ` ${data.set_aside} album${data.set_aside === 1 ? "" : "s"} set aside.`;
  }
  return m;
}

/** Pending duplicates, derived from the feed rows: the backend `progress` has
 * no duplicate counter. The row says a decision is OFFERED, not that the worker
 * is blocked on it (an unattended duplicate sets this status and skips on) —
 * either way the user has something to clear, so a screen-reader user must hear
 * it. Whether the worker is blocked is `awaiting_decision`; the spinner and the
 * poll cadence read that instead. */
function pendingDuplicates(data: ImportJobState): number {
  return data.albums.filter((a) => a.status === "needs_dup_resolution").length;
}

/** Whether the announcement names something the run is waiting for. The one
 * condition {@link progressMessage} uses to name a wait, so the elapsed clause
 * can only be suppressed where the announcement says why. */
function namesAWait(data: ImportJobState): boolean {
  return data.progress.needs_review > 0 || pendingDuplicates(data) > 0;
}

/** Active (non-terminal) non-sweep runs: count what's applied + flag pending
 * decisions (review, duplicate) so a screen-reader user hears the import is
 * waiting on them. */
function progressMessage(data: ImportJobState): string {
  const { applied, skipped, needs_review } = data.progress;
  const needs_dup = pendingDuplicates(data);
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
 * for 59 of every 60 polls the line is frozen — indistinguishable from the
 * wedged page this number exists to rule out. With seconds it changes on EVERY
 * poll, which is what the reader actually sees; the poll is 1 s while the worker
 * works and 10 s once it is blocked on a person, so "every poll" is the honest
 * claim, not "every second". Two units also read as a duration rather than as
 * one more of the line's `2 albums`-shaped counts.
 *
 * Above the hour the second unit is minutes, so the label freezes for a whole
 * minute again. Accepted, not solved: `1h 2m 30s` reads as a clock, and the run
 * that gets there is the unattended sweep.
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

/** The floor for the LIVE announcer's clause. Minute granularity is what lets
 * the announcer's identical-string de-dup cap the clause at one announcement a
 * minute rather than one a second. */
export const SPOKEN_LIVE_FLOOR_S = 60;

/** The SPOKEN elapsed value — "45 seconds", "12 minutes", "1 hour 5 minutes" —
 * or null below `floorSeconds`. Words, not `12m`, which a screen reader reads
 * as a letter.
 *
 * Two floors, because there are two consumers. The live announcer passes
 * {@link SPOKEN_LIVE_FLOOR_S}. A terminal announcement fires exactly once (it
 * bypasses the throttle) and the sr-only twin of a visible label is not
 * repeated at all, so both pass {@link ELAPSED_AFTER_S} — the visible line's
 * own threshold, which showed `· 45s` while the announcer said nothing. */
export function spokenElapsed(
  seconds: number,
  floorSeconds: number = SPOKEN_LIVE_FLOOR_S,
): string | null {
  if (!Number.isFinite(seconds)) return null;
  if (seconds < floorSeconds) return null;
  const whole = Math.floor(seconds);
  if (whole < 60) return plural(whole, "second");
  const minutes = Math.floor(whole / 60);
  if (minutes < 60) return plural(minutes, "minute");
  const hours = plural(Math.floor(minutes / 60), "hour");
  const rest = minutes % 60;
  return rest === 0 ? hours : `${hours} ${plural(rest, "minute")}`;
}
