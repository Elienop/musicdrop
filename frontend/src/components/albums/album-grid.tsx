import { Link } from "react-router";

import type { Album } from "@/api/useAlbums";
import { Back } from "@/components/icons";
import { CoverArt } from "@/components/system/CoverArt";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";

/** Responsive album-cover grid columns, shared by every album-grid surface.
 * Borderless cards get breathing room between rows (gap-y-6) while columns
 * stay tight (gap-x-4); column counts are unchanged. */
export const GRID_CLASS =
  "grid grid-cols-1 gap-x-4 gap-y-6 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 2xl:grid-cols-5";

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
 * was clicked so the album page can offer a contextual back link.
 *
 * Borderless (Phase 4): the art IS the card — no Card chrome. ONE hover
 * mechanism: the wrapper div owns the ring (ring-transparent →
 * group-hover:ring-primary/50) AND the clipping (rounded-lg overflow-hidden);
 * the image owns the scale. Focus is the shared focus-ring dialect on the
 * link itself. */
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
      className="focus-ring group block rounded-lg"
    >
      <div className="group-hover:ring-primary/50 overflow-hidden rounded-lg ring-1 ring-transparent transition-shadow">
        <CoverArt
          src={`/api/albums/${album.id}/cover`}
          alt={`${album.title} cover`}
          className="w-full rounded-lg transition-transform motion-safe:group-hover:scale-[1.02]"
        />
      </div>
      <div className="mt-3 flex flex-col gap-1">
        <span className="block truncate text-sm font-medium" title={album.title}>
          {album.title}
        </span>
        <span
          className="text-muted-foreground block truncate text-sm"
          title={album.album_artist}
        >
          {album.album_artist}
        </span>
        <div className="text-muted-foreground flex flex-wrap items-center gap-2 text-sm">
          {album.year !== null && (
            <Badge variant="secondary">{album.year}</Badge>
          )}
          <span>
            {album.track_count} {album.track_count === 1 ? "track" : "tracks"}
          </span>
          {album.genre && (
            <>
              <span aria-hidden="true">&middot;</span>
              <span className="min-w-0 truncate" title={album.genre}>
                {album.genre}
              </span>
            </>
          )}
        </div>
      </div>
    </Link>
  );
}

export function AlbumsGridSkeleton({ count }: { count: number }) {
  return (
    <ul className={GRID_CLASS} aria-hidden="true">
      {Array.from({ length: count }, (_, i) => (
        <li key={i}>
          {/* Mirrors the borderless card: square art + two text lines. */}
          <Skeleton className="aspect-square w-full rounded-lg" />
          <div className="mt-3 flex flex-col gap-1">
            <Skeleton className="h-5 w-3/4" />
            <Skeleton className="h-4 w-1/2" />
          </div>
        </li>
      ))}
    </ul>
  );
}
