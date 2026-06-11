import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

/**
 * THE page header — exactly one per route. Renders the page `h1` at the
 * `page` type scale with `tabIndex={-1}` so RouteAnnouncer can move focus to
 * it after navigation, plus a meta/count line and a right-aligned actions
 * slot. The meta line is an always-mounted polite live region (the
 * ArtistsPage idiom): assistive tech must observe the region before the
 * async count arrives, so only the TEXT toggles, never the element.
 */
export function PageHeader({
  title,
  meta,
  actions,
}: {
  title: string;
  meta?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <header className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
      <div className="flex min-w-0 flex-col gap-1">
        {/* Display face + fluid `text-display` scale (clamp-based token) —
            the Koito hierarchy: ONE big element per page, semibold not bold,
            everything else stays small. */}
        <h1
          tabIndex={-1}
          className="font-display text-display font-semibold tracking-tight"
        >
          {title}
        </h1>
        {/* min-h-5 keeps the line from collapsing/shifting when meta lands;
            tabular-nums pins the meta dialect for counts (spec §3). */}
        <p
          className="text-muted-foreground min-h-5 text-sm tabular-nums"
          aria-live="polite"
        >
          {meta}
        </p>
      </div>
      {actions !== undefined && (
        <div className="flex shrink-0 items-center gap-2">{actions}</div>
      )}
    </header>
  );
}

/**
 * Page content width wrapper — exactly two named widths in the app:
 * "default" (full shell width) and "narrow" (centered max-w-3xl reading
 * column, e.g. the Review page). No ad-hoc per-page width CSS.
 */
export function PageBody({
  variant = "default",
  children,
}: {
  variant?: "default" | "narrow";
  children: ReactNode;
}) {
  return (
    <div
      className={cn(
        "flex w-full flex-col gap-6",
        variant === "narrow" && "mx-auto max-w-3xl",
      )}
    >
      {children}
    </div>
  );
}
