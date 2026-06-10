import {
  useArtistArtBackfillStatus,
  useArtistArtSettings,
  useSetArtistArtSettings,
  useStartArtistArtBackfill,
  useStopArtistArtBackfill,
} from "@/api/useArtistArt";
import { Spinner } from "@/components/icons";
import { SettingsSection } from "@/components/system/SettingsSection";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";

/** Settings → Artist art for Plex: a persisted on/off toggle for writing
 * artist-poster/-background files into each $albumartist/ folder, plus a
 * library-wide backfill. The toggle drives both the image fetch AND the write —
 * off = nothing is fetched or written, and the backfill button is disabled. */
export function ArtistArtPanel() {
  const settings = useArtistArtSettings();
  const setEnabled = useSetArtistArtSettings();
  const status = useArtistArtBackfillStatus();
  const start = useStartArtistArtBackfill();
  const stop = useStopArtistArtBackfill();

  const enabled = settings.data?.enabled ?? false;
  const job = status.data;
  // The library-wide job owns the panel's progress when it isn't scoped to a
  // single artist (artist == null). A per-artist Apply run (artist != null) is
  // surfaced on the artist page, not here.
  const libraryRunning = job?.phase === "running" && job.artist == null;
  const libraryTerminal =
    job != null &&
    job.artist == null &&
    (job.phase === "done" || job.phase === "stopped" || job.phase === "failed");

  return (
    <SettingsSection
      title="Artist art for Plex"
      description="Write artist-poster and artist-background files into each artist folder so Plex shows artist art. When off, nothing is fetched or written."
    >

      <div className="flex items-center gap-3">
        <Switch
          checked={enabled}
          disabled={settings.isPending || setEnabled.isPending}
          onCheckedChange={(v) => setEnabled.mutate(v)}
          aria-label="Write artist art to library"
        />
        <span className="text-sm">{enabled ? "On" : "Off"}</span>
        {setEnabled.isPending && (
          <Spinner className="text-muted-foreground size-4 animate-spin" aria-hidden="true" />
        )}
        {setEnabled.isError && (
          <span className="text-destructive text-sm" role="alert">
            {setEnabled.error.message}
          </span>
        )}
      </div>

      {libraryRunning && job ? (
        <div className="flex flex-col gap-2" role="status">
          <div className="flex items-center gap-3 text-sm">
            <Spinner
              className="text-muted-foreground size-5 shrink-0 animate-spin"
              aria-hidden="true"
            />
            <span className="flex-1">
              Writing artist art… {job.written} / {job.total} · skipped {job.skipped} · failed{" "}
              {job.failed}
            </span>
            <Button
              variant="outline"
              size="sm"
              onClick={() => stop.mutate()}
              disabled={stop.isPending}
            >
              Stop
            </Button>
          </div>
          {job.current && (
            <p className="text-muted-foreground truncate text-xs">{job.current}</p>
          )}
        </div>
      ) : (
        <div className="flex flex-wrap items-center gap-3">
          <Button onClick={() => start.mutate()} disabled={!enabled || start.isPending}>
            {start.isPending ? (
              <>
                <Spinner className="size-4 animate-spin" aria-hidden="true" /> Starting…
              </>
            ) : (
              "Write all to library"
            )}
          </Button>
          {!enabled && (
            <span className="text-muted-foreground text-sm">
              Turn on artist art for Plex first.
            </span>
          )}
          <span className="text-muted-foreground text-sm">
            writes every artist&apos;s folder → Plex reads them
          </span>
          {start.isError && (
            <span className="text-destructive text-sm" role="alert">
              {(start.error as Error).message}
            </span>
          )}
          {/* A finished or interrupted run shows its tally so the user knows what
              happened without watching the live feed. */}
          {libraryTerminal && job && (job.phase === "done" || job.phase === "stopped") && (
            <span className="text-muted-foreground text-sm" role="status">
              {job.phase === "done" ? "Done" : "Stopped"} — written {job.written} · skipped{" "}
              {job.skipped} · failed {job.failed}
            </span>
          )}
          {/* A failed job surfaces its error inline so a 409/library-locked run
              isn't a silent no-op. */}
          {libraryTerminal && job && job.phase === "failed" && (
            <span className="text-destructive text-sm" role="alert">
              Backfill failed{job.error ? `: ${job.error}` : "."}
            </span>
          )}
        </div>
      )}
    </SettingsSection>
  );
}
