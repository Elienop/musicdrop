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
 */
export function EmptyState({
  icon: Icon,
  title,
  body,
  action,
  bordered = false,
}: Readonly<{
  icon: AppIcon;
  title: string;
  body?: string;
  action?: ReactNode;
  bordered?: boolean;
}> ) {
  return (
    <div
      data-slot="empty-state"
      className={cn(
        "flex flex-col items-center gap-3 text-center",
        bordered
          ? "border-border rounded-xl border border-dashed py-16"
          : "py-24",
      )}
    >
      <Icon className="text-muted-foreground size-10" aria-hidden="true" />
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
