import { ChevronLeft, ChevronRight, Disc3, Loader2 } from "lucide-react";
import { useState } from "react";
import { useParams, useSearchParams } from "react-router";

import { useAlbums } from "@/api/useAlbums";
import {
  AlbumCard,
  AlbumsGridSkeleton,
  BackLink,
  ErrorState,
  GRID_CLASS,
} from "@/components/albums/album-grid";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

interface ArtistAlbumsPageProps {
  /** Page size. Defaults to 50; overridable for tests. */
  initialLimit?: number;
}

export function ArtistAlbumsPage({ initialLimit = 50 }: ArtistAlbumsPageProps) {
  const [limit] = useState(initialLimit);
  // The artist is fixed by the ROUTE (`/artists/:artistName`), so a different
  // artist is a fresh mount — no in-place filter-change/offset-reset effect.
  const { artistName } = useParams<{ artistName: string }>();
  const artist = decodeURIComponent(artistName ?? "");

  // Offset lives in the URL (`?offset=N`) so the grid page is bookmarkable and
  // restored when navigating back from album detail.
  const [searchParams, setSearchParams] = useSearchParams();
  const offset = Math.max(0, Number(searchParams.get("offset") ?? "0") || 0);

  const { data, isPending, isError, isFetching, refetch } = useAlbums({
    limit,
    offset,
    artist,
  });

  const total = data?.total ?? 0;
  const hasPagination = total > limit;
  const canPrev = offset > 0;
  const canNext = offset + limit < total;

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
    <section className="flex flex-col gap-6" aria-label={`Albums by ${artist}`}>
      <div className="flex flex-col gap-2">
        <BackLink to="/" label="Artists" />
        <div className="flex flex-col gap-1">
          {/* The heading is the artist, so the count stays a plain "{n} albums"
              (no "by {artist}" — that would be redundant). */}
          <h2 className="text-2xl font-semibold tracking-tight">{artist}</h2>
          {/* Live region mounted unconditionally so assistive tech can observe
              it before the count arrives; only the text toggles. */}
          <p
            className="text-muted-foreground min-h-5 text-sm"
            aria-live="polite"
          >
            {!isPending && !isError && total > 0
              ? `${total.toLocaleString()} ${total === 1 ? "album" : "albums"}`
              : ""}
          </p>
        </div>
      </div>

      {isPending ? (
        <>
          <p className="sr-only" role="status">
            Loading albums&hellip;
          </p>
          <AlbumsGridSkeleton count={Math.min(limit, 18)} />
        </>
      ) : isError ? (
        <ErrorState onRetry={() => void refetch()} />
      ) : data.items.length === 0 ? (
        <ArtistEmptyState artist={artist} />
      ) : (
        <>
          <ul
            className={cn(
              GRID_CLASS,
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

/** Empty state for an artist with no albums (e.g. a stale bookmark). Names the
 * artist and offers an escape back to the roster. */
function ArtistEmptyState({ artist }: { artist: string }) {
  return (
    <div className="border-border flex flex-col items-center gap-3 rounded-xl border border-dashed py-16 text-center">
      <Disc3 className="text-muted-foreground size-10" aria-hidden="true" />
      <div className="flex flex-col gap-1">
        <p className="font-medium">No albums for {artist}</p>
        <p className="text-muted-foreground text-sm">
          Nothing in your library is filed under this artist.
        </p>
      </div>
      <BackLink to="/" label="Artists" />
    </div>
  );
}
