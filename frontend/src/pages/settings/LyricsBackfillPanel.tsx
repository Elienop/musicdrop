import { useEffect, useState } from "react";
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
  const [recheckMisses, setRecheckMisses] = useState(false);
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
      description={(() => {
        const c = coverage.data;
        if (!c) return undefined;
        const parts = [`Coverage ${c.percent}% (${c.with_lyrics} of ${c.total} tracks)`];
        if (c.instrumental > 0) parts.push(`${c.instrumental} instrumental`);
        if (c.checked_no_lyrics > 0) parts.push(`${c.checked_no_lyrics} with no lyrics found`);
        // Instrumental is an answer, not a gap — no sweep re-searches one, so it
        // leaves the to-do count alongside the tracks already searched in vain.
        const left = c.total - c.with_lyrics - c.instrumental - c.checked_no_lyrics;
        if (left > 0) parts.push(`${left} left to check`);
        return parts.join(" · ");
      })()}
    >

      {libraryRunning && status.data ? (
        <div className="flex flex-col gap-2" role="status">
          <div className="flex items-center gap-3 text-sm">
            <Spinner className="text-muted-foreground size-4 shrink-0 animate-spin" aria-hidden="true" />
            <span className="flex-1">
              Backfilling… {status.data.processed} / {status.data.total} · found{" "}
              {status.data.found} · instrumental {status.data.instrumental} · none{" "}
              {status.data.not_found} · failed {status.data.failed} · skipped{" "}
              {status.data.skipped}
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
          <Button
            onClick={() => start.mutate({ recheckMisses })}
            disabled={start.isPending || albumFetchRunning}
          >
            {start.isPending ? (
              <>
                <Spinner className="size-4 animate-spin" aria-hidden="true" /> Starting…
              </>
            ) : (
              "Backfill missing lyrics"
            )}
          </Button>
          <span className="text-muted-foreground text-sm">writes tags → Plex reads them</span>
          <label className="flex w-full items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={recheckMisses}
              onChange={(e) => setRecheckMisses(e.target.checked)}
              className="size-4"
            />
            Re-check tracks already found to have no lyrics
          </label>
          {albumFetchRunning && (
            <span className="text-muted-foreground text-sm">A lyrics fetch is in progress.</span>
          )}
          {start.isError && (
            <span className="text-destructive text-sm" role="alert">
              {(start.error as Error).message}
            </span>
          )}
          {/* A finished or interrupted run shows its tally so the user knows
              what happened without watching the live feed. Every outcome is
              named, skips included: tracks already flagged instrumental are
              skipped by every sweep, and a tally that dropped them would leave
              hundreds of processed tracks unaccounted for. */}
          {status.data && status.data.album_id == null &&
            (status.data.phase === "done" || status.data.phase === "stopped") && (
            <span className="text-muted-foreground text-sm" role="status">
              {status.data.phase === "done" ? "Done" : "Stopped"}: found {status.data.found} ·
              instrumental {status.data.instrumental} · none {status.data.not_found} · failed{" "}
              {status.data.failed} · skipped {status.data.skipped}
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
