import { Fragment, useEffect, useRef, useState } from "react";
import { Link, useLocation, useParams } from "react-router";
import { useQueryClient } from "@tanstack/react-query";

import type { AlbumDetail, Track } from "@/api/useAlbum";
import { AlbumNotFoundError, useAlbum } from "@/api/useAlbum";
import { useStartAlbumLyricsFetch } from "@/api/useAlbumLyrics";
import { useLyricsBackfillStatus, useStopLyricsBackfill } from "@/api/useLyricsBackfill";
import { useAlbumMissing, type MissingReleaseTrack } from "@/api/useAlbumMissing";
import { formatDuration } from "@/lib/format";
import { buildDiscGroups, type DiscGroup } from "@/pages/albums/missingTracks";
import { BackLink, type AlbumOrigin } from "@/components/albums/album-grid";
import {
  Cover as CoverIcon,
  Edit as EditIcon,
  Lyrics as LyricsIcon,
  MusicFallback,
  Spinner,
} from "@/components/icons";
import { AddToPlaylistMenu } from "@/components/playlists/AddToPlaylistMenu";
import { ReorganizeControl } from "@/components/reorganize/ReorganizeControl";
import { CoverArt } from "@/components/system/CoverArt";
import { EmptyState } from "@/components/system/EmptyState";
import { ErrorState } from "@/components/system/ErrorState";
import { PageSkeleton } from "@/components/system/PageSkeleton";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { Skeleton } from "@/components/ui/skeleton";
import { AlbumEditPanel } from "@/pages/albums/AlbumEditPanel";
import { CoverEditPanel } from "@/pages/albums/CoverEditPanel";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

/** Read a contextual back origin off router `state` (`{ from: {label, to} }`),
 * set by an AlbumCard when it knows where the album was opened from. */
function albumOriginFromState(state: unknown): AlbumOrigin | undefined {
  if (typeof state !== "object" || state === null) return undefined;
  const from = (state as { from?: unknown }).from;
  if (
    typeof from === "object" &&
    from !== null &&
    typeof (from as AlbumOrigin).label === "string" &&
    typeof (from as AlbumOrigin).to === "string"
  ) {
    return from as AlbumOrigin;
  }
  return undefined;
}

export function AlbumDetailPage() {
  const { albumId } = useParams<{ albumId: string }>();
  const id = Number(albumId);
  // A non-numeric id (e.g. /albums/abc) would make the backend return 422, not
  // 404 — which falls through to the generic error + retry loop. Treat it as
  // not-found up front and skip the doomed fetch entirely.
  const validId = Number.isInteger(id);
  const { data, isPending, isError, error, refetch } = useAlbum(id, {
    enabled: validId,
  });

  if (!validId) {
    return <NotFoundState />;
  }

  if (isPending) {
    return <DetailSkeleton />;
  }

  if (isError) {
    if (error instanceof AlbumNotFoundError) {
      return <NotFoundState />;
    }
    return <LoadErrorState onRetry={() => void refetch()} />;
  }

  return <AlbumDetailView album={data} />;
}

