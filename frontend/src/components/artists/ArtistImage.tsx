import { useEffect, useState } from "react";

import { useArtistArtSettings } from "@/api/useArtistArt";
import { useArtistImageSettings } from "@/api/useArtistImage";
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
  version,
}: {
  name: string;
  className?: string;
  monogramClassName?: string;
  decorative?: boolean;
  /** Bump to defeat the max-age cache after an override save/reset. */
  version?: number;
}) {
  const [failed, setFailed] = useState(false);
  const imageSettings = useArtistImageSettings();
  const artSettings = useArtistArtSettings();
  // Fetching is on when EITHER the image toggle OR the "write to library" toggle
  // is on (one switch — the write toggle also turns image fetching on). Only
  // short-circuit to the monogram (no request) when BOTH are loaded-and-off,
  // which avoids a 404-per-artist storm; while either loads or is on, attempt it.
  const disabled =
    imageSettings.data?.enabled === false && artSettings.data?.enabled === false;

  // A new version (or artist) means the portrait may now exist — clear a stale
  // error so the <img> is retried instead of stuck on the monogram.
  useEffect(() => setFailed(false), [name, version]);

  if (failed || disabled) {
    return (
      <div
        className={cn(
          "bg-muted text-muted-foreground flex items-center justify-center font-semibold select-none",
          className,
        )}
        {...(decorative ? { "aria-hidden": true } : { role: "img", "aria-label": name })}
      >
        <span className={cn("leading-none", monogramClassName)}>{monogram(name)}</span>
      </div>
    );
  }

  const src =
    `/api/artists/image?name=${encodeURIComponent(name)}` +
    (version !== undefined ? `&v=${version}` : "");

  return (
    <img
      src={src}
      alt={decorative ? "" : `${name} portrait`}
      loading="lazy"
      onError={() => setFailed(true)}
      className={cn("bg-muted object-cover", className)}
    />
  );
}
