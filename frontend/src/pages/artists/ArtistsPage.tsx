import { Link, useSearchParams } from "react-router";

import { useArtists } from "@/api/useArtists";
import { GRID_CLASS } from "@/components/albums/album-grid";
import { ArtistCard } from "@/components/artists/ArtistCard";
import { Artists } from "@/components/icons";
import { EmptyState } from "@/components/system/EmptyState";
import { ErrorState } from "@/components/system/ErrorState";
import { PageBody, PageHeader } from "@/components/system/PageHeader";
import { PageSkeleton } from "@/components/system/PageSkeleton";
import {
  PAGE_SIZE_OPTIONS,
  PageSizeSelect,
  Pagination,
} from "@/components/system/Pagination";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { usePageSize } from "@/lib/usePageSize";

export function ArtistsPage() {
  const { data, isPending, isError, refetch } = useArtists();
  const [searchParams, setSearchParams] = useSearchParams();
  const { pageSize, setPageSize } = usePageSize();

  // Client-side pagination over the full roster (the API returns it whole): at
  // 10k-library scale rendering every ArtistCard on mount is a needless
  // render-storm — and each SSE `library:changed` would reconcile the lot. The
  // page size comes from usePageSize (URL `?limit=` or the remembered
  // preference); the offset lives in the URL so it's bookmarkable and Back works.
  const artists = !isPending && !isError ? data : [];
  const offset = Math.max(Number(searchParams.get("offset")) || 0, 0);
  const pageArtists = artists.slice(offset, offset + pageSize);

  const goToOffset = (value: number) => {
    const next = new URLSearchParams(searchParams);
    if (value <= 0) next.delete("offset");
    else next.set("offset", String(value));
    setSearchParams(next);
    window.scrollTo({ top: 0 });
  };

  return (
    <PageBody>
      <PageHeader
        title="Artists"
        meta={
          // Always the FULL roster count, not the current page's size.
          artists.length > 0
            ? `${artists.length.toLocaleString()} ${
                artists.length === 1 ? "artist" : "artists"
              }`
            : undefined
        }
      />
      {isPending ? (
        <PageSkeleton announce="Loading artists…">
          <ArtistsGridSkeleton count={12} />
        </PageSkeleton>
      ) : isError ? (
        <ErrorState
          message="Couldn’t load artists. Check the backend and try again."
          onRetry={() => void refetch()}
        />
      ) : artists.length === 0 ? (
        <EmptyState
          bordered
          icon={Artists}
          title="No artists yet"
          body="Your beets library is empty. Import some music and it’ll show up here."
          action={
            <Button variant="outline" size="sm" asChild>
              <Link to="/import">Add music from a folder</Link>
            </Button>
          }
        />
      ) : pageArtists.length === 0 ? (
        // Roster > 0 but this page is empty → the offset is past the end (the
        // roster shrank under it). Offer a way back to page 1.
        <EmptyState
          bordered
          icon={Artists}
          title="This page is empty — the roster changed under it."
          action={
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => goToOffset(0)}
            >
              Back to first page
            </Button>
          }
        />
      ) : (
        <>
          {artists.length > PAGE_SIZE_OPTIONS[0] && (
            <div className="flex items-center justify-end gap-2">
              <PageSizeSelect value={pageSize} onChange={setPageSize} />
              {artists.length > pageSize && (
                <Pagination
                  compact
                  label="Pagination (top)"
                  total={artists.length}
                  offset={offset}
                  limit={pageSize}
                  onOffsetChange={goToOffset}
                />
              )}
            </div>
          )}
          <ul className={GRID_CLASS}>
            {pageArtists.map((artist) => (
              <li key={artist.name}>
                <ArtistCard artist={artist} />
              </li>
            ))}
          </ul>
          {artists.length > pageSize && (
            <Pagination
              total={artists.length}
              offset={offset}
              limit={pageSize}
              onOffsetChange={goToOffset}
            />
          )}
        </>
      )}
    </PageBody>
  );
}

function ArtistsGridSkeleton({ count }: { count: number }) {
  return (
    <ul className={GRID_CLASS}>
      {Array.from({ length: count }, (_, i) => (
        <li key={i} className="flex items-center gap-3 p-2">
          {/* Mirrors the row card: square portrait + text lines beside it. */}
          <Skeleton className="size-32 shrink-0 rounded-lg" />
          <div className="flex min-w-0 flex-1 flex-col gap-2">
            <Skeleton className="h-5 w-3/4" />
            <Skeleton className="h-4 w-16" />
          </div>
        </li>
      ))}
    </ul>
  );
}