function AlbumDetailView({ album }: { album: AlbumDetail }) {
  const missingQuery = useAlbumMissing(album.id, album.mb_albumid);
  const report = missingQuery.data;
  const missingTracks: MissingReleaseTrack[] =
    report?.status === "ok" ? report.missing : [];
  const discs: DiscGroup[] = buildDiscGroups(album.tracks, missingTracks);
  const multiDisc = discs.length > 1;

  const withLyrics = album.tracks.filter((t) => t.has_lyrics).length;
  const missingLyrics = album.tracks.length - withLyrics;

  const [editing, setEditing] = useState(false);
  const [editingCover, setEditingCover] = useState(false);
  const [coverVersion, setCoverVersion] = useState(0);

  // Spec §4 disclosure pattern: opening an inline panel moves focus into it so
  // keyboard/SR users land on what just appeared. Closing leaves focus where
  // it already is — on the toggle button.
  const editPanelRef = useRef<HTMLDivElement>(null);
  const coverPanelRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (editing) editPanelRef.current?.focus();
  }, [editing]);
  useEffect(() => {
    if (editingCover) coverPanelRef.current?.focus();
  }, [editingCover]);

  const backOrigin = albumOriginFromState(useLocation().state);

  return (
    <article
      className="flex flex-col gap-8"
      aria-labelledby="album-detail-title"
    >
      {/* Up-link: back to where the album was opened from (Browse with its
          filters, Search) when known, else UP the spine to the artist. */}
      {backOrigin ? (
        <BackLink to={backOrigin.to} label={backOrigin.label} />
      ) : (
        <BackLink
          to={`/artists/${encodeURIComponent(album.album_artist)}`}
          label={album.album_artist}
        />
      )}

      <header className="flex flex-col gap-6 sm:flex-row sm:items-stretch">
        {/* Hero cover on the shared CoverArt (same dimensions — the blurred-art
            hero itself is Phase 4). */}
        <CoverArt
          src={`/api/albums/${album.id}/cover${coverVersion ? `?v=${coverVersion}` : ""}`}
          className="size-40 shrink-0 rounded-xl shadow-sm"
        />
        <div className="flex min-w-0 flex-col gap-2">
          {/* THE page h1 — detail pages own their h1 directly (PageHeader's
              shape doesn't fit the hero); tabIndex -1 keeps RouteAnnouncer's
              focus contract. */}
          <h1
            id="album-detail-title"
            tabIndex={-1}
            className="text-3xl font-bold tracking-tight break-words"
          >
            {album.title}
          </h1>
          <p className="text-muted-foreground text-lg">{album.album_artist}</p>
          <div className="text-muted-foreground flex flex-wrap items-center gap-2 text-sm">
            {album.year !== null && (
              <Badge variant="secondary">{album.year}</Badge>
            )}
            <span>
              {album.track_count} {album.track_count === 1 ? "track" : "tracks"}
            </span>
            {album.genre && (
              <>
                <span aria-hidden="true">&middot;</span>
                <span>{album.genre}</span>
              </>
            )}
          </div>
          {/* Maintenance actions — a single row pushed to the bottom of the
              column so it lines up with the bottom of the cover, mirroring the
              artist page. */}
          <div className="border-border mt-auto flex flex-wrap items-center gap-3 border-t pt-3">
            <Button
              variant="outline"
              size="sm"
              onClick={() => setEditing((v) => !v)}
              aria-label="Edit album"
              aria-expanded={editing}
              aria-controls="album-edit-panel"
            >
              <EditIcon className="size-4" aria-hidden="true" /> Edit
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={() => setEditingCover((v) => !v)}
              aria-label="Edit cover"
              aria-expanded={editingCover}
              aria-controls="album-cover-panel"
            >
              <CoverIcon className="size-4" aria-hidden="true" /> Cover
            </Button>
            <ReorganizeControl scope={{ scope: "album", albumId: album.id }} />
            <AddToPlaylistMenu
              trackIds={album.tracks.map((t) => t.id)}
              label="Add album to playlist"
            />
          </div>
        </div>
      </header>

      {editing && (
        <div
          id="album-edit-panel"
          ref={editPanelRef}
          tabIndex={-1}
          className="outline-none"
        >
          <AlbumEditPanel album={album} onClose={() => setEditing(false)} />
        </div>
      )}

      {editingCover && (
        <div
          id="album-cover-panel"
          ref={coverPanelRef}
          tabIndex={-1}
          className="outline-none"
        >
          <CoverEditPanel
            albumId={album.id}
            onInstalled={() => setCoverVersion((v) => v + 1)}
            onClose={() => setEditingCover(false)}
          />
        </div>
      )}

      <Separator />

      <section aria-label="Tracklist" className="flex flex-col gap-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <TracklistStatus query={missingQuery} report={report} />
          <LyricsStatus
            albumId={album.id}
            total={album.tracks.length}
            withLyrics={withLyrics}
            missing={missingLyrics}
          />
        </div>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="w-12 pr-4 text-right">#</TableHead>
              <TableHead>Title</TableHead>
              <TableHead className="w-20 text-right">Length</TableHead>
              <TableHead className="w-16 text-center">Lyrics</TableHead>
              <TableHead className="w-12 text-center">
                <span className="sr-only">Add to playlist</span>
              </TableHead>
            </TableRow>
          </TableHeader>
          {discs.map((group) => (
            <Fragment key={group.disc}>
              {multiDisc && group.disc > 0 && (
                <TableBody>
                  <TableRow className="hover:bg-transparent">
                    <TableHead
                      scope="rowgroup"
                      colSpan={5}
                      className="text-muted-foreground h-auto pt-6 text-xs font-medium tracking-wide uppercase"
                    >
                      Disc {group.disc}
                    </TableHead>
                  </TableRow>
                </TableBody>
              )}
              <TableBody>
                {group.rows.map((row) =>
                  row.kind === "present" ? (
                    <TrackRow
                      key={`p-${row.track.id}`}
                      track={row.track}
                      albumArtist={album.album_artist}
                    />
                  ) : (
                    <MissingTrackRow key={`m-${row.track.index}-${row.track.mb_trackid ?? ""}`} track={row.track} />
                  ),
                )}
              </TableBody>
            </Fragment>
          ))}
        </Table>
      </section>
    </article>
  );
}

