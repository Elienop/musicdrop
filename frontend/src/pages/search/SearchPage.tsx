import { AlertCircle, Search } from "lucide-react";
import { Link, useSearchParams } from "react-router";

import type { SearchTrack } from "@/api/useSearch";
import { useSearch } from "@/api/useSearch";
import {
  AlbumCard,
  AlbumsGridSkeleton,
  GRID_CLASS,
} from "@/components/albums/album-grid";
import { ArtistCard } from "@/components/artists/ArtistCard";
import { Button } from "@/components/ui/button";

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

  const { data, isPending, isError, refetch } = useSearch(q);

  // Idle: blank query — prompt rather than "no results" (no request was made).
  if (trimmed.length === 0) {
    return <IdleState />;
  }

  if (isPending) {
    return (
      <section className="flex flex-col gap-6" aria-label="Search results">
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

  const empty =
    data.artist_total === 0 &&
    data.album_total === 0 &&
    data.track_total === 0;

  if (empty) {
    return <NoResults query={trimmed} />;
  }

  return (
    <section className="flex flex-col gap-10" aria-label="Search results">
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
 * A single track hit: title · artist · album · duration. Links to the album
 * page when `album_id` is set; singletons (no album) render as a plain,
 * non-link row. Self-contained so the playlist feature can wrap it later.
 */
function TrackRow({ track }: { track: SearchTrack }) {
  const meta = (
    <div className="flex min-w-0 items-center gap-3 px-4 py-3">
      <div className="flex min-w-0 flex-1 flex-col">
        <span className="truncate font-medium">{track.title}</span>
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

  if (track.album_id === null) {
    // Singleton: no album page to link to.
    return meta;
  }

  return (
    <Link
      to={`/albums/${track.album_id}`}
      className="hover:bg-muted/50 focus-visible:ring-ring block rounded-md focus-visible:ring-2 focus-visible:outline-none"
    >
      {meta}
    </Link>
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

function SearchSkeleton() {
  return (
    <div className="flex flex-col gap-4" aria-hidden="true">
      <AlbumsGridSkeleton count={5} />
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
