// frontend/src/lib/format.ts

/** Separator between the segments of a status or metadata line. The space
 * AFTER the middot is non-breaking, so a wrap cannot strand a dangling "·" at
 * the end of a line; the ordinary space before it is where the line is allowed
 * to break — which means a wrapped line CAN open with the middot (measured: 22
 * of 71 error lengths at 360px, back when the failed panel glued the duration
 * to the raw exception with this constant). The failed panel builds it again
 * for its count line — the page's own text, not an exception. Measured at
 * 360px: one line at realistic counts, and at six figures it wraps and opens
 * with the middot, exactly as JobDone's identical line already does.
 *
 * It lived in `pages/import/importStatus.ts` while the import page was the
 * only consumer. Two more arrived: `CandidateReview`'s match header, which
 * wraps at 360px and is the reason this glyph pair matters there, and
 * `ReviewPage`'s decision row.
 *
 * The Review row gains nothing measurable and is here for ONE dialect: its
 * string reaches `AlbumRow`'s `meta` slot, a `flex-shrink: 0` item whose used
 * width is therefore max-content. Measured at 360px with a 68-character
 * artist: 130.5px wide, one client rect, `lines: 1` — identical to the import
 * feed's row, which has always passed this constant into the same slot.
 * Max-content is the whole reason no separator can strand there; the slot
 * itself does NOT clip (measured `scrollWidth 131 == clientWidth 131`,
 * `overflow-x: visible`). The list's `overflow-hidden` clips the slot's
 * NEIGHBOUR, the `min-w-0 truncate` subtitle, which at 360px is squeezed to
 * zero width — recorded in BACKLOG, not solved here. */
export const SEGMENT_SEP = " ·\u00a0";

/** Human-readable total duration for the library dashboard:
 * ">= 1 day" -> "6.3 days"; ">= 1 hour" -> "18h 42m"; else "5m". */
export function formatTotalDuration(seconds: number): string {
  if (seconds >= 86_400) {
    return `${(seconds / 86_400).toFixed(1)} days`;
  }
  if (seconds >= 3_600) {
    const h = Math.floor(seconds / 3_600);
    const m = Math.floor((seconds % 3_600) / 60);
    return `${h}h ${m}m`;
  }
  return `${Math.floor(seconds / 60)}m`;
}

/** SI (base-1000) byte size: "74.2 GB", "5.5 MB", "512 B". The caller adds a
 * leading "~" when the value is an estimate. */
export function formatBytes(bytes: number): string {
  if (bytes < 1000) {
    return `${bytes} B`;
  }
  const units = ["KB", "MB", "GB", "TB", "PB"];
  let value = bytes / 1000;
  let i = 0;
  while (value >= 1000 && i < units.length - 1) {
    value /= 1000;
    i += 1;
  }
  return `${value.toFixed(1)} ${units[i]}`;
}

/** "3 tracks", "1 track" — the word alone; callers place the number. */
export function plural(n: number, one: string, many = `${one}s`): string {
  return n === 1 ? one : many;
}

/** A wire timestamp (UTC ISO 8601, e.g. "2026-08-02T13:53:00Z") as a local
 * date AND time: "Aug 2, 2026, 1:53 PM" in en-US. Both halves matter — a job
 * result is only readable as STALE next to the moment it was produced, and a
 * date alone can't tell this morning's run from tonight's. Rendered in the
 * reader's own zone and locale, since the wire value is always UTC. */
export function formatTimestamp(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

/** Track length as `m:ss` (e.g. 284 -> "4:44", 5 -> "0:05"). Returns an
 * en-dash for a missing duration so untimed rows still align. Distinct from
 * formatTotalDuration above: this is per-track, never rolls into hours. */
export function formatDuration(seconds: number | null): string {
  if (seconds === null) {
    return "-";
  }
  const total = Math.floor(seconds);
  const mins = Math.floor(total / 60);
  const secs = total % 60;
  return `${mins}:${secs.toString().padStart(2, "0")}`;
}