function TrackRow({
  track,
  albumArtist,
}: {
  track: Track;
  albumArtist: string;
}) {
  const showArtist = track.artist !== albumArtist;
  return (
    <TableRow>
      <TableCell className="text-muted-foreground pr-4 text-right tabular-nums">
        {/* Untagged track number comes through as 0 — show an en-dash instead,
            matching the duration fallback. */}
        {track.track || "–"}
      </TableCell>
      <TableCell>
        <div className="flex min-w-0 flex-col">
          <span className="truncate font-medium">{track.title}</span>
          {showArtist && (
            <span className="text-muted-foreground truncate text-sm">
              {track.artist}
            </span>
          )}
        </div>
      </TableCell>
      <TableCell className="text-muted-foreground text-right tabular-nums">
        {formatDuration(track.duration_seconds)}
      </TableCell>
      <TableCell className="text-center">
        {track.has_lyrics ? (
          <LyricsIcon className="text-foreground inline size-4" aria-label="Has lyrics" />
        ) : (
          <span className="text-muted-foreground" aria-label="No lyrics">
            –
          </span>
        )}
      </TableCell>
      <TableCell className="text-center">
        <AddToPlaylistMenu
          trackIds={[track.id]}
          label={`Add ${track.title} to playlist`}
        />
      </TableCell>
    </TableRow>
  );
}

function MissingTrackRow({ track }: { track: MissingReleaseTrack }) {
  // Convey "missing" through a subtle row tint + italic title + a labelled
  // badge — NOT row-level opacity, which would composite the title and the
  // already-muted #/duration cells below the WCAG AA 4.5:1 floor. Every cell
  // keeps its colour at full alpha (muted-foreground already clears 4.5:1), so
  // the text stays legible while the row still reads as a gap.
  return (
    <TableRow className="bg-muted/40 hover:bg-muted/60">
      <TableCell className="text-muted-foreground pr-4 text-right tabular-nums">
        {track.index || "–"}
      </TableCell>
      <TableCell>
        <div className="flex min-w-0 items-center gap-2">
          {/* Screen readers read the row linearly ("Nude missing 4:44"), so the
              visual greying carries no meaning on its own. Lead with a hidden
              qualifier and an aria-labelled badge so the state announces as a
              distinct phrase, not a stray word beside the title. */}
          <span className="sr-only">Not in your library: </span>
          <span className="text-muted-foreground truncate font-medium italic">
            {track.title}
          </span>
          <Badge
            variant="outline"
            aria-label="Missing from library"
            className="shrink-0 text-xs font-normal"
          >
            missing
          </Badge>
        </div>
      </TableCell>
      <TableCell className="text-muted-foreground text-right tabular-nums">
        {formatDuration(track.duration_seconds)}
      </TableCell>
      <TableCell aria-hidden="true" className="text-muted-foreground text-center">
        –
      </TableCell>
      {/* No add-to-playlist for a track that isn't in the library. */}
      <TableCell aria-hidden="true" />
    </TableRow>
  );
}

function TracklistStatus({
  query,
  report,
}: {
  query: ReturnType<typeof useAlbumMissing>;
  report: ReturnType<typeof useAlbumMissing>["data"];
}) {
  if (query.fetchStatus === "fetching" && !report) {
    return (
      <p className="text-muted-foreground text-sm" role="status">
        Checking MusicBrainz for missing tracks…
      </p>
    );
  }
  if (!report) {
    return null; // disabled (non-MB album) or transport error -> no overlay
  }
  if (report.status === "ok") {
    if (report.missing.length === 0) return null;
    return (
      <p className="text-muted-foreground text-sm">
        {report.missing.length} of {report.total} tracks missing
        {report.source ? ` · from ${report.source}` : ""}
      </p>
    );
  }
  if (report.status === "release_unavailable") {
    return (
      <p className="text-muted-foreground text-sm">
        Couldn’t find this release on MusicBrainz.
      </p>
    );
  }
  if (report.status === "fetch_failed") {
    return (
      <p className="text-muted-foreground flex items-center gap-2 text-sm">
        Couldn’t reach MusicBrainz.
        <Button variant="link" size="sm" className="h-auto p-0" onClick={() => void query.refetch()}>
          Retry
        </Button>
      </p>
    );
  }
  return null; // no_musicbrainz_id -> silent (query is usually disabled anyway)
}

