import { useState } from "react";

import { cn } from "@/lib/utils";

/** First alphanumeric character of the name, uppercased — the monogram letter.
 * Falls back to "?" for names with no alphanumerics (shouldn't happen; the
 * backend filters blanks). Uses a Unicode-aware match so accented/non-Latin
 * leading letters still surface. */
function monogram(name: string): string {
  const match = name.match(/[\p{L}\p{N}]/u);
  return (match?.[0] ?? "?").toUpperCase();
}

/**
 * Square artist portrait backed by `GET /api/artists/image?name=<name>`. The
 * feature is opt-in, so the endpoint 404s when it's off / there's no match, and
 * decode failures fire `onError` — both flip to a muted INITIALS-MONOGRAM
 * placeholder of the same dimensions (via the shared `className`) so the card
 * never shows a broken image and there's no layout shift. The monogram (vs a
 * generic person glyph) makes a default-off roster a grid of distinct letters.
 *
 * Mirrors the album `CoverImage` pattern (album-grid.tsx), incl. the
 * descriptive-vs-decorative alt split:
 * - default (descriptive): `alt="{name} portrait"`; fallback is a labelled
 *   `role="img"` so assistive tech announces the artist.
 * - `decorative`: `alt=""` + `aria-hidden` fallback — for when an adjacent
 *   heading already names the artist (the artist-albums header).
 *
 * `className` sizes/rounds the box (roster: `aspect-square w-full rounded-t-xl`;
 * header: `size-40 rounded-xl`). `monogramClassName` sizes the letter so it
 * looks right at both scales (small grid card vs the 160px header).
 */
export function ArtistImage({
  name,
  className,
  monogramClassName = "text-2xl",
  decorative = false,
}: {
  name: string;
  className?: string;
  monogramClassName?: string;
  decorative?: boolean;
}) {
  const [failed, setFailed] = useState(false);

  if (failed) {
    return (
      <div
        className={cn(
          "bg-muted text-muted-foreground flex items-center justify-center font-semibold select-none",
          className,
        )}
        {...(decorative
          ? { "aria-hidden": true }
          : { role: "img", "aria-label": name })}
      >
        <span className={cn("leading-none", monogramClassName)}>
          {monogram(name)}
        </span>
      </div>
    );
  }

  return (
    <img
      src={`/api/artists/image?name=${encodeURIComponent(name)}`}
      alt={decorative ? "" : `${name} portrait`}
      loading="lazy"
      onError={() => setFailed(true)}
      className={cn("bg-muted object-cover", className)}
    />
  );
}
