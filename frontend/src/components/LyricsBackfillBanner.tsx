import { Loader2 } from "lucide-react";

import { useLyricsBackfillStatus } from "@/api/useLyricsBackfill";

/** A thin app-wide banner shown only while a lyrics backfill is running. */
export function LyricsBackfillBanner() {
  const { data } = useLyricsBackfillStatus();
  if (data?.phase !== "running") return null;
  return (
    <div
      className="border-border bg-muted/50 mx-auto flex max-w-7xl items-center gap-3 rounded-xl border px-4 py-2 text-sm"
      role="status"
    >
      <Loader2 className="text-muted-foreground size-4 shrink-0 animate-spin" aria-hidden="true" />
      <span className="flex-1">
        Backfilling lyrics… {data.processed} / {data.total}
      </span>
    </div>
  );
}
