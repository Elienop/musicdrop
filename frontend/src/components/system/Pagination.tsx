import { useEffect, useRef, useState } from "react";

import {
  Back,
  Expand,
  Forward,
  SkipBack,
  SkipForward,
  Spinner,
} from "@/components/icons";
import { Button } from "@/components/ui/button";
import { pageWindow } from "@/lib/pageWindow";

/** The default library page size — surfaces without a size selector use it. */
export const PAGE_SIZE = 48;

/** Selectable page sizes (multiples of the default; 192 < backend's 200 cap). */
export const PAGE_SIZE_OPTIONS = [48, 96, 192] as const;

/** One of the selectable page sizes. */
export type PageSize = (typeof PAGE_SIZE_OPTIONS)[number];

/**
 * The single pagination control. Buttons disable at BOUNDS only — never
 * while busy (disabling mid-flight strands keyboard focus on <body>);
 * instead re-clicks are ignored while `busy`, and the page readout swaps
 * for the spinner under `aria-busy`.
 *
 * `compact` is the toolbar form: icon-only buttons and a terse `2 / 67`
 * readout, for the row above the grid. `label` disambiguates the landmark
 * when a page mounts two pagers (e.g. "Pagination (top)").
 *
 * Deliberately NO scrolling or focus management here: callers own what
 * happens after a page change (scroll-to-top, focus the results region),
 * so the control works identically in grids, tables and popovers.
 */
