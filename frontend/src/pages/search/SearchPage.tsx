import { AlertCircle, Search } from "lucide-react";
import { Link, useSearchParams } from "react-router";

import type { SearchTrack } from "@/api/useSearch";
import { useSearch } from "@/api/useSearch";
import { AlbumCard, GRID_CLASS } from "@/components/albums/album-grid";
import { ArtistCard } from "@/components/artists/ArtistCard";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";

/** Format a duration in seconds as `m:ss` (e.g. 284 -> "4:44", 5 -> "0:05").
 * Returns an en-dash for a missing duration so untimed rows still align.
 * Mirrors the AlbumDetailPage tracklist formatting. */
function formatDuration(seconds: number | null): string {
  if (seconds === null) {
    return "–";
  }
  const total = Math.floor(seconds);
  const mins = Math.floor(total / 60);
  const secs = total % 60;
  return `${mins}:${secs.toString().padStart(2, "0")}`;
}

export function SearchPage() {
  const [searchParams] = useSearchParams();
  const q = searchParams.get("q") ?? "";
  const trimmed = q.trim();

  const { data, isPending, isError, isFetching, isPlaceholderData, refetch } =
    useSearch(q);

  // Idle: blank query — prompt rather than "no results" (no request was made).
  if (trimmed.length === 0) {
    return <IdleState />;
  }

  // First load only (no prior data to keep on screen): mirror the results
  // shape so the layout doesn't jump when data arrives.
  if (isPending) {
    return (
      <section className="flex flex-col gap-8" aria-label="Search results">
        <p className="sr-only" role="status">
          Searching&hellip;
        </p>
        <SearchSkeleton />
      </section>
    );
  }

  if (isError) {
    return <ErrorState onRetry={() => void refetch()} />;
  }

  // Derive "empty" from what we'd actually RENDER (the arrays), not the server
  // totals — so a total/array divergence can never leave a blank results area.
  const shownCount =
    data.artists.length + data.albums.length + data.tracks.length;

  if (shownCount === 0) {
    return <NoResults query={trimmed} />;
  }

  // In-flight cue while refining an existing query (placeholderData keeps the
  // previous results visible): dim + aria-busy, matching the album grid.
  const refining = isFetching && isPlaceholderData;

  return (
    <section className="flex flex-col gap-8" aria-label="Search results">
      <div className="flex flex-col gap-1">
        <h1 className="text-2xl font-semibold tracking-tight">
          Results for &ldquo;{trimmed}&rdquo;
        </h1>
        {/* ONE polite live region announces the outcome when a search settles —
            not one per section. */}
        <p className="text-muted-foreground min-h-5 text-sm" aria-live="polite">
          {shownCount} {shownCount === 1 ? "result" : "results"}
        </p>
      </div>

      <div
        className={cn(
          "flex flex-col gap-10",
          refining && "pointer-events-none opacity-60 transition-opacity",
        )}
        aria-busy={refining}
      >
      {data.artists.length > 0 && (
        <ResultSection
          title="Artists"
          shown={data.artists.length}
          total={data.artist_total}
        >
          <ul className={GRID_CLASS}>
            {data.artists.map((artist) => (
              <li key={artist.name}>
                <ArtistCard artist={artist} />
              </li>
            ))}
          </ul>
        </ResultSection>
      )}

      {data.albums.length > 0 && (
        <ResultSection
          title="Albums"
          shown={data.albums.length}
          total={data.album_total}
        >
          <ul className={GRID_CLASS}>
            {data.albums.map((album) => (
              <li key={album.id}>
                <AlbumCard album={album} />
              </li>
            ))}
          </ul>
        </ResultSection>
      )}

      {data.tracks.length > 0 && (
        <ResultSection
          title="Tracks"
          shown={data.tracks.length}
          total={data.track_total}
        >
          {/* A flat list of self-contained rows — the playlist seam. A later
              slice slots a per-row checkbox + selection bar in here. */}
          <ul className="border-border divide-border divide-y rounded-xl border">
            {data.tracks.map((track) => (
              <li key={track.id}>
                <TrackRow track={track} />
              </li>
            ))}
          </ul>
        </ResultSection>
      )}
      </div>
    </section>
  );
}

/** Section wrapper: a heading + "{shown} of {total}" then the body. */
function ResultSection({
  title,
  shown,
  total,
  children,
}: {
  title: string;
  shown: number;
  total: number;
  children: React.ReactNode;
}) {
  return (
    <section className="flex flex-col gap-4" aria-label={title}>
      <div className="flex items-baseline gap-3">
        <h2 className="text-2xl font-semibold tracking-tight">{title}</h2>
        <span className="text-muted-foreground text-sm">
          {shown.toLocaleString()} of {total.toLocaleString()}
        </span>
      </div>
      {children}
    </section>
  );
}

