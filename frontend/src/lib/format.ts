// frontend/src/lib/format.ts

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
