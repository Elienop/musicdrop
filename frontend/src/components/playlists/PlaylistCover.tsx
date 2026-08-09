// frontend/src/components/playlists/PlaylistCover.tsx
import { Playlists } from "@/components/icons";
import { CoverArt } from "@/components/system/CoverArt";
import { cn } from "@/lib/utils";

/** The cover-relevant slice of a Playlist / PlaylistDetail — both carry these
 * (Task 2's contract), so a summary row and the detail header can share one
 * component. */
interface PlaylistCoverSource {
  id: string;
  artwork_hash?: string | null;
  cover_album_ids: number[];
}

/** How a collage tile spans the 2×2 grid so a partial set still fills a full
 * square: one id spans everything, two split into columns, and the first of
 * three spans the top row (leaving its pair to fill the bottom cells). Four is
 * the natural 2×2 — no spanning. */
function tileSpan(count: number, index: number): string {
  if (count === 1) {
    return "col-span-2 row-span-2";
  }
  if (count === 2) {
    return "row-span-2";
  }
  if (count === 3 && index === 0) {
    return "col-span-2";
  }
  return "";
}

/**
 * A playlist's square cover, in three states (spec §Task 5):
 *  1. `artwork_hash` set → the imported/rendered artwork file, one square image
 *     cache-busted by its content hash (a Plex re-upload changes the hash ⇒ the
 *     `?v=` reloads). Uses {@link CoverArt} for the shared 404/decode fallback.
 *  2. else `cover_album_ids` non-empty → a 2×2 collage of up to four album
 *     covers, the first tile spanning to fill when there are fewer than four.
 *  3. else → the `Playlists` glyph in a muted box, matching CoverArt's
 *     icon-placeholder treatment.
 *
 * The image is decorative (`alt=""` / `aria-hidden`): the surrounding row or
 * header always names the playlist. Sizing + rounding come from `className`
 * (e.g. "size-12 rounded-lg", "size-40 rounded-xl").
 */
export function PlaylistCover({
  playlist,
  className,
}: {
  playlist: PlaylistCoverSource;
  className?: string;
}) {
  const { id, artwork_hash, cover_album_ids } = playlist;

  if (artwork_hash) {
    return (
      <CoverArt
        src={`/api/playlists/${id}/artwork?v=${artwork_hash}`}
        className={className}
      />
    );
  }

  if (cover_album_ids.length > 0) {
    const ids = cover_album_ids.slice(0, 4);
    return (
      <div
        data-slot="playlist-cover-collage"
        aria-hidden="true"
        className={cn(
          "bg-muted grid aspect-square grid-cols-2 grid-rows-2 gap-px overflow-hidden",
          className,
        )}
      >
        {ids.map((albumId, index) => (
          <img
            key={albumId}
            src={`/api/albums/${albumId}/cover?size=thumb`}
            alt=""
            loading="lazy"
            className={cn(
              "bg-muted size-full object-cover",
              tileSpan(ids.length, index),
            )}
          />
        ))}
      </div>
    );
  }

  return (
    <div
      data-slot="playlist-cover-fallback"
      aria-hidden="true"
      className={cn(
        "bg-muted flex aspect-square items-center justify-center",
        className,
      )}
    >
      {/* Fractional size scales the glyph with whatever box the caller sized —
          a size-40 hero and a size-12 thumb both look right. */}
      <Playlists className="text-muted-foreground size-1/3" aria-hidden="true" />
    </div>
  );
}
