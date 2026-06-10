// frontend/src/components/system/StatusBanner.tsx
import { cva } from "class-variance-authority";
import type { ReactNode } from "react";

import type { AppIcon } from "@/components/icons";
import { cn } from "@/lib/utils";

const bannerVariants = cva(
  "flex items-center gap-3 rounded-xl border p-3 text-sm",
  {
    variants: {
      tone: {
        neutral: "border-border bg-muted/50",
        warning: "border-warning/50 bg-warning/10",
        destructive: "border-destructive/40 bg-destructive/5",
      },
    },
  },
);

const iconToneClass: Record<"neutral" | "warning" | "destructive", string> = {
  neutral: "text-muted-foreground",
  warning: "text-warning",
  destructive: "text-destructive",
};

/**
 * The one banner recipe (spec §4 — generalizes the hand-copied banner
 * markup of the pre-Phase-2 app banners, since deleted, and AlbumEditPanel
 * MoveNotice's --warning tokens). Tones map to roles: neutral is ambient
 * (`role="status"`); warning and destructive demand attention
 * (`role="alert"`).
 *
 * Layout margin belongs to the CALLER (the old banners hardcoded mb-6) —
 * the banner is just the box. `icon` is a concept icon from icons.ts;
 * `action` is a trailing slot (e.g. a Resume button).
 */
export function StatusBanner({
  tone,
  icon: Icon,
  action,
  children,
}: {
  tone: "neutral" | "warning" | "destructive";
  icon?: AppIcon;
  action?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div
      data-slot="status-banner"
      role={tone === "neutral" ? "status" : "alert"}
      className={bannerVariants({ tone })}
    >
      {Icon !== undefined && (
        <Icon
          className={cn("size-5 shrink-0", iconToneClass[tone])}
          aria-hidden="true"
        />
      )}
      <div className="min-w-0 flex-1">{children}</div>
      {action}
    </div>
  );
}