export function Pagination({
  total,
  offset,
  limit,
  onOffsetChange,
  busy = false,
  compact = false,
  label = "Pagination",
}: {
  total: number;
  offset: number;
  limit: number;
  onOffsetChange: (o: number) => void;
  busy?: boolean;
  compact?: boolean;
  label?: string;
}) {
  const totalPages = Math.max(1, Math.ceil(total / limit));
  const page = Math.min(totalPages, Math.floor(offset / limit) + 1);
  const canPrev = offset > 0;
  const canNext = offset + limit < total;
  const [editing, setEditing] = useState(false);
  const goToPageRef = useRef<HTMLButtonElement>(null);
  const wasEditingRef = useRef(editing);

  const go = (next: number) => {
    if (busy) return; // in flight — keep focus, swallow the re-click
    onOffsetChange(next);
  };

  // Commit (Enter), Escape, and blur-revert all just flip `editing` back to
  // false and let the input unmount — none of them re-focuses anything on
  // their own, which would otherwise strand keyboard focus on <body> (the
  // one thing this component's buttons are careful never to do). Catch the
  // true -> false transition here instead, for all three exit paths at once.
  //
  // The setTimeout(0) is load-bearing, not decorative: focusing the ghost
  // Button synchronously (i.e. in this effect's own tick) races the still-
  // in-flight Enter keystroke that triggered the commit. userEvent's Enter
  // handling re-reads document.activeElement right after the keydown's
  // effects flush and synthesizes a click on whatever is now focused if
  // it's Enter-activatable (see @testing-library/user-event's keyboard
  // system) — so a same-tick .focus() on this <button> makes that same
  // Enter press immediately re-click it, reopening the input it just
  // closed. Deferring one macrotask lets that keystroke's handling finish
  // — against the original input — before focus moves.
  //
  // The activeElement === body check guards a second race: when the input
  // closes via onBlur because the user clicked a DIFFERENT focusable
  // element (not Escape, not Enter — genuinely moving on), that element
  // already owns focus by the time this timeout fires. Refocusing
  // unconditionally would yank focus back off it a tick later. Only claim
  // focus if nothing else did — i.e. it's still sitting on <body>, which is
  // where an unmounted input's focus falls back to.
  useEffect(() => {
    if (wasEditingRef.current && !editing) {
      const id = setTimeout(() => {
        if (document.activeElement === document.body) {
          goToPageRef.current?.focus();
        }
      }, 0);
      wasEditingRef.current = editing;
      return () => clearTimeout(id);
    }
    wasEditingRef.current = editing;
  }, [editing]);

  if (compact) {
    return (
      <nav
        aria-label={label}
        aria-busy={busy}
        className="flex shrink-0 items-center gap-1"
      >
        <Button
          type="button"
          variant="outline"
          size="icon-sm"
          disabled={!canPrev}
          aria-label="First page"
          onClick={() => go(0)}
        >
          <SkipBack aria-hidden="true" />
        </Button>
        <Button
          type="button"
          variant="outline"
          size="icon-sm"
          disabled={!canPrev}
          aria-label="Previous page"
          onClick={() => go(Math.max(0, offset - limit))}
        >
          <Back aria-hidden="true" />
        </Button>
        {editing ? (
          <input
            type="number"
            min={1}
            max={totalPages}
            defaultValue={page}
            autoFocus
            aria-label={`Go to page (1–${totalPages})`}
            className="border-input bg-background focus-visible:border-ring focus-visible:ring-ring/50 h-8 w-14 rounded-md border px-1 text-center text-sm tabular-nums shadow-xs focus-visible:ring-[3px] focus-visible:outline-none"
            onFocus={(e) => e.currentTarget.select()}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                const n = Number((e.target as HTMLInputElement).value);
                setEditing(false);
                if (Number.isFinite(n) && n >= 1)
                  go(
                    (Math.min(totalPages, Math.max(1, Math.round(n))) - 1) *
                      limit,
                  );
              } else if (e.key === "Escape") {
                setEditing(false);
              }
            }}
            onBlur={() => setEditing(false)}
          />
        ) : (
          // Mounted in BOTH busy and idle states — only its CONTENT swaps.
          // A caller's onOffsetChange can flip `busy` true synchronously in
          // the same render that closes `editing` (e.g. BrowsePage's
          // isFetching, set inside the same state update as the new
          // offset), so this can never be the thing that disappears on
          // commit: goToPageRef needs a live node across that transition
          // for the deferred refocus effect above to land on.
          <Button
            ref={goToPageRef}
            type="button"
            variant="ghost"
            size="sm"
            className="min-w-12 tabular-nums"
            aria-label={`Go to page (1–${totalPages})`}
            onClick={() => {
              if (!busy) setEditing(true); // no-op re-click while in flight
            }}
          >
            {busy ? (
              <>
                <Spinner
                  className="size-4 animate-spin"
                  aria-hidden="true"
                />
                <span className="sr-only">Loading page&hellip;</span>
              </>
            ) : (
              <>
                <span className="sr-only">Page </span>
                {page} / {totalPages}
              </>
            )}
          </Button>
        )}
        <Button
          type="button"
          variant="outline"
          size="icon-sm"
          disabled={!canNext}
          aria-label="Next page"
          onClick={() => go(offset + limit)}
        >
          <Forward aria-hidden="true" />
        </Button>
        <Button
          type="button"
          variant="outline"
          size="icon-sm"
          disabled={!canNext}
          aria-label="Last page"
          onClick={() => go((totalPages - 1) * limit)}
        >
          <SkipForward aria-hidden="true" />
        </Button>
      </nav>
    );
  }

  return (
    <nav
      aria-label={label}
      aria-busy={busy}
      className="flex items-center justify-between gap-4"
    >
      <Button
        type="button"
        variant="outline"
        size="sm"
        disabled={!canPrev}
        onClick={() => go(Math.max(0, offset - limit))}
      >
        <Back aria-hidden="true" />
        Previous
      </Button>
      <span className="text-muted-foreground flex items-center gap-2 text-sm tabular-nums sm:hidden">
        {busy ? (
          <>
            <Spinner className="size-4 animate-spin" aria-hidden="true" />
            <span className="sr-only">Loading page&hellip;</span>
          </>
        ) : (
          <>
            Page {page} of {totalPages}
          </>
        )}
      </span>
      <span className="hidden items-center gap-1 sm:flex">
        {busy ? (
          <>
            <Spinner className="size-4 animate-spin" aria-hidden="true" />
            <span className="sr-only">Loading page&hellip;</span>
          </>
        ) : (
          <>
            <span className="sr-only">
              <span>Page </span>
              <span>{page}</span>
              <span> of </span>
              <span>{totalPages}</span>
            </span>
            {pageWindow(page, totalPages).map((entry, i) =>
              entry === "gap" ? (
                <span
                  key={`gap-${i}`}
                  aria-hidden="true"
                  className="text-muted-foreground px-1"
                >
                  &hellip;
                </span>
              ) : (
                <Button
                  key={entry}
                  type="button"
                  size="icon-sm"
                  variant={entry === page ? "secondary" : "ghost"}
                  aria-label={`Page ${entry}`}
                  aria-current={entry === page ? "page" : undefined}
                  className="tabular-nums"
                  onClick={() => go((entry - 1) * limit)}
                >
                  {entry}
                </Button>
              ),
            )}
          </>
        )}
      </span>
      <Button
        type="button"
        variant="outline"
        size="sm"
        disabled={!canNext}
        onClick={() => go(offset + limit)}
      >
        Next
        <Forward aria-hidden="true" />
      </Button>
    </nav>
  );
}

/**
 * Page-size picker for the paginated grids — a native select (matches the
 * Browse sort select's anatomy) over PAGE_SIZE_OPTIONS. The caller persists
 * the choice (see usePageSize); this is a controlled dumb control.
 */
export function PageSizeSelect({
  value,
  onChange,
}: {
  value: PageSize;
  onChange: (n: PageSize) => void;
}) {
  return (
    // The CandidateReview select anatomy: appearance-none + reserved right
    // padding + our own caret, because Blink draws the UA arrow flush
    // against the edge (cramped at toolbar size).
    <span className="relative shrink-0">
      <select
        aria-label="Results per page"
        className="border-input bg-background focus-visible:border-ring focus-visible:ring-ring/50 h-8 appearance-none rounded-md border px-2 pr-7 text-sm shadow-xs focus-visible:ring-[3px] focus-visible:outline-none"
        value={value}
        onChange={(e) => onChange(Number(e.target.value) as PageSize)}
      >
        {PAGE_SIZE_OPTIONS.map((n) => (
          <option key={n} value={n}>
            {n} per page
          </option>
        ))}
      </select>
      <Expand
        className="text-muted-foreground pointer-events-none absolute top-1/2 right-2 size-4 -translate-y-1/2"
        aria-hidden="true"
      />
    </span>
  );
}
