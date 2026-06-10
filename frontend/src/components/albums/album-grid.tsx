import { AlertCircle } from "lucide-react";
import { Link } from "react-router";

import type { Album } from "@/api/useAlbums";
import { Back } from "@/components/icons";
import { CoverArt } from "@/components/system/CoverArt";
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
 * A hierarchical "back" affordance: ghost button + leading Back caret, used by
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
        <Back className="shrink-0" aria-hidden="true" />
        <span className="truncate">{label}</span>
      </Link>
    </Button>
  );
}

/** Where an album was opened from — carried as router state so the album page's
 * "up" link can return there (e.g. back to Browse with its filters). */
export type AlbumOrigin = { label: string; to: string };

/** Read a contextual origin off router `state` (`{ from: {label, to} }`), set
 * by whatever link opened the page (AlbumCard, Review/Import decision links).
 * Returns undefined for absent/malformed state so deep links degrade to each
 * page's default back target. */
export function albumOriginFromState(state: unknown): AlbumOrigin | undefined {
  if (typeof state !== "object" || state === null) return undefined;
  const from = (state as { from?: unknown }).from;
  if (
    typeof from === "object" &&
    from !== null &&
    typeof (from as AlbumOrigin).label === "string" &&
    typeof (from as AlbumOrigin).to === "string"
  ) {
    return from as AlbumOrigin;
  }
  return undefined;
}

/** Whole-card link to an album's tracklist (`/albums/:id`). A real <a> so it's
 * keyboard- and screen-reader-navigable; the link's accessible name is the
 * card's text (title + artist + meta). `from` (optional) records where the card
 * was clicked so the album page can offer a contextual back link. */
export function AlbumCard({
  album,
  from,
}: {
  album: Album;
  from?: AlbumOrigin;
}) {
  return (
    <Link
      to={`/albums/${album.id}`}
      state={from ? { from } : undefined}
      className="focus-visible:ring-ring block h-full rounded-xl focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:outline-none"
    >
      <Card className="hover:border-primary/50 h-full gap-3 overflow-hidden py-0 pb-4 transition-colors">
        <CoverArt
          src={`/api/albums/${album.id}/cover`}
          alt={`${album.title} cover`}
          className="w-full rounded-t-xl"
        />
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
