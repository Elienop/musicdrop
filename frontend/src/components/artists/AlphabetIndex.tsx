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
 * else outside A-Z, and "Édith Piaf" buckets to "E".
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
 * artist lives, using the same offset math as the pager. Letters whose
 * bucket falls on the currently visible page get the SegmentedControl
 * active-tint treatment via `aria-pressed`.
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

  return (
    <nav
      aria-label="Jump to artists by letter"
      className="flex flex-wrap gap-0.5"
    >
      {LETTERS.map((letter) => {
        const index = firstIndex.get(letter);
        const isEmpty = index === undefined;
        const isPressed =
          !isEmpty && index >= offset && index < offset + pageSize;
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
