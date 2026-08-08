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

  const go = (next: number) => {
    if (busy) return; // in flight — keep focus, swallow the re-click
    onOffsetChange(next);
  };

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
        <span className="text-muted-foreground min-w-12 text-center text-sm tabular-nums">
          {busy ? (
            <>
              <Spinner
                className="inline size-4 animate-spin"
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
        </span>
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
