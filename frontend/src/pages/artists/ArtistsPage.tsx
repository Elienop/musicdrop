import { Link, useSearchParams } from "react-router";

import { useArtists } from "@/api/useArtists";
import { GRID_CLASS } from "@/components/albums/album-grid";
import { AlphabetIndex } from "@/components/artists/AlphabetIndex";
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
import { plural } from "@/lib/format";
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

  // The loading/error/empty/content ladder as early returns instead of a
  // chained ternary — same four states, same order, same markup.
  const renderArtistsBody = () => {
    if (isPending) {
      return (
        <PageSkeleton announce="Loading artists…">
          <ArtistsGridSkeleton count={12} />
        </PageSkeleton>
      );
    }
    if (isError) {
      return (
        <ErrorState
          message="Couldn’t load artists. Check the backend and try again."
          onRetry={() => void refetch()}
        />
      );
    }
    if (artists.length === 0) {
      return (
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
      );
    }
    if (pageArtists.length === 0) {
      // Roster > 0 but this page is empty → the offset is past the end (the
      // roster shrank under it). Offer a way back to page 1.
      return (
        <EmptyState
          bordered
          icon={Artists}
          title="This page is empty; the roster changed under it."
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
      );
    }
    return (
      <>
        {/* ONE band, not a toolbar row plus a letter row: the letters take
            the empty space to the left, the pager stays right. Both appear
            on the same condition — you can only need either once the roster
            outgrows the current page — so this is one gate, not two.
            LETTER ORDER IS LOAD-BEARING: letters first, pager last. */}
        {artists.length > pageSize && (
          <div className="flex flex-wrap items-center justify-end gap-2">
            <AlphabetIndex
              artists={artists}
              pageSize={pageSize}
              offset={offset}
              onJump={goToOffset}
            />
            <Pagination
              compact
              label="Pagination (top)"
              total={artists.length}
              offset={offset}
              limit={pageSize}
              onOffsetChange={goToOffset}
            />
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
    );
  };

  return (
    <PageBody>
      <PageHeader
        title="Artists"
        meta={
          // Always the FULL roster count, not the current page's size.
          artists.length > 0
            ? `${artists.length.toLocaleString()} ${plural(artists.length, "artist")}`
            : undefined
        }
        // How much of the page you see is a property OF THE PAGE, so it sits
        // with the page's own controls rather than in the row that moves you
        // through the roster. Same placement on Browse.
        actions={
          artists.length > PAGE_SIZE_OPTIONS[0] ? (
            <PageSizeSelect value={pageSize} onChange={setPageSize} />
          ) : undefined
        }
      />
      {renderArtistsBody()}
    </PageBody>
  );
}

function ArtistsGridSkeleton({ count }: Readonly<{ count: number }>) {
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
