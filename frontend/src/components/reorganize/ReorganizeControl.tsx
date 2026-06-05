// frontend/src/components/reorganize/ReorganizeControl.tsx
import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import {
  type ReorganizeBackfillStatus,
  type ReorganizeMove,
  type ReorganizePlan,
  type ReorganizeScope,
  usePreviewReorganize,
  useReorganizeStatus,
  useStartReorganize,
  useStopReorganize,
} from "@/api/useReorganize";
import { useReorganizeNotice } from "@/components/reorganize/reorganizeNotice";
import { Button } from "@/components/ui/button";
import { useAutoDismiss } from "@/lib/useAutoDismiss";

function jobMatches(
  job: ReorganizeBackfillStatus | undefined,
  scope: ReorganizeScope,
): boolean {
  if (!job || job.scope == null) return false;
  if (scope.scope === "album")
    return job.scope === "album" && job.album_id === scope.albumId;
  if (scope.scope === "artist")
    return job.scope === "artist" && job.artist === scope.artist;
  return job.scope === "library";
}

function MoveRow({ m }: { m: ReorganizeMove }) {
  const renameInPlace = m.from_path === m.to_path;
  return (
    <li className="flex flex-col gap-0.5 border-b py-1.5 last:border-b-0">
      <span className="font-medium">{m.label}</span>
      {renameInPlace ? (
        <span className="text-muted-foreground text-xs">
          {m.track_count} file{m.track_count === 1 ? "" : "s"} renamed in place
          · {m.to_path}
        </span>
      ) : (
        <span className="text-muted-foreground truncate text-xs">
          {m.from_path} → {m.to_path}
        </span>
      )}
    </li>
  );
}

function PlanView({ plan }: { plan: ReorganizePlan }) {
  return (
    <div className="flex flex-col gap-2" aria-label="Reorganize preview">
      <p className="text-sm">
        <span className="font-medium">{plan.will_move} will move</span> ·{" "}
        {plan.already_in_place} already in place
      </p>
      {plan.moves.length > 0 && (
        <ul className="max-h-72 overflow-auto rounded-md border px-3 text-sm">
          {plan.moves.map((m, i) => (
            <MoveRow key={`${m.label}-${i}`} m={m} />
          ))}
          {plan.truncated && (
            <li className="text-muted-foreground py-1.5 text-xs">
              + {plan.will_move - plan.moves.length} more…
            </li>
          )}
        </ul>
      )}
    </div>
  );
}

export function ReorganizeControl({ scope }: { scope: ReorganizeScope }) {
  const queryClient = useQueryClient();
  const status = useReorganizeStatus();
  const preview = usePreviewReorganize();
  const start = useStartReorganize();
  const stop = useStopReorganize();
  const { showNotice } = useReorganizeNotice();
  const [plan, setPlan] = useState<ReorganizePlan | null>(null);

  const job = status.data;
  const phase = job?.phase;
  const isThis = jobMatches(job, scope);
  const runningThis = phase === "running" && isThis;
  const otherRunning = phase === "running" && !isThis;
  const terminalThis =
    isThis && (phase === "done" || phase === "stopped" || phase === "failed");
  // The finished-job tally fades ~8s after it completes instead of lingering.
  const showTally = useAutoDismiss(terminalThis, job?.job_id ?? null);

  // Files moved + DB paths changed — refresh the album/artist rosters that show
  // those paths once THIS scope's job reaches a terminal state.
  useEffect(() => {
    if (
      isThis &&
      (phase === "done" || phase === "stopped" || phase === "failed")
    ) {
      void queryClient.invalidateQueries({ queryKey: ["album"] });
      void queryClient.invalidateQueries({ queryKey: ["albums"] });
      if (scope.scope === "library") {
        void queryClient.invalidateQueries({ queryKey: ["artists"] });
      }
    }
  }, [isThis, phase, scope.scope, queryClient]);

  // A preview with moves opens the inline review; an empty preview raises a
  // transient banner notice instead (no inline message, no Done button).
  function onPreviewed(result: ReorganizePlan) {
    if (result.will_move === 0) {
      showNotice(
        "Nothing to reorganize — everything already matches your config.",
      );
    } else {
      setPlan(result);
    }
  }

  return (
    <div className="flex flex-col items-start gap-2">
      {/* Messages (top): preview / terminal tally / errors. Live running
          progress lives in the app banner (ReorganizeBanner), not inline. */}
      {!runningThis &&
        showTally &&
        job &&
        (job.phase === "done" || job.phase === "stopped") && (
          <span className="text-muted-foreground text-sm" role="status">
            {job.phase === "done" ? "Done" : "Stopped"} — moved {job.moved} ·
            skipped {job.skipped} · failed {job.failed}
          </span>
        )}
      {!runningThis && showTally && job?.phase === "failed" && (
        <span className="text-destructive text-sm" role="alert">
          Reorganize failed{job.error ? `: ${job.error}` : "."}
        </span>
      )}
      {otherRunning && (
        <span className="text-muted-foreground text-sm">
          another library job is running
        </span>
      )}
      {plan != null && <PlanView plan={plan} />}
      {preview.isError && (
        <span className="text-destructive text-sm" role="alert">
          {(preview.error as Error).message}
        </span>
      )}
      {start.isError && (
        <span className="text-destructive text-sm" role="alert">
          {(start.error as Error).message}
        </span>
      )}

      {/* Buttons (bottom row). */}
      <div className="flex flex-wrap items-center gap-3">
        {runningThis ? (
          <Button
            variant="outline"
            size="sm"
            onClick={() => stop.mutate()}
            disabled={stop.isPending}
          >
            Stop
          </Button>
        ) : plan == null ? (
          <Button
            variant="outline"
            size="sm"
            disabled={preview.isPending || otherRunning}
            onClick={() => preview.mutate(scope, { onSuccess: onPreviewed })}
          >
            {preview.isPending ? "Building preview…" : "Preview reorganize"}
          </Button>
        ) : (
          <>
            <Button
              size="sm"
              disabled={start.isPending || otherRunning}
              onClick={() =>
                start.mutate(scope, { onSuccess: () => setPlan(null) })
              }
            >
              {start.isPending
                ? "Starting…"
                : `Reorganize ${plan.will_move} item${plan.will_move === 1 ? "" : "s"}`}
            </Button>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => setPlan(null)}
              disabled={start.isPending}
            >
              Cancel
            </Button>
          </>
        )}
      </div>
    </div>
  );
}
