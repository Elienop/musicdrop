import type { ReactNode } from "react";
import { Link } from "react-router";

import {
  Error as ErrorIcon,
  Forward,
  Spinner,
  Stop,
  Success,
} from "@/components/icons";
import { Button } from "@/components/ui/button";

/**
 * One activity row (spec §2) — the unit the activity popover renders per
 * job, and that page-scoped job echoes reuse. The state chip always pairs
 * the icon with a text label (Running/Failed/Done) so state is never
 * color-only. The live region (`role="status"`) is scoped to the StateChip
 * only — never the whole row: a row-wide region would atomically re-announce
 * the Stop/View controls on every progress tick, and interactive content
 * inside a live region is a known screen-reader hazard. State TRANSITIONS
 * (running → done/failed) announce; per-tick n/total churn stays silent.
 */
export function JobProgress({
  label,
  scope,
  state,
  progress,
  counts,
  onStop,
  href,
}: Readonly<{
  label: string;
  scope?: string;
  state: "running" | "failed" | "done";
  progress?: { done: number; total: number };
  counts?: ReactNode;
  onStop?: () => void;
  href?: string;
}> ) {
  const pct =
    progress && progress.total > 0
      ? Math.min(100, Math.round((progress.done / progress.total) * 100))
      : 0;
  return (
    <div className="flex min-w-0 flex-col gap-2 px-4 py-3">
      <div className="flex min-w-0 items-center gap-3">
        <div className="flex min-w-0 flex-1 flex-col">
          <span className="truncate text-sm font-medium" title={label}>
            {label}
          </span>
          {scope !== undefined && (
            <span
              className="text-muted-foreground truncate text-xs"
              title={scope}
            >
              {scope}
            </span>
          )}
        </div>
        <StateChip state={state} />
        {onStop !== undefined && (
          // `sm`, not the row's `xs`, and no size class on the glyph: this is
          // the app's one stop register — every other Stop/Pause (the run
          // page's "Stop this run", the sweep's "Pause sweep", Reorganize, Disk
          // sync, Artist art, album lyrics) is `outline sm`. The register is
          // also what puts the glyph on the spec's inline 16 step: `xs`
          // overrides the button's svg token to `size-3`, and a 12px box gives
          // Phosphor's light stroke (12u of a 256 viewBox) 0.563px — a grey
          // hairline at 1x, and off the 16/20/40 scale entirely.
          <Button variant="outline" size="sm" onClick={onStop}>
            <Stop aria-hidden="true" />
            Stop
          </Button>
        )}
        {href !== undefined && (
          <Button variant="ghost" size="xs" asChild>
            <Link to={href}>
              View
              {/* Sized for the same reason as the popover's dismiss glyph:
                  `xs` would render it 12px. Owner's call 2026-09-20. */}
              <Forward aria-hidden="true" className="size-4" />
            </Link>
          </Button>
        )}
      </div>
      {progress && (
        <div className="flex items-center gap-3">
          <div
            className="bg-muted h-1.5 min-w-0 flex-1 overflow-hidden rounded-full"
            aria-hidden="true"
          >
            <div
              className="bg-primary h-full rounded-full transition-[width]"
              style={{ width: `${pct}%` }}
            />
          </div>
          <span className="text-muted-foreground shrink-0 text-xs tabular-nums">
            {progress.done} / {progress.total}
          </span>
        </div>
      )}
      {counts !== undefined && (
        <div className="text-muted-foreground text-xs">{counts}</div>
      )}
    </div>
  );
}

/** State chip: one glyph + the state WORD — text always carries it. The
 * chip is the row's live region (role=status), so transitions announce
 * without re-reading the surrounding controls. */
function StateChip({ state }: Readonly<{ state: "running" | "failed" | "done" }>) {
  if (state === "running") {
    return (
      <output
        className="text-muted-foreground flex shrink-0 items-center gap-1 text-xs font-medium"
      >
        <Spinner className="size-4 animate-spin" aria-hidden="true" />
        Running
      </output>
    );
  }
  if (state === "failed") {
    return (
      <output
        className="text-destructive flex shrink-0 items-center gap-1 text-xs font-medium"
      >
        <ErrorIcon className="size-4" aria-hidden="true" />
        Failed
      </output>
    );
  }
  return (
    <output
      className="text-success flex shrink-0 items-center gap-1 text-xs font-medium"
    >
      <Success className="size-4" aria-hidden="true" />
      Done
    </output>
  );
}
