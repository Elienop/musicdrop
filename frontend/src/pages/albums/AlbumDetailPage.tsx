import { AlertCircle, ChevronLeft, Music } from "lucide-react";
import { Fragment, useState } from "react";
import { Link, useParams } from "react-router";

import type { AlbumDetail, Track } from "@/api/useAlbum";
import { AlbumNotFoundError, useAlbum } from "@/api/useAlbum";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

/** Format a duration in seconds as `m:ss` (e.g. 284 -> "4:44", 5 -> "0:05").
 * Returns an en-dash for a missing duration so untimed rows still align. */
function formatDuration(seconds: number | null): string {
  if (seconds === null) {
    return "–";
  }
  const total = Math.floor(seconds);
  const mins = Math.floor(total / 60);
  const secs = total % 60;
  return `${mins}:${secs.toString().padStart(2, "0")}`;
}

export function AlbumDetailPage() {
  const { albumId } = useParams<{ albumId: string }>();
  const id = Number(albumId);
  const { data, isPending, isError, error, refetch } = useAlbum(id);

  if (isPending) {
    return <DetailSkeleton />;
  }

  if (isError) {
    if (error instanceof AlbumNotFoundError) {
      return <NotFoundState />;
    }
    return <ErrorState onRetry={() => void refetch()} />;
  }

  return <AlbumDetailView album={data} />;
}

/** Back-to-library link, shared across the loaded, error, and not-found views. */
function BackLink() {
  return (
    <Button variant="ghost" size="sm" className="-ml-2 w-fit" asChild>
      <Link to="/">
        <ChevronLeft />
        Back to library
      </Link>
    </Button>
  );
}

function AlbumDetailView({ album }: { album: AlbumDetail }) {
  const multiDisc = new Set(album.tracks.map((t) => t.disc)).size > 1;
  // Group by disc preserving the API's disc-then-track order. Tracks already
  // arrive sorted, so a single pass that opens a new group on disc change is
  // enough — no re-sorting needed.
  const discs: { disc: number; tracks: Track[] }[] = [];
  for (const track of album.tracks) {
    const current = discs.at(-1);
    if (!current || current.disc !== track.disc) {
      discs.push({ disc: track.disc, tracks: [track] });
    } else {
      current.tracks.push(track);
    }
  }

  return (
    <article className="flex flex-col gap-8">
      <BackLink />

      <header className="flex flex-col gap-6 sm:flex-row sm:items-end">
        <CoverImage album={album} />
        <div className="flex min-w-0 flex-col gap-2">
          <h2 className="text-3xl font-semibold tracking-tight">
            {album.title}
          </h2>
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
        </div>
      </header>

      <Separator />

      <section aria-label="Tracklist">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="w-12 text-right">#</TableHead>
              <TableHead>Title</TableHead>
              <TableHead className="w-20 text-right">Length</TableHead>
            </TableRow>
          </TableHeader>
          {discs.map((group) => (
            <Fragment key={group.disc}>
              {multiDisc && (
                <TableBody>
                  <TableRow className="hover:bg-transparent">
                    <TableCell
                      colSpan={3}
                      className="text-muted-foreground pt-6 text-xs font-medium tracking-wide uppercase"
                    >
                      Disc {group.disc}
                    </TableCell>
                  </TableRow>
                </TableBody>
              )}
              <TableBody>
                {group.tracks.map((track) => (
                  <TrackRow
                    key={track.id}
                    track={track}
                    albumArtist={album.album_artist}
                  />
                ))}
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
      <TableCell className="text-muted-foreground text-right tabular-nums">
        {track.track}
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
    </TableRow>
  );
}

/**
 * Larger header cover backed by `GET /api/albums/{id}/cover`. Mirrors the grid
 * card's fallback: a 404 or decode failure flips to a music-note placeholder of
 * the same dimensions so the header never shows a broken image.
 */
function CoverImage({ album }: { album: AlbumDetail }) {
  const [failed, setFailed] = useState(false);
  const alt = `${album.title} cover`;

  if (failed) {
    return (
      <div
        className="bg-muted flex size-40 shrink-0 items-center justify-center rounded-xl"
        role="img"
        aria-label={`${alt} unavailable`}
      >
        <Music className="text-muted-foreground size-12" aria-hidden="true" />
      </div>
    );
  }

  return (
    <img
      src={`/api/albums/${album.id}/cover`}
      alt={alt}
      loading="lazy"
      onError={() => setFailed(true)}
      className="bg-muted size-40 shrink-0 rounded-xl object-cover shadow-sm"
    />
  );
}

function DetailSkeleton() {
  return (
    <div className="flex flex-col gap-8" aria-hidden="true">
      <Skeleton className="h-8 w-32" />
      <div className="flex flex-col gap-6 sm:flex-row sm:items-end">
        <Skeleton className="size-40 shrink-0 rounded-xl" />
        <div className="flex flex-col gap-3">
          <Skeleton className="h-9 w-64" />
          <Skeleton className="h-6 w-40" />
          <Skeleton className="h-5 w-48" />
        </div>
      </div>
      <Separator />
      <div className="flex flex-col gap-3">
        {Array.from({ length: 8 }, (_, i) => (
          <div key={i} className="flex items-center gap-4">
            <Skeleton className="size-5 shrink-0" />
            <Skeleton className="h-5 flex-1" />
            <Skeleton className="h-5 w-12 shrink-0" />
          </div>
        ))}
      </div>
    </div>
  );
}

function NotFoundState() {
  return (
    <div className="flex flex-col items-center gap-4 py-16 text-center">
      <Music className="text-muted-foreground size-10" aria-hidden="true" />
      <div className="flex flex-col gap-1">
        <p className="font-medium">Album not found</p>
        <p className="text-muted-foreground text-sm">
          This album doesn&rsquo;t exist in your library. It may have been
          removed.
        </p>
      </div>
      <Button variant="outline" size="sm" asChild>
        <Link to="/">Back to library</Link>
      </Button>
    </div>
  );
}

function ErrorState({ onRetry }: { onRetry: () => void }) {
  return (
    <div className="flex flex-col gap-6">
      <BackLink />
      <div className="border-destructive/40 bg-destructive/5 flex flex-col items-center gap-3 rounded-xl border py-16 text-center">
        <AlertCircle className="text-destructive size-10" aria-hidden="true" />
        <div className="flex flex-col gap-1">
          <p className="font-medium">Couldn&rsquo;t load this album</p>
          <p className="text-muted-foreground text-sm">
            The library didn&rsquo;t respond. Check the backend and try again.
          </p>
        </div>
        <Button variant="outline" size="sm" onClick={onRetry}>
          Retry
        </Button>
      </div>
    </div>
  );
}
