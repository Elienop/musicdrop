import { Link } from "react-router";

import type { Artist } from "@/api/useArtists";
import { ArtistImage } from "@/components/artists/ArtistImage";

/**
 * Artist poster card: square portrait with the name + "{n} albums" below, the
 * whole card a `<Link>` into that artist's albums page. Shared by the roster
 * (`ArtistsPage`) and search results so both render identically.
 *
 * Borderless (Phase 4): same anatomy as AlbumCard — no Card chrome; the
 * wrapper div owns hover ring + clipping, the portrait owns the scale, and
 * focus is the shared focus-ring dialect on the link.
 */
export function ArtistCard({ artist }: { artist: Artist }) {
  // Empty names shouldn't reach here (backend excludes them) but guard anyway.
  const displayName = artist.name || "Unknown artist";
  return (
    // Whole-card link: a real <a> so it's keyboard- and screen-reader-navigable.
    // Drills into this artist's albums page (the next level of the spine).
    <Link
      to={`/artists/${encodeURIComponent(artist.name)}`}
      className="focus-ring hover:bg-surface-hover flex items-center gap-3 rounded-lg p-2 transition-colors"
    >
      {/* Anatomy = Koito's "Albums featuring" row: square portrait with the
          info beside it, vertically centered. Rows highlight with a surface
          tint on hover. */}
      <ArtistImage
        name={displayName}
        className="border-border size-32 shrink-0 rounded-lg border"
      />
      <div className="flex min-w-0 flex-col gap-1 text-left">
        <span className="block truncate text-base" title={displayName}>
          {displayName}
        </span>
        <span className="text-muted-foreground text-sm">
          {artist.album_count} {artist.album_count === 1 ? "album" : "albums"}
        </span>
      </div>
    </Link>
  );
}
