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
 * Built to sit INSIDE the Artists toolbar band (flex row), taking the empty
 * space left of the size select and pager: hence `flex-1 min-w-0` here
 * rather than at the call site. 27 buttons never fit beside those controls
 * below a wide desktop, and it must not wrap — a second line is the row the
 * band exists to reclaim — so it degrades by scrolling sideways, keeping
 * every bucket reachable at every width (drag, shift-wheel, or Tab, which
 * scrolls the next button into view). `py-1 -my-1` gives the focus ring room
 * inside the scroll box without the strip standing taller than the h-8
 * controls beside it; `-ml-1` cancels the leading padding so "#" still lines
 * up with the grid's left edge, while the trailing `px-1` stays as breathing
 * room before the size select. `scroll-px-1` keeps a Tab-scrolled button's
 * ring off the clipped edge.
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
      className="-my-1 -ml-1 flex min-w-0 flex-1 scroll-px-1 gap-0.5 overflow-x-auto px-1 py-1 [scrollbar-width:thin]"
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
