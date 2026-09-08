// frontend/src/components/system/EmptyState.tsx
import type { ReactNode } from "react";

import type { AppIcon } from "@/components/icons";
import { cn } from "@/lib/utils";

/**
 * The one empty-state recipe (spec §4 — replaces 4 dialects): a centered
 * concept icon, a font-medium title, an optional muted body, and an optional
 * action slot (usually a CTA link — spec §5 wants every empty state to offer
 * one). `bordered` wraps it in the dashed card (roster/list surfaces, the
 * ArtistsPage recipe); the default is the bare py-24 column (full-page
 * idle/no-results surfaces, the SearchPage recipe).
 *
 * Intentionally no live-region role: an empty state is static page content.
 * Pages that need to announce it own the announcement.
 *
 * `body` takes a node, not just a string: it renders inside one `<p>`, so a
 * caller can pair a visible glyph with its `sr-only` spoken twin, or break a
 * value onto its own line. Pass phrasing content only (`<span>`, text) — a
 * `<div>` or `<p>` child is invalid inside the paragraph.
 *
 * `tone="destructive"` swaps the neutral chrome for the app's existing error
 * recipe — ErrorState's `border-destructive/40 bg-destructive/5` box (solid,
 * not dashed) and a `text-destructive` icon. It exists so an outcome panel
 * whose recovery is a navigation can read as a failure without ErrorState's
 * mandatory Retry. The tone colours the icon always and the box only when
 * `bordered`. Still no live-region role: a caller that needs the failure
 * announced owns the announcement.
 */
export function EmptyState({
  icon: Icon,
  title,
  body,
  action,
  bordered = false,
  tone = "neutral",
}: Readonly<{
  icon: AppIcon;
  title: string;
  body?: ReactNode;
  action?: ReactNode;
  bordered?: boolean;
  tone?: "neutral" | "destructive";
}> ) {
  const destructive = tone === "destructive";
  return (
    <div
      data-slot="empty-state"
      data-tone={tone}
      className={cn(
        "flex flex-col items-center gap-3 text-center",
        bordered ? "rounded-xl border py-16" : "py-24",
        bordered &&
          (destructive
            ? "border-destructive/40 bg-destructive/5"
            : "border-border border-dashed"),
      )}
    >
      <Icon
        className={cn(
          "size-10",
          destructive ? "text-destructive" : "text-muted-foreground",
        )}
        aria-hidden="true"
      />
      <div className="flex flex-col gap-1">
        <p className="font-medium">{title}</p>
        {body !== undefined && (
          <p className="text-muted-foreground text-sm">{body}</p>
        )}
      </div>
      {action}
    </div>
  );
}