function DetailSkeleton() {
  return (
    <PageSkeleton announce="Loading album…">
      <div className="flex flex-col gap-8">
        {/* Matches the BackLink button height. */}
        <Skeleton className="h-8 w-32" />
        {/* sm:items-center + gap-2 mirror the loaded header to minimize CLS. */}
        <div className="flex flex-col gap-6 sm:flex-row sm:items-center">
          <Skeleton className="size-40 shrink-0 rounded-xl" />
          <div className="flex flex-col gap-2">
            <Skeleton className="h-9 w-64" />
            <Skeleton className="h-6 w-40" />
            <Skeleton className="h-5 w-48" />
          </div>
        </div>
        <Separator />
        <div className="flex flex-col gap-2">
          {/* Table header row (#, Title, Length). */}
          <div className="flex items-center gap-4 pb-2">
            <Skeleton className="h-4 w-8 shrink-0" />
            <Skeleton className="h-4 w-16" />
            <Skeleton className="ml-auto h-4 w-12 shrink-0" />
          </div>
          {/* Track rows — taller to match the two-line-capable real rows. */}
          {Array.from({ length: 8 }, (_, i) => (
            <div key={i} className="flex items-center gap-4 py-1">
              <Skeleton className="h-5 w-8 shrink-0" />
              <Skeleton className="h-5 flex-1" />
              <Skeleton className="ml-auto h-5 w-12 shrink-0" />
            </div>
          ))}
        </div>
      </div>
    </PageSkeleton>
  );
}

function NotFoundState() {
  return (
    <EmptyState
      icon={MusicFallback}
      title="Album not found"
      body="This album doesn’t exist in your library. It may have been removed."
      action={
        <Button variant="outline" size="sm" asChild>
          {/* No album data here, so the artist is unknown — fall back to the
              roster rather than guessing a parent. */}
          <Link to="/artists">Back to artists</Link>
        </Button>
      }
    />
  );
}

function LyricsStatus({
  albumId,
  total,
  withLyrics,
  missing,
}: {
  albumId: number;
  total: number;
  withLyrics: number;
  missing: number;
}) {
  const queryClient = useQueryClient();
  const status = useLyricsBackfillStatus();
  const start = useStartAlbumLyricsFetch(albumId);
  const stop = useStopLyricsBackfill();
  const job = status.data;
  const isThisAlbum = job?.album_id === albumId;
  const runningThis = job?.phase === "running" && isThisAlbum;
  const otherRunning = job?.phase === "running" && !isThisAlbum;
  const terminalThis =
    isThisAlbum && (job?.phase === "done" || job?.phase === "stopped");
  const phase = job?.phase;

  // When THIS album's job ends, the per-track has_lyrics flags are stale — refresh.
  useEffect(() => {
    if (isThisAlbum && (phase === "done" || phase === "stopped" || phase === "failed")) {
      void queryClient.invalidateQueries({ queryKey: ["album", albumId] });
    }
  }, [isThisAlbum, phase, albumId, queryClient]);

  if (runningThis && job) {
    return (
      <div className="flex flex-wrap items-center gap-3" role="status">
        <Spinner className="text-muted-foreground size-4 shrink-0 animate-spin" aria-hidden="true" />
        <span className="text-muted-foreground text-sm">
          Fetching lyrics… {job.processed} / {job.total} · found {job.found}
        </span>
        <Button variant="outline" size="sm" onClick={() => stop.mutate()} disabled={stop.isPending}>
          Stop
        </Button>
      </div>
    );
  }

  return (
    <div className="flex flex-wrap items-center gap-3">
      <span className="text-muted-foreground text-sm">
        {withLyrics} of {total} tracks have lyrics
      </span>
      {missing > 0 && (
        <Button
          variant="outline"
          size="sm"
          onClick={() => start.mutate()}
          disabled={start.isPending || otherRunning}
        >
          {start.isPending ? (
            <>
              <Spinner className="size-4 animate-spin" aria-hidden="true" /> Starting…
            </>
          ) : (
            `Fetch missing lyrics (${missing})`
          )}
        </Button>
      )}
      {otherRunning && (
        <span className="text-muted-foreground text-sm">another lyrics job is running</span>
      )}
      {terminalThis && job && (
        <span className="text-muted-foreground text-sm" role="status">
          {job.found} added · {job.not_found} none · {job.failed} failed
          {job.writes_enabled ? "" : " · not written to files (enable writes in config)"}
        </span>
      )}
      {start.isError && (
        <span className="text-destructive text-sm" role="alert">
          {(start.error as Error).message}
        </span>
      )}
    </div>
  );
}

function LoadErrorState({ onRetry }: { onRetry: () => void }) {
  return (
    <div className="flex flex-col gap-6">
      {/* Artist unknown on error — the escape keeps the BackLink to the roster. */}
      <BackLink to="/artists" label="Artists" />
      <ErrorState
        message="Couldn’t load this album. The library didn’t respond — check the backend and try again."
        onRetry={onRetry}
      />
    </div>
  );
}
