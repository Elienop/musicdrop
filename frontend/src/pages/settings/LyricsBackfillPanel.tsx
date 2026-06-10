import { useEffect } from "react";
import { useQueryClient } from "@tanstack/react-query";

import {
  useLyricsBackfillStatus,
  useLyricsCoverage,
  useStartLyricsBackfill,
  useStopLyricsBackfill,
} from "@/api/useLyricsBackfill";
import { Spinner } from "@/components/icons";
import { SettingsSection } from "@/components/system/SettingsSection";
import { Button } from "@/components/ui/button";

/** Settings → Library maintenance: lyrics coverage + the library-wide backfill. */
export function LyricsBackfillPanel() {
  const queryClient = useQueryClient();
  const coverage = useLyricsCoverage();
  const status = useLyricsBackfillStatus();
  const start = useStartLyricsBackfill();
  const stop = useStopLyricsBackfill();
  const phase = status.data?.phase;
  const job = status.data;
  const libraryRunning = phase === "running" && job?.album_id == null;
  const albumFetchRunning = phase === "running" && job?.album_id != null;

  // Once a job reaches a terminal phase the coverage % a backfill wrote is
  // stale — refresh it. Keyed on the phase so it fires once per transition
  // (whether the job finished on its own, was stopped, or failed).
  useEffect(() => {
    if (phase === "done" || phase === "stopped" || phase === "failed") {
      void queryClient.invalidateQueries({ queryKey: ["lyrics", "coverage"] });
    }
  }, [phase, queryClient]);

  return (
    <SettingsSection
      title="Lyrics"
      description={
        coverage.data
          ? `Coverage ${coverage.data.percent}% (${coverage.data.with_lyrics} of ${coverage.data.total} tracks)`
          : undefined
      }
    >

      {libraryRunning && status.data ? (
        <div className="flex flex-col gap-2" role="status">
          <div className="flex items-center gap-3 text-sm">
            <Spinner className="text-muted-foreground size-4 shrink-0 animate-spin" aria-hidden="true" />
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
          <Button onClick={() => start.mutate()} disabled={start.isPending || albumFetchRunning}>
            {start.isPending ? (
              <>
                <Spinner className="size-4 animate-spin" aria-hidden="true" /> Starting…
              </>
            ) : (
              "Backfill missing lyrics"
            )}
          </Button>
          <span className="text-muted-foreground text-sm">writes tags → Plex reads them</span>
          {albumFetchRunning && (
            <span className="text-muted-foreground text-sm">A lyrics fetch is in progress.</span>
          )}
          {start.isError && (
            <span className="text-destructive text-sm" role="alert">
              {(start.error as Error).message}
            </span>
          )}
          {/* A finished or interrupted run shows its tally so the user knows
              what happened without watching the live feed. */}
          {status.data && status.data.album_id == null &&
            (status.data.phase === "done" || status.data.phase === "stopped") && (
            <span className="text-muted-foreground text-sm" role="status">
              {status.data.phase === "done" ? "Done" : "Stopped"} — found {status.data.found} · none{" "}
              {status.data.not_found} · failed {status.data.failed}
            </span>
          )}
          {/* A failed job surfaces its error inline so a 409/library-locked
              run isn't a silent no-op. */}
          {status.data && status.data.album_id == null && status.data.phase === "failed" && (
            <span className="text-destructive text-sm" role="alert">
              Backfill failed{status.data.error ? `: ${status.data.error}` : "."}
            </span>
          )}
        </div>
      )}
    </SettingsSection>
  );
}
