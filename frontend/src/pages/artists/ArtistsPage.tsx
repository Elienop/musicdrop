import { Link } from "react-router";

import { useArtists } from "@/api/useArtists";
import { GRID_CLASS } from "@/components/albums/album-grid";
import { ArtistCard } from "@/components/artists/ArtistCard";
import { Artists } from "@/components/icons";
import { EmptyState } from "@/components/system/EmptyState";
import { ErrorState } from "@/components/system/ErrorState";
import { PageBody, PageHeader } from "@/components/system/PageHeader";
import { PageSkeleton } from "@/components/system/PageSkeleton";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";

export function ArtistsPage() {
  const { data, isPending, isError, refetch } = useArtists();

  return (
    <PageBody>
      <PageHeader
        title="Artists"
        meta={
          !isPending && !isError && data.length > 0
            ? `${data.length.toLocaleString()} ${
                data.length === 1 ? "artist" : "artists"
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
      ) : data.length === 0 ? (
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
      ) : (
        <ul className={GRID_CLASS}>
          {data.map((artist) => (
            <li key={artist.name}>
              <ArtistCard artist={artist} />
            </li>
          ))}
        </ul>
      )}
    </PageBody>
  );
}

function ArtistsGridSkeleton({ count }: { count: number }) {
  return (
    <ul className={GRID_CLASS}>
      {Array.from({ length: count }, (_, i) => (
        <li key={i}>
          {/* Mirrors the borderless ArtistCard: square portrait + two lines. */}
          <Skeleton className="aspect-square w-full rounded-lg" />
          <div className="mt-3 flex flex-col gap-1">
            <Skeleton className="h-5 w-3/4" />
            <Skeleton className="h-4 w-16" />
          </div>
        </li>
      ))}
    </ul>
  );
}
