import { Link } from "react-router";

import type { Artist } from "@/api/useArtists";
import { ArtistImage } from "@/components/artists/ArtistImage";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";

/**
 * Artist poster card: square portrait on top, name + "{n} albums" below, the
 * whole card a `<Link>` into that artist's albums page. Shared by the roster
 * (`ArtistsPage`) and search results so both render identically.
 */
export function ArtistCard({ artist }: { artist: Artist }) {
  // Empty names shouldn't reach here (backend excludes them) but guard anyway.
  const displayName = artist.name || "Unknown artist";
  return (
    // Whole-card link: a real <a> so it's keyboard- and screen-reader-navigable.
    // Drills into this artist's albums page (the next level of the spine).
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
            {artist.album_count} {artist.album_count === 1 ? "album" : "albums"}
          </span>
        </CardContent>
      </Card>
    </Link>
  );
}
