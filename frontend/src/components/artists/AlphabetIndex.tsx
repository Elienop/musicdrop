import { useMemo } from "react";

import type { Artist } from "@/api/useArtists";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

const LETTERS = ["#", ..."ABCDEFGHIJKLMNOPQRSTUVWXYZ"] as const;

/**
 * The bucket a name sorts into: the FIRST CHARACTER (not first letter) of
 * the name after stripping combining diacritics, uppercased. This must
 * mirror the roster's casefold sort order from the backend — so "*NSYNC"
 * and "10cc" (which sort before letters) bucket to "#" alongside anything
 * else outside A-Z, and "Édith Piaf" buckets to "E" (the backend's
 * `list_artists` sorts on `normalize_artist_name` — NFKD accent-fold — as
 * its primary key precisely so this holds). Non-Latin scripts (CJK,
 * Cyrillic, ...) still fall outside A-Z here and bucket to "#" — the
 * backend sorts them after "z" too, so the "#" bucket at least stays
 * honest about what's actually on each page; a proper script-aware index
 * is out of scope.
 */
function bucketOf(name: string): string {
  const first = name
    .normalize("NFD")
    .replace(/\p{M}/gu, "")
    .charAt(0)
    .toUpperCase();
  return first >= "A" && first <= "Z" ? first : "#";
}

/**
 * A-Z jump strip above the Artists grid: one button per bucket letter (plus
 * "#" for symbols/digits), disabled when the roster has nothing in that
 * bucket. Clicking a letter jumps the page to wherever that letter's first
 * artist lives, using the same offset math as the pager. A letter reads as
 * pressed (SegmentedControl's active-tint treatment via `aria-pressed`)
 * exactly when an artist of that bucket is visible on the CURRENT page —
 * derived by bucketing the page slice itself, not just each bucket's first
 * occurrence, so a bucket spanning more than one page (e.g. 50+ artists all
 * starting with the same letter) reads pressed on every page it touches.
 *
 * Built to sit INSIDE the Artists toolbar band (a wrapping flex row), taking
 * the empty space to the left of the pager: hence `mr-auto` here rather than
 * at the call site — an auto margin beats the band's `justify-end`, so the
 * letters stay left and the pager stays right without stretching either.
 *
 * DEGRADATION IS WRAP, NOT SCROLL. 27 buttons stop fitting beside the pager
 * around a 1440px viewport, and this is a TARGETING control — you aim at a
 * remembered position rather than reading along it, and the pressed tint is
 * the strip's only "where am I" signal. A scroll box would hide the tail,
 * which on the last page is exactly the pressed run (V-Z), and there is no
 * scroll-into-view here to bring it back. So the strip keeps a content-sized
 * flex basis and the BAND breaks first: letters on one line, pager on the
 * next (what the owner asked for on mobile). Only when the strip alone is
 * wider than the page do its own buttons wrap onto extra rows. Every bucket
 * stays visible and in the same relative position at every width.
 */
export function AlphabetIndex({
  artists,
  pageSize,
  offset,
  onJump,
}: {
  artists: Artist[];
  pageSize: number;
  offset: number;
  onJump: (offset: number) => void;
}) {
  const firstIndex = useMemo(() => {
    const map = new Map<string, number>();
    artists.forEach((artist, index) => {
      const letter = bucketOf(artist.name);
      if (!map.has(letter)) map.set(letter, index);
    });
    return map;
  }, [artists]);

  const pressedLetters = useMemo(() => {
    const visible = artists.slice(offset, offset + pageSize);
    return new Set(visible.map((artist) => bucketOf(artist.name)));
  }, [artists, offset, pageSize]);

  return (
    <nav
      aria-label="Jump to artists by letter"
      className="mr-auto flex flex-wrap gap-0.5"
    >
      {LETTERS.map((letter) => {
        const index = firstIndex.get(letter);
        const isEmpty = index === undefined;
        const isPressed = pressedLetters.has(letter);
        return (
          <Button
            key={letter}
            type="button"
            variant="ghost"
            size="icon-sm"
            disabled={isEmpty}
            aria-label={`Jump to artists starting with ${letter}`}
            aria-pressed={isPressed}
            className={cn(
              isPressed && "bg-primary/15 text-primary-light",
            )}
            onClick={() => {
              if (index === undefined) return;
              onJump(Math.floor(index / pageSize) * pageSize);
            }}
          >
            {letter}
          </Button>
        );
      })}
    </nav>
  );
}
