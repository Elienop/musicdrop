import { useMemo, useRef, useState, type KeyboardEvent } from "react";

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
 *
 * ROVING TABINDEX — one Tab stop, arrows within. This EXTENDS the house rule
 * in segmented-control.tsx ("no roving tabindex — Tab moves between options,
 * which is correct for a short row of real <button>s") rather than
 * contradicting it: the rule holds while the row is short, and 27 buttons is
 * where it stops. The letters precede the pager in the DOM because that is
 * the layout, so plain Tab order would charge a keyboard user ~29 presses to
 * reach "Next page". The threshold is roughly a handful: keep plain Tab for a
 * segmented control's 2-5 options, rove once a group is a full alphabet.
 * ArrowLeft/Right step between FILLED buckets (empty ones are `disabled`,
 * hence unfocusable) without wrapping around; Home/End go to the first/last
 * filled bucket; Up/Down are left alone so the page still scrolls.
 *
 * WHY TWO NESTED ELEMENTS. Roving costs discoverability, and paying that
 * silently would be a regression only screen-reader users could see: before
 * it, all 27 buckets were reachable with the most obvious key on the
 * keyboard; after it, one button is tabbable and nothing says the arrows do
 * anything, so the strip can read as offering a single letter. Both of these
 * must hold at once:
 *
 *   1. the NAVIGATION LANDMARK survives — that is how assistive tech reaches
 *      a jump strip in the first place;
 *   2. the COMPOSITE is announced — `role="toolbar"` is the platform's way of
 *      saying "one tab stop, arrows move within".
 *
 * A role REPLACES an element's semantics rather than adding to it, so
 * `role="toolbar"` on the <nav> would buy (2) by destroying (1). They only
 * coexist on separate elements: landmark outside, toolbar inside. Collapsing
 * them back into one element silently drops whichever you didn't keep.
 */
export function AlphabetIndex({
  artists,
  pageSize,
  offset,
  onJump,
}: Readonly<{
  artists: Artist[];
  pageSize: number;
  offset: number;
  onJump: (offset: number) => void;
}> ) {
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

  // Only buckets WITH artists take part: the empty ones render `disabled`,
  // which already makes them unfocusable, so the arrows step over them.
  const filled = LETTERS.filter((letter) => firstIndex.has(letter));
  const buttons = useRef(new Map<string, HTMLButtonElement>());
  const [focused, setFocused] = useState<string | null>(null);

  // The one tabbable button: where the user last was, else the first bucket
  // ON THIS PAGE, else the first bucket at all — so Tab lands you where you
  // already are rather than at the far end of the alphabet.
  const tabStop =
    (focused !== null && firstIndex.has(focused) ? focused : undefined) ??
    filled.find((letter) => pressedLetters.has(letter)) ??
    filled[0];

  const moveTo = (letter: string | undefined) => {
    if (letter === undefined) return; // already at the end of the run
    setFocused(letter);
    buttons.current.get(letter)?.focus();
  };

  const onArrowKey = (e: KeyboardEvent, letter: string) => {
    const at = filled.indexOf(letter);
    if (e.key === "ArrowRight") moveTo(filled[at + 1]);
    else if (e.key === "ArrowLeft") moveTo(filled[at - 1]);
    else if (e.key === "Home") moveTo(filled[0]);
    else if (e.key === "End") moveTo(filled.at(-1));
    else return; // Up/Down and everything else keep their page behaviour
    e.preventDefault();
  };

  return (
    // TWO elements because there are two things to say and one element can
    // only say one of them: the landmark on the <nav>, the composite widget
    // on the container inside it. `role="toolbar"` here would REPLACE
    // navigation, not add to it. Don't collapse these.
    <nav aria-label="Jump to artists by letter" className="mr-auto">
      {/* Unnamed on purpose: the landmark immediately outside already carries
          this name, so labelling the toolbar too makes assistive tech read
          "jump to artists by letter, navigation, jump to artists by letter,
          toolbar" on the way in. (The house rule that a grouped control names
          itself — segmented-control.tsx — is for groups that stand alone.) */}
      <div
        role="toolbar"
        aria-orientation="horizontal"
        className="flex flex-wrap gap-0.5"
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
              tabIndex={letter === tabStop ? 0 : -1}
              ref={(node) => {
                if (node) buttons.current.set(letter, node);
                else buttons.current.delete(letter);
              }}
              onFocus={() => setFocused(letter)}
              onKeyDown={(e) => onArrowKey(e, letter)}
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
      </div>
    </nav>
  );
}
