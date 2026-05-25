import { AlertCircle, Users } from "lucide-react";
import { Link } from "react-router";

import type { Artist } from "@/api/useArtists";
import { useArtists } from "@/api/useArtists";
import { GRID_CLASS } from "@/components/albums/album-grid";
import { ArtistImage } from "@/components/artists/ArtistImage";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";

export function ArtistsPage() {
  const { data, isPending, isError, refetch } = useArtists();

  return (
    <section className="flex flex-col gap-6" aria-label="Artists">
      <div className="flex flex-col gap-1">
        <h2 className="text-2xl font-semibold tracking-tight">Artists</h2>
        {/* Live region mounted unconditionally so assistive tech can observe it
            before the count arrives; only the text toggles. */}
        <p className="text-muted-foreground min-h-5 text-sm" aria-live="polite">
          {!isPending && !isError && data.length > 0
            ? `${data.length.toLocaleString()} ${
                data.length === 1 ? "artist" : "artists"
              }`
            : ""}
        </p>
      </div>

      {isPending ? (
        <>
          <p className="sr-only" role="status">
            Loading artists&hellip;
          </p>
          <ArtistsGridSkeleton count={12} />
        </>
      ) : isError ? (
        <ErrorState onRetry={() => void refetch()} />
      ) : data.length === 0 ? (
        <EmptyState />
      ) : (
        <ul className={GRID_CLASS}>
          {data.map((artist) => (
            <li key={artist.name}>
              <ArtistCard artist={artist} />
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function ArtistCard({ artist }: { artist: Artist }) {
  // Empty names shouldn't reach here (backend excludes them) but guard anyway.
  const displayName = artist.name || "Unknown artist";
  return (
    // Whole-card link: a real <a> so it's keyboard- and screen-reader-navigable.
    // Drills into this artist's albums page (the next level of the spine).
    // Poster card matching the album cards: square portrait on top, text below.
    <Link
      to={`/artists/${encodeURIComponent(artist.name)}`}
      className="focus-visible:ring-ring block h-full rounded-xl focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:outline-none"
    >
      <Card className="hover:border-primary/50 h-full gap-3 overflow-hidden py-0 pb-4 transition-colors">
        <ArtistImage
          name={displayName}
          className="aspect-square w-full rounded-t-xl"
        />
        <CardHeader className="px-4 pt-3">
          <CardTitle className="truncate" title={displayName}>
            {displayName}
          </CardTitle>
        </CardHeader>
        <CardContent className="px-4">
          <span className="text-muted-foreground text-sm">
            {artist.album_count}{" "}
            {artist.album_count === 1 ? "album" : "albums"}
          </span>
        </CardContent>
      </Card>
    </Link>
  );
}

function ArtistsGridSkeleton({ count }: { count: number }) {
  return (
    <ul className={GRID_CLASS} aria-hidden="true">
      {Array.from({ length: count }, (_, i) => (
        <li key={i}>
          <Card className="h-full gap-3 overflow-hidden py-0 pb-4">
            {/* Square portrait placeholder — matches the real card so the
                image loading in doesn't shift the layout. */}
            <Skeleton className="aspect-square w-full rounded-none" />
            <CardHeader className="gap-2 px-4 pt-3">
              {/* Mirrors CardTitle (name) + the "N albums" line. */}
              <Skeleton className="h-5 w-3/4" />
            </CardHeader>
            <CardContent className="px-4">
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
      <Users className="text-muted-foreground size-10" aria-hidden="true" />
      <div className="flex flex-col gap-1">
        <p className="font-medium">No artists yet</p>
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
        <p className="font-medium">Couldn&rsquo;t load artists</p>
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
