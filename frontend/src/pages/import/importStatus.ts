import type { ImportJobState, SweepStatus } from "@/api/useImport";

/** The single spoken status for the whole run. Verb-first and intentionally
 * worded differently from the visible cue/panels so it never substring-collides
 * with them in the DOM — it is the one `aria-live` source. */
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
  if (data.phase === "failed") return "The import failed.";
  if (data.origin === "sweep" && data.sweep) {
    return sweepMessage(data.sweep, data.phase);
  }
  if (data.phase === "done") {
    const { applied, skipped } = data.progress;
    return `Import complete. Imported ${applied}, skipped ${skipped}.`;
  }
  if (data.phase === "scanning" && data.albums.length === 0) {
    return "Scanning the folder for albums.";
  }
  return progressMessage(data);
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
  // duplicate-pending count from the feed rows. A parked duplicate BLOCKS the
  // worker, so a screen-reader user must hear that the import is waiting on them.
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

/** Seconds a run must pass before its working line carries the elapsed value.
 * Below it the wait is not worth asking about, so a fast import gains no text. */
export const ELAPSED_AFTER_S = 30;

/** The working line's elapsed suffix — `45s`, `12m`, `2h 5m` — or null below
 * {@link ELAPSED_AFTER_S}, where the line reads exactly as it did before.
 * Truncates to whole units, like the server's own whole-second count. */
export function elapsedLabel(seconds: number): string | null {
  if (seconds < ELAPSED_AFTER_S) return null;
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}
