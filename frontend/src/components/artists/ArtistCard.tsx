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
      className="focus-ring group block rounded-lg"
    >
      <div className="group-hover:ring-primary/50 overflow-hidden rounded-lg ring-1 ring-transparent transition-shadow">
        <ArtistImage
          name={displayName}
          className="aspect-square w-full rounded-lg transition-transform motion-safe:group-hover:scale-[1.02]"
        />
      </div>
      <div className="mt-3 flex flex-col gap-1">
        <span className="block truncate text-sm font-medium" title={displayName}>
          {displayName}
        </span>
        <span className="text-muted-foreground text-sm">
          {artist.album_count} {artist.album_count === 1 ? "album" : "albums"}
        </span>
      </div>
    </Link>
  );
}
