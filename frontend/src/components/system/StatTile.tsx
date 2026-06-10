import type { AppIcon } from "@/components/icons";
import { Card, CardContent } from "@/components/ui/card";

/**
 * One dashboard stat: muted icon+label line over a big tabular-nums value,
 * optional hint below. Owns exactly ONE dense padding (`py-4` Card /
 * `px-4` content) — replacing the LibraryDashboard recipe whose default
 * `py-6` Card fought a `p-4` CardContent.
 *
 * Skeleton contract: the hint-less tile's content height is fixed at 88px
 * (16+16 padding, 20px label line, 4px gap, 32px value line) — a loading
 * placeholder should use `h-22` (5.5rem = 88px) to avoid layout shift.
 */
export function StatTile({
  icon: Icon,
  label,
  value,
  hint,
}: {
  icon: AppIcon;
  label: string;
  value: string;
  hint?: string;
}) {
  return (
    <Card className="gap-0 py-4">
      <CardContent className="flex flex-col gap-1 px-4">
        <span className="text-muted-foreground flex items-center gap-1.5 text-sm">
          <Icon className="size-4" aria-hidden="true" />
          {label}
        </span>
        <span className="text-2xl font-semibold tracking-tight tabular-nums">
          {value}
        </span>
        {hint !== undefined && (
          <span className="text-muted-foreground text-xs">{hint}</span>
        )}
      </CardContent>
    </Card>
  );
}
