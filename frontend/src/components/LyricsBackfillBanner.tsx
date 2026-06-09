import { AlertCircle, Loader2 } from "lucide-react";

import { useLyricsBackfillStatus } from "@/api/useLyricsBackfill";

/**
 * App-wide banner for the lyrics backfill. Shown while a job is running
 * (live progress) and briefly on failure (so a backstage error isn't silent).
 * Hidden when idle/done/stopped. Mounted inside `main` (mx-auto max-w-8xl px-6),
 * so it's a plain full-width child — no own width or horizontal padding.
 */
export function LyricsBackfillBanner() {
  const { data } = useLyricsBackfillStatus();

  if (data?.phase === "running") {
    return (
      <div
        className="border-border bg-muted/50 mb-6 flex items-center gap-3 rounded-xl border p-3 text-sm"
        role="status"
      >
        <Loader2 className="text-muted-foreground size-5 shrink-0 animate-spin" aria-hidden="true" />
        <span className="flex-1">
          {data.album_id != null
            ? `Fetching lyrics — ${data.scope_label}… ${data.processed} / ${data.total}`
            : `Backfilling lyrics… ${data.processed} / ${data.total}`}
        </span>
      </div>
    );
  }

  if (data?.phase === "failed") {
    return (
      <div
        className="border-destructive/40 bg-destructive/5 mb-6 flex items-center gap-3 rounded-xl border p-3 text-sm"
        role="alert"
      >
        <AlertCircle className="text-destructive size-5 shrink-0" aria-hidden="true" />
        <span className="flex-1">
          Lyrics backfill failed{data.error ? `: ${data.error}` : "."}
        </span>
      </div>
    );
  }

  return null;
}
