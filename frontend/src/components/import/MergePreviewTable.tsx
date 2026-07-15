import { Fragment } from "react";

import type { DuplicateTrackRow, MergePreview } from "@/api/useImport";
import { SectionLabel } from "@/components/system/SectionLabel";
import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { cn } from "@/lib/utils";

/** "FLAC · 1000", "MP3" (no bitrate), or an em-dash when that side lacks the track. */
function quality(fmt: string | null, kbps: number | null): string {
  if (!fmt) return "-";
  return kbps ? `${fmt} · ${kbps}` : fmt;
}

/** Per-state badge; library_only / same carry no badge (no actionable change). */
const BADGE: Partial<Record<DuplicateTrackRow["state"], string>> = {
  added: "+ adds",
  upgrade: "↑ upgrade",
  downgrade: "↓ lower",
  missing: "missing",
};

function summarise(p: MergePreview): string {
  const parts = [`${p.total} tracks`];
  if (p.added_count) parts.push(`${p.added_count} added`);
  if (p.upgrade_count) {
    parts.push(`${p.upgrade_count} ${p.upgrade_count === 1 ? "upgrade" : "upgrades"}`);
  }
  if (p.missing_count) parts.push(`${p.missing_count} still missing`);
  return parts.join(" · ");
}

/**
 * The per-track before/after comparison: every release position with your
 * library copy beside the incoming import. Makes the duplicate decision legible
 * — many `+ adds` favours Merge, `↑ upgrade` rows favour Replace, all-unchanged
 * favours Skip. Pure render over a MergePreview (rows pre-ordered by position).
 */
export function MergePreviewTable({ preview }: { preview: MergePreview }) {
  const multiDisc = new Set(preview.rows.map((r) => r.disc)).size > 1;
  return (
    <section aria-label="Track comparison" className="flex flex-col gap-3">
      <div className="flex flex-col gap-1">
        <SectionLabel>Track comparison</SectionLabel>
        <p className="text-muted-foreground text-sm">{summarise(preview)}</p>
      </div>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead className="w-12 pr-4 text-right">#</TableHead>
            <TableHead>Title</TableHead>
            <TableHead>In your library</TableHead>
            <TableHead>This import</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {preview.rows.map((row, i) => {
            const showHeader = multiDisc && (i === 0 || preview.rows[i - 1].disc !== row.disc);
            const highlight = row.state === "added" || row.state === "upgrade";
            const faded = row.state === "missing";
            const badge = BADGE[row.state];
            return (
              <Fragment key={row.position}>
                {showHeader && (
                  <TableRow className="hover:bg-transparent">
                    <TableCell
                      colSpan={4}
                      className="text-muted-foreground pt-4 text-xs font-medium tracking-wide uppercase"
                    >
                      Disc {row.disc}
                    </TableCell>
                  </TableRow>
                )}
                <TableRow className={cn("hover:bg-transparent", highlight && "bg-primary/5")}>
                  <TableCell className="text-muted-foreground pr-4 text-right tabular-nums">
                    {row.position}
                  </TableCell>
                  <TableCell>
                    <span className="flex min-w-0 items-center gap-2">
                      <span
                        className={cn(
                          "truncate",
                          faded && "text-muted-foreground italic",
                          highlight && "font-medium",
                        )}
                      >
                        {row.title}
                      </span>
                      {badge && (
                        <Badge variant="secondary" className="shrink-0">
                          {badge}
                        </Badge>
                      )}
                    </span>
                  </TableCell>
                  <TableCell className="text-muted-foreground tabular-nums">
                    {quality(row.library_format, row.library_bitrate_kbps)}
                  </TableCell>
                  <TableCell className="text-muted-foreground tabular-nums">
                    {quality(row.import_format, row.import_bitrate_kbps)}
                  </TableCell>
                </TableRow>
              </Fragment>
            );
          })}
        </TableBody>
      </Table>
    </section>
  );
}
