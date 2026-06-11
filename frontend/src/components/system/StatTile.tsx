import type { AppIcon } from "@/components/icons";

/**
 * One dashboard stat — cardless (the Koito stat-line direction): a LARGE
 * muted icon on the left spanning both text lines, then a column of the
 * big tabular value over its muted label (+ optional hint). No border or
 * surface — the stats read as typography on the page, not as boxes.
 *
 * Skeleton contract: the row is 56px tall — the `size-14` icon is the
 * tallest child (deliberately a touch taller than the two text lines, per
 * design) — so loading placeholders use `h-14` to avoid layout shift.
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
    <div className="flex items-center gap-3">
      <Icon
        className="text-muted-foreground size-14 shrink-0"
        aria-hidden="true"
      />
      <div className="flex min-w-0 flex-col">
        <span className="text-2xl font-semibold tracking-tight tabular-nums">
          {value}
        </span>
        <span className="text-muted-foreground truncate text-sm">{label}</span>
        {hint !== undefined && (
          <span className="text-muted-foreground text-xs">{hint}</span>
        )}
      </div>
    </div>
  );
}
