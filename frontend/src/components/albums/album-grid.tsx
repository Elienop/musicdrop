import { AlertCircle, ChevronLeft, Music } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router";

import type { Album } from "@/api/useAlbums";
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

/** Responsive album-cover grid columns, shared by every album-grid surface. */
export const GRID_CLASS =
  "grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 2xl:grid-cols-5";

/**
 * A hierarchical "back" affordance: ghost button + leading ChevronLeft, used by
 * every drill-down page so the up-navigation looks identical. `to` is the
 * parent route; `label` names the destination (e.g. "Artists", "Radiohead").
 */
export function BackLink({ to, label }: { to: string; label: string }) {
  return (
    // `max-w-full` lets the button shrink within its container; the chevron
    // stays fixed (`shrink-0`) while a long label truncates rather than forcing
    // horizontal page scroll on mobile. `title` exposes the full name on hover.
    <Button variant="ghost" size="sm" className="-ml-2 w-fit max-w-full" asChild>
      <Link to={to} title={label}>
        <ChevronLeft className="shrink-0" />
        <span className="truncate">{label}</span>
      </Link>
    </Button>
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

/** Whole-card link to an album's tracklist (`/albums/:id`). A real <a> so it's
 * keyboard- and screen-reader-navigable; the link's accessible name is the
 * card's text (title + artist + meta). */
export function AlbumCard({ album }: { album: Album }) {
  return (
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

export function AlbumsGridSkeleton({ count }: { count: number }) {
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

/** Album-grid error state with a retry. */
export function ErrorState({ onRetry }: { onRetry: () => void }) {
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