/**
 * A single track hit. Every row shares identical chrome (padding/layout); only
 * the TITLE is a link (→ its album page) — so the link's accessible name is
 * just the title, not a run-on of title/artist/album/duration. Singletons
 * (no `album_id`) render the title as plain text but keep the same row shape,
 * so they don't look broken next to linked rows.
 *
 * The `artist · album` sub-line and the duration are plain siblings OUTSIDE the
 * link. Self-contained so the playlist feature can drop a checkbox beside the
 * title link later (checkbox + title-link = two tidy stops).
 */
function TrackRow({ track }: { track: SearchTrack }) {
  return (
    <div className="flex min-w-0 items-center gap-3 px-4 py-3">
      <div className="flex min-w-0 flex-1 flex-col">
        {track.album_id === null ? (
          <span className="truncate font-medium">{track.title}</span>
        ) : (
          <Link
            to={`/albums/${track.album_id}`}
            className="hover:text-primary focus-visible:ring-ring w-fit max-w-full truncate rounded-sm font-medium focus-visible:ring-2 focus-visible:outline-none"
          >
            {track.title}
          </Link>
        )}
        <span className="text-muted-foreground truncate text-sm">
          {track.artist}
          {track.album && (
            <>
              <span aria-hidden="true"> &middot; </span>
              {track.album}
            </>
          )}
        </span>
      </div>
      <span className="text-muted-foreground shrink-0 text-sm tabular-nums">
        {formatDuration(track.duration_seconds)}
      </span>
    </div>
  );
}

function IdleState() {
  return (
    <div className="flex flex-col items-center gap-3 py-24 text-center">
      <Search className="text-muted-foreground size-10" aria-hidden="true" />
      <div className="flex flex-col gap-1">
        <p className="font-medium">Search your library</p>
        <p className="text-muted-foreground text-sm">
          Find artists, albums, and tracks by name.
        </p>
      </div>
    </div>
  );
}

function NoResults({ query }: { query: string }) {
  return (
    <div className="flex flex-col items-center gap-3 py-24 text-center">
      <Search className="text-muted-foreground size-10" aria-hidden="true" />
      <div className="flex flex-col gap-1">
        <p className="font-medium">
          No results for &ldquo;{query}&rdquo;
        </p>
        <p className="text-muted-foreground text-sm">
          Try a different term.
        </p>
      </div>
    </div>
  );
}

/** Mirrors the results shape (a heading + a small card grid + a few track-row
 * lines) so the first-load placeholder doesn't shift the layout. */
function SearchSkeleton() {
  return (
    <div className="flex flex-col gap-8" aria-hidden="true">
      {/* Page heading + count line. */}
      <div className="flex flex-col gap-2">
        <Skeleton className="h-7 w-56" />
        <Skeleton className="h-4 w-20" />
      </div>
      {/* A card-grid section (stands in for Artists/Albums). */}
      <div className="flex flex-col gap-4">
        <Skeleton className="h-7 w-32" />
        <ul className={GRID_CLASS}>
          {Array.from({ length: 5 }, (_, i) => (
            <li key={i}>
              <Card className="h-full gap-3 overflow-hidden py-0 pb-4">
                <Skeleton className="aspect-square w-full rounded-none" />
                <CardHeader className="gap-2 px-4 pt-3">
                  <Skeleton className="h-5 w-3/4" />
                </CardHeader>
                <CardContent className="px-4">
                  <Skeleton className="h-4 w-16" />
                </CardContent>
              </Card>
            </li>
          ))}
        </ul>
      </div>
      {/* A tracks-list section. */}
      <div className="flex flex-col gap-4">
        <Skeleton className="h-7 w-28" />
        <div className="border-border divide-border divide-y rounded-xl border">
          {Array.from({ length: 4 }, (_, i) => (
            <div key={i} className="flex items-center gap-3 px-4 py-3">
              <div className="flex flex-1 flex-col gap-2">
                <Skeleton className="h-4 w-1/3" />
                <Skeleton className="h-3 w-1/2" />
              </div>
              <Skeleton className="h-4 w-10" />
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

function ErrorState({ onRetry }: { onRetry: () => void }) {
  return (
    <div className="border-destructive/40 bg-destructive/5 flex flex-col items-center gap-3 rounded-xl border py-16 text-center">
      <AlertCircle className="text-destructive size-10" aria-hidden="true" />
      <div className="flex flex-col gap-1">
        <p className="font-medium">Couldn&rsquo;t run the search</p>
        <p className="text-muted-foreground text-sm">
          The library didn&rsquo;t respond. Check the backend and try again.
        </p>
      </div>
      <Button variant="outline" size="sm" onClick={onRetry}>
        Retry
      </Button>
    </div>
  );
}
