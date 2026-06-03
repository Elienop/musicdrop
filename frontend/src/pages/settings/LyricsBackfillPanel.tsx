import { Loader2 } from "lucide-react";

import {
  useLyricsBackfillStatus,
  useLyricsCoverage,
  useStartLyricsBackfill,
  useStopLyricsBackfill,
} from "@/api/useLyricsBackfill";
import { Button } from "@/components/ui/button";

/** Settings → Library maintenance: lyrics coverage + the library-wide backfill. */
export function LyricsBackfillPanel() {
  const coverage = useLyricsCoverage();
  const status = useLyricsBackfillStatus();
  const start = useStartLyricsBackfill();
  const stop = useStopLyricsBackfill();
  const running = status.data?.phase === "running";

  return (
    <section
      aria-label="Lyrics backfill"
      className="border-border flex flex-col gap-3 rounded-xl border p-4"
    >
      <header className="flex flex-col gap-1">
        <h3 className="text-lg font-semibold tracking-tight">Lyrics</h3>
        {coverage.data && (
          <p className="text-muted-foreground text-sm">
            Coverage {coverage.data.percent}% ({coverage.data.with_lyrics} of{" "}
            {coverage.data.total} tracks)
          </p>
        )}
      </header>

      {running && status.data ? (
        <div className="flex flex-col gap-2" role="status">
          <div className="flex items-center gap-3 text-sm">
            <Loader2 className="text-muted-foreground size-5 shrink-0 animate-spin" aria-hidden="true" />
            <span className="flex-1">
              Backfilling… {status.data.processed} / {status.data.total} · found{" "}
              {status.data.found} · none {status.data.not_found} · failed {status.data.failed}
            </span>
            <Button variant="outline" size="sm" onClick={() => stop.mutate()} disabled={stop.isPending}>
              Stop
            </Button>
          </div>
          {status.data.current && (
            <p className="text-muted-foreground truncate text-xs">{status.data.current}</p>
          )}
        </div>
      ) : (
        <div className="flex flex-wrap items-center gap-3">
          <Button onClick={() => start.mutate()} disabled={start.isPending}>
            {start.isPending ? (
              <>
                <Loader2 className="size-4 animate-spin" aria-hidden="true" /> Starting…
              </>
            ) : (
              "Backfill missing lyrics"
            )}
          </Button>
          <span className="text-muted-foreground text-sm">writes tags → Plex reads them</span>
          {start.isError && (
            <span className="text-destructive text-sm" role="alert">
              {(start.error as Error).message}
            </span>
          )}
          {status.data && status.data.phase === "done" && (
            <span className="text-muted-foreground text-sm" role="status">
              Done — found {status.data.found} · none {status.data.not_found} · failed{" "}
              {status.data.failed}
            </span>
          )}
        </div>
      )}
    </section>
  );
}
