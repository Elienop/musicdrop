// frontend/src/components/system/CoverArt.tsx
import { useEffect, useState } from "react";

import { MusicFallback } from "@/components/icons";
import { cn } from "@/lib/utils";

/**
 * Square cover image with the library-wide 404/decode fallback: a muted box
 * with a music-note glyph, always the same dimensions as the image so the
 * layout never shifts (unifies the album-grid / album-detail / duplicates
 * cover implementations — spec §4).
 *
 * Sizing and rounding come from `className` (e.g. "size-40 rounded-xl",
 * "size-9 rounded-md"); the fallback glyph scales with the box.
 *
 * `src: null` means "we already know there is no art" — render the fallback
 * without issuing a request. `alt` defaults to "" (decorative — for covers
 * whose album is already named by adjacent text); pass a real `alt` when the
 * image is the only naming element, and the fallback then announces
 * "<alt> unavailable" (the album-grid idiom).
 */
export function CoverArt({
  src,
  alt = "",
  className,
}: {
  src: string | null;
  alt?: string;
  className?: string;
}) {
  const [failed, setFailed] = useState(false);

  // A new src is a new fetch — forget the previous failure (same idiom as
  // ArtistImage's reset-on-version-change), so cache-busted `?v=` reloads
  // recover from a stale error state.
  useEffect(() => setFailed(false), [src]);

  if (src === null || failed) {
    return (
      <div
        data-slot="cover-art-fallback"
        className={cn(
          "bg-muted flex aspect-square items-center justify-center",
          className,
        )}
        {...(alt === ""
          ? { "aria-hidden": true }
          : { role: "img", "aria-label": `${alt} unavailable` })}
      >
        {/* Fractional size scales the glyph with whatever box the caller
            sized — a size-40 hero and a size-9 thumb both look right. */}
        <MusicFallback
          className="text-muted-foreground size-1/3"
          aria-hidden="true"
        />
      </div>
    );
  }

  return (
    <img
      data-slot="cover-art"
      src={src}
      alt={alt}
      loading="lazy"
      onError={() => setFailed(true)}
      className={cn("bg-muted aspect-square object-cover", className)}
    />
  );
}
