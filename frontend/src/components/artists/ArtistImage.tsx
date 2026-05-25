import { User } from "lucide-react";
import { useState } from "react";

import { cn } from "@/lib/utils";

/**
 * Square artist portrait backed by `GET /api/artists/image?name=<name>`. The
 * feature is opt-in, so the endpoint 404s when it's off / there's no match, and
 * decode failures fire `onError` — both flip to a muted person-glyph
 * placeholder of the same dimensions (via the shared `className`) so the card
 * never shows a broken image and there's no layout shift.
 *
 * Mirrors the album `CoverImage` fault-tolerant pattern (album-grid.tsx), but
 * for the artist endpoint and the `User` glyph. `className` lets the caller
 * size/round it (roster: `aspect-square w-full rounded-t-xl`; header:
 * `size-40 rounded-xl`).
 */
export function ArtistImage({
  name,
  className,
}: {
  name: string;
  className?: string;
}) {
  const [failed, setFailed] = useState(false);

  if (failed) {
    return (
      <div
        className={cn(
          "bg-muted flex items-center justify-center",
          className,
        )}
        role="img"
        aria-label={`${name} unavailable`}
      >
        <User className="text-muted-foreground size-10" aria-hidden="true" />
      </div>
    );
  }

  return (
    <img
      src={`/api/artists/image?name=${encodeURIComponent(name)}`}
      alt={name}
      loading="lazy"
      onError={() => setFailed(true)}
      className={cn("bg-muted object-cover", className)}
    />
  );
}
