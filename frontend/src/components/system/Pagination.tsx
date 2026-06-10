import { Back, Forward, Spinner } from "@/components/icons";
import { Button } from "@/components/ui/button";

/** The one library page size — every paginated surface uses it. */
export const PAGE_SIZE = 48;

/**
 * The single pagination control. Buttons disable at BOUNDS only — never
 * while busy (disabling mid-flight strands keyboard focus on <body>);
 * instead re-clicks are ignored while `busy`, and the page readout swaps
 * for the spinner under `aria-busy`.
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
}: {
  total: number;
  offset: number;
  limit: number;
  onOffsetChange: (o: number) => void;
  busy?: boolean;
}) {
  const totalPages = Math.max(1, Math.ceil(total / limit));
  const page = Math.min(totalPages, Math.floor(offset / limit) + 1);
  const canPrev = offset > 0;
  const canNext = offset + limit < total;

  const go = (next: number) => {
    if (busy) return; // in flight — keep focus, swallow the re-click
    onOffsetChange(next);
  };

  return (
    <nav
      aria-label="Pagination"
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
      <span className="text-muted-foreground flex items-center gap-2 text-sm tabular-nums">
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
