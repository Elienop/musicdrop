import { Link } from "react-router";

import type { Album } from "@/api/useAlbums";
import { Back } from "@/components/icons";
import { CoverArt } from "@/components/system/CoverArt";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";

/** Responsive card-row grid, shared by every album/artist grid surface.
 * Cards are horizontal rows (the Koito "Albums featuring" anatomy: square
 * thumb + info beside it). Columns AUTO-FILL at a minimum of
 * `--card-row-min` (derived in styles.css: text ≥ 1.5× the thumb width), so
 * the count falls out of the space instead of per-breakpoint constants;
 * `min(...,100%)` keeps narrow phones from overflowing. */
export const GRID_CLASS =
  "grid gap-x-6 gap-y-3 grid-cols-[repeat(auto-fill,minmax(min(var(--card-row-min),100%),1fr))]";

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
 * Anatomy = Koito's "Albums featuring" row: a square thumb with the info
 * beside it, vertically centered. No fade here — the dissolve stays a
 * detail-rail treatment; rows highlight with a surface tint on hover. */
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
      className="focus-ring hover:bg-surface-hover flex items-center gap-3 rounded-lg p-2 transition-colors"
    >
      <CoverArt
        src={`/api/albums/${album.id}/cover`}
        assetKey={`album:${album.id}`}
        alt={`${album.title} cover`}
        className="border-border size-32 shrink-0 rounded-lg border"
      />
      <div className="flex min-w-0 flex-col gap-1 text-left">
        <span className="block truncate text-base" title={album.title}>
          {album.title}
        </span>
        <span
          className="text-muted-foreground block truncate text-sm"
          title={album.album_artist}
        >
          {album.album_artist}
        </span>
        <span className="text-muted-foreground block truncate text-sm">
          {album.year !== null && <>{album.year} &middot; </>}
          {album.track_count} {album.track_count === 1 ? "track" : "tracks"}
          {album.genre && <> &middot; {album.genre}</>}
        </span>
      </div>
    </Link>
  );
}

export function AlbumsGridSkeleton({ count }: { count: number }) {
  return (
    <ul className={GRID_CLASS} aria-hidden="true">
      {Array.from({ length: count }, (_, i) => (
        <li key={i} className="flex items-center gap-3 p-2">
          {/* Mirrors the row card: square thumb + text lines beside it. */}
          <Skeleton className="border-border size-32 shrink-0 rounded-lg border" />
          <div className="flex min-w-0 flex-1 flex-col gap-2">
            <Skeleton className="h-5 w-3/4" />
            <Skeleton className="h-4 w-1/2" />
          </div>
        </li>
      ))}
    </ul>
  );
}
