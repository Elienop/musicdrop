// frontend/src/components/shell/ActivityPopover.tsx
//
// The topbar activity entry point (spec §2) — replaces the three deleted
// app-wide banners as the ONE place running/failed/done jobs surface.
// Failed rows persist here with an explicit dismiss (✕), never as permanent
// chrome. Stop actions are deliberately absent in Phase 2: the stop
// mutations stay on their settings panels until Phase 3, so JobProgress's
// onStop simply isn't passed.
import { useState } from "react";

import { useActivity, useActivityDismissals } from "@/api/useActivity";
import { Activity, Close } from "@/components/icons";
import { JobProgress } from "@/components/system/JobProgress";
import { Button } from "@/components/ui/button";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";

export function ActivityButton() {
  const { rows, runningCount } = useActivity();
  const { dismiss } = useActivityDismissals();
  // Controlled so a row's "View" deep link closes the popover as it
  // navigates (same posture as the mobile drawer) — otherwise it lingers
  // over the new page with keyboard focus trapped inside it.
  const [open, setOpen] = useState(false);

  // The icon-action dialect: tooltip text === aria-label, and the label
  // carries the live count so hover/SR get the same information as the
  // corner badge.
  const label =
    runningCount > 0 ? `Activity (${runningCount} running)` : "Activity";

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <Tooltip>
        <TooltipTrigger asChild>
          <PopoverTrigger asChild>
            <Button
              variant="ghost"
              size="icon-xl"
              aria-label={label}
              className="focus-ring text-muted-foreground hover:text-foreground relative"
            >
              {/* Same scale as the detail-rail icon actions: 56px hit area,
                  40px thin glyph. */}
              <Activity
                weight="thin"
                aria-hidden="true"
                className={cn(
                  "size-10",
                  runningCount > 0 && "motion-safe:animate-pulse",
                )}
              />
              {runningCount > 0 && (
                <span
                  aria-hidden="true"
                  className="bg-primary text-primary-foreground absolute top-1 right-1 flex size-4 items-center justify-center rounded-full text-[10px] font-semibold tabular-nums"
                >
                  {runningCount}
                </span>
              )}
            </Button>
          </PopoverTrigger>
        </TooltipTrigger>
        <TooltipContent>{label}</TooltipContent>
      </Tooltip>
      <PopoverContent
        align="end"
        className="w-96"
        onClickCapture={(event) => {
          // A row's "View" link navigates — close alongside it.
          if ((event.target as HTMLElement).closest("a") !== null) {
            setOpen(false);
          }
        }}
      >
        {rows.length === 0 ? (
          <p className="text-muted-foreground px-4 py-6 text-center text-sm">
            Nothing running.
          </p>
        ) : (
          <ul className="divide-border divide-y">
            {rows.map((row) => (
              <li key={row.id} className="flex items-start">
                <div className="min-w-0 flex-1">
                  <JobProgress
                    label={row.label}
                    scope={row.scope}
                    state={row.state}
                    progress={row.progress}
                    counts={row.countsText}
                    href={row.href}
                  />
                </div>
                {row.state === "failed" && (
                  <Button
                    variant="ghost"
                    size="icon-xs"
                    aria-label={`Dismiss ${row.label}`}
                    className="mt-3 mr-2 shrink-0"
                    onClick={() => dismiss(row.id)}
                  >
                    <Close aria-hidden="true" />
                  </Button>
                )}
              </li>
            ))}
          </ul>
        )}
        <p className="text-muted-foreground border-t px-4 py-2 text-xs">
          Downloads will appear here when Soulseek search ships.
        </p>
      </PopoverContent>
    </Popover>
  );
}
