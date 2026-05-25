import {
  AlertCircle,
  ArrowLeft,
  ChevronLeft,
  ChevronRight,
  Disc3,
  Loader2,
  Music,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router";

import type { Album } from "@/api/useAlbums";
import { useAlbums } from "@/api/useAlbums";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";

const GRID_CLASS =
  "grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 2xl:grid-cols-5";

interface AlbumsPageProps {
  /** Page size. Defaults to 50; overridable for tests. */
  initialLimit?: number;
}

export function AlbumsPage({ initialLimit = 50 }: AlbumsPageProps) {
  const [limit] = useState(initialLimit);
  // Offset lives in the URL (`?offset=N`) so the grid page is bookmarkable and
  // restored when navigating back from album detail. Reads default to 0 and
  // clamp negatives away; a 0 offset is left out of the URL to keep it clean.
  const [searchParams, setSearchParams] = useSearchParams();
  const offset = Math.max(0, Number(searchParams.get("offset") ?? "0") || 0);
  // The active artist filter (`?artist=`), shared with the Artists roster. A
  // blank value is treated as "no filter".
  const artist = searchParams.get("artist")?.trim() || undefined;

  // When the artist filter changes, the offset no longer maps to the new,
  // shorter result set — drop the stale page so we start from the top rather
  // than landing on an out-of-range page. Tracks the previous artist across
  // renders and clears `offset` on transition.
  const prevArtist = useRef(artist);
  useEffect(() => {
    if (prevArtist.current !== artist) {
      prevArtist.current = artist;
      if (offset > 0) {
        setSearchParams(
          (prev) => {
            const params = new URLSearchParams(prev);
            params.delete("offset");
            return params;
          },
          { replace: true },
        );
      }
    }
  }, [artist, offset, setSearchParams]);

  const { data, isPending, isError, isFetching, refetch } = useAlbums({
    limit,
    offset,
    artist,
  });

  const total = data?.total ?? 0;
  const hasPagination = total > limit;
  const canPrev = offset > 0;
  const canNext = offset + limit < total;

  // Reset scroll on page change so a new page starts from the top rather than
  // mid-scroll. Guarded for jsdom, which has no smooth-scroll behavior.
  function goToOffset(next: number) {
    setSearchParams(
      (prev) => {
        const params = new URLSearchParams(prev);
        if (next <= 0) {
          params.delete("offset");
        } else {
          params.set("offset", String(next));
        }
        return params;
      },
      { replace: false },
    );
    if (typeof window !== "undefined") {
      window.scrollTo({ top: 0, behavior: "smooth" });
    }
  }

  return (
    <section className="flex flex-col gap-6" aria-label="Albums">
      <div className="flex items-end justify-between gap-4">
        <div className="flex flex-col gap-1">
          <h2 className="text-2xl font-semibold tracking-tight">
            {artist ? `Albums by ${artist}` : "Albums"}
          </h2>
          {!isPending && !isError && total > 0 && (
            <p className="text-muted-foreground text-sm" aria-live="polite">
              {total.toLocaleString()} {total === 1 ? "album" : "albums"}
            </p>
          )}
        </div>
        {artist && (
          <Link
            to="/"
            className="text-muted-foreground hover:text-foreground focus-visible:ring-ring flex shrink-0 items-center gap-1.5 rounded-sm text-sm focus-visible:ring-2 focus-visible:outline-none"
          >
            <ArrowLeft className="size-4" aria-hidden="true" />
            All albums
          </Link>
        )}
      </div>

      {isPending ? (
        <AlbumsGridSkeleton count={Math.min(limit, 18)} />
      ) : isError ? (
        <ErrorState onRetry={() => void refetch()} />
      ) : data.items.length === 0 ? (
        <EmptyState />
      ) : (
        <>
          <ul
            className={cn(
              GRID_CLASS,
              // In-flight cue while paging: dim + dezoom interaction so sighted
              // users get feedback that a new page is loading.
              isFetching && "pointer-events-none opacity-60 transition-opacity",
            )}
            aria-busy={isFetching}
          >
            {data.items.map((album) => (
              <li key={album.id}>
                <AlbumCard album={album} />
              </li>
            ))}
          </ul>

          {hasPagination && (
            <nav
              className="flex items-center justify-between gap-4"
              aria-label="Albums pagination"
            >
              <Button
                variant="outline"
                size="sm"
                disabled={!canPrev || isFetching}
                onClick={() => goToOffset(Math.max(0, offset - limit))}
              >
                <ChevronLeft />
                Previous
              </Button>
              <span
                className="text-muted-foreground flex items-center gap-2 text-sm"
                aria-live="polite"
              >
                {isFetching && (
                  <Loader2 className="size-3.5 animate-spin" aria-hidden="true" />
                )}
                <span>
                  {offset + 1}&ndash;{Math.min(offset + limit, total)} of {total}
                </span>
              </span>
              <Button
                variant="outline"
                size="sm"
                disabled={!canNext || isFetching}
                onClick={() => goToOffset(offset + limit)}
              >
                Next
                <ChevronRight />
              </Button>
            </nav>
          )}
        </>
      )}
    </section>
  );
}

/**
 * Square album cover backed by `GET /api/albums/{id}/cover`. Albums without art
 * 404, and decode failures fire `onError` — both flip to a muted music-note
 * placeholder of the same dimensions so the card never shows a broken image.
 */
function CoverImage({ album }: { album: Album }) {
  const [failed, setFailed] = useState(false);
  const alt = `${album.title} cover`;

  if (failed) {
    return (
      <div
        className="bg-muted flex aspect-square w-full items-center justify-center rounded-t-xl"
        role="img"
        aria-label={`${alt} unavailable`}
      >
        <Music className="text-muted-foreground size-10" aria-hidden="true" />
      </div>
    );
  }

  return (
    <img
      src={`/api/albums/${album.id}/cover`}
      alt={alt}
      loading="lazy"
      onError={() => setFailed(true)}
      className="bg-muted aspect-square w-full rounded-t-xl object-cover"
    />
  );
}

function AlbumCard({ album }: { album: Album }) {
  return (
    // Whole-card link: a real <a> (not a div+onClick) so it's keyboard- and
    // screen-reader-navigable for free. The Card stays a plain wrapper. The
    // link's accessible name is the card's text (title + artist + meta).
    <Link
      to={`/albums/${album.id}`}
      className="focus-visible:ring-ring block h-full rounded-xl focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:outline-none"
    >
      <Card className="hover:border-primary/50 h-full gap-3 overflow-hidden py-0 pb-4 transition-colors">
        <CoverImage album={album} />
        <CardHeader className="px-4 pt-3">
          <CardTitle className="truncate" title={album.title}>
            {album.title}
          </CardTitle>
          <CardDescription className="truncate" title={album.album_artist}>
            {album.album_artist}
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-wrap items-center gap-2 px-4">
          {album.year !== null && (
            <Badge variant="secondary">{album.year}</Badge>
          )}
          <span className="text-muted-foreground text-sm">
            {album.track_count} {album.track_count === 1 ? "track" : "tracks"}
          </span>
          {album.genre && (
            <>
              <span className="text-muted-foreground text-sm" aria-hidden="true">
                &middot;
              </span>
              <span
                className="text-muted-foreground min-w-0 truncate text-sm"
                title={album.genre}
              >
                {album.genre}
              </span>
            </>
          )}
        </CardContent>
      </Card>
    </Link>
  );
}

function AlbumsGridSkeleton({ count }: { count: number }) {
  return (
    <ul className={GRID_CLASS} aria-hidden="true">
      {Array.from({ length: count }, (_, i) => (
        <li key={i}>
          <Card className="h-full gap-3 overflow-hidden py-0 pb-4">
            {/* Square cover placeholder — matches the real card so the cover
                loading in doesn't shift the layout. */}
            <Skeleton className="aspect-square w-full rounded-none" />
            <CardHeader className="gap-2 px-4 pt-3">
              {/* Mirrors CardTitle (leading-none font height) + CardDescription. */}
              <Skeleton className="h-5 w-3/4" />
              <Skeleton className="h-4 w-1/2" />
            </CardHeader>
            <CardContent className="flex items-center gap-2 px-4">
              {/* Year badge + "N tracks" — matches the real card's footer row. */}
              <Skeleton className="h-5 w-14 rounded-md" />
              <Skeleton className="h-4 w-16" />
            </CardContent>
          </Card>
        </li>
      ))}
    </ul>
  );
}

function EmptyState() {
  return (
    <div className="border-border flex flex-col items-center gap-3 rounded-xl border border-dashed py-16 text-center">
      <Disc3 className="text-muted-foreground size-10" aria-hidden="true" />
      <div className="flex flex-col gap-1">
        <p className="font-medium">No albums yet</p>
        <p className="text-muted-foreground text-sm">
          Your beets library is empty. Import some music and it&rsquo;ll show
          up here.
        </p>
      </div>
    </div>
  );
}

function ErrorState({ onRetry }: { onRetry: () => void }) {
  return (
    <div className="border-destructive/40 bg-destructive/5 flex flex-col items-center gap-3 rounded-xl border py-16 text-center">
      <AlertCircle className="text-destructive size-10" aria-hidden="true" />
      <div className="flex flex-col gap-1">
        <p className="font-medium">Couldn&rsquo;t load albums</p>
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
