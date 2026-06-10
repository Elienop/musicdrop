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
import { Button } from "@/components/ui/button";

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

/** Action-local feedback: an empty-preview result is informational
 * (`role="status"`), a failed action is an error (`role="alert"`) — the
 * inline-span idiom LyricsBackfillPanel uses, so the result lives next to the
 * button that caused it instead of in app-wide chrome. */
type ActionMessage = { kind: "info" | "error"; text: string };

export function ReorganizeControl({ scope }: { scope: ReorganizeScope }) {
  const queryClient = useQueryClient();
  const status = useReorganizeStatus();
  const preview = usePreviewReorganize();
  const start = useStartReorganize();
  const stop = useStopReorganize();
  const [plan, setPlan] = useState<ReorganizePlan | null>(null);
  const [message, setMessage] = useState<ActionMessage | null>(null);

  const job = status.data;
  const phase = job?.phase;
  const isThis = jobMatches(job, scope);
  const runningThis = phase === "running" && isThis;
  const otherRunning = phase === "running" && !isThis;

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

  // A preview with moves opens the inline review; an empty preview shows an
  // inline info note instead (no inline plan, no Done button).
  function onPreviewed(result: ReorganizePlan) {
    if (result.will_move === 0) {
      setMessage({
        kind: "info",
        text: "Nothing to reorganize — everything already matches your config.",
      });
    } else {
      setPlan(result);
    }
  }

  // Errors surface inline next to the buttons too — running progress and the
  // backstage failed state live in the topbar activity popover.
  function onActionError(e: unknown) {
    setMessage({ kind: "error", text: (e as Error).message });
  }

  return (
    <div className="flex flex-col items-start gap-2">
      {plan != null && <PlanView plan={plan} />}
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
            onClick={() => {
              setMessage(null);
              preview.mutate(scope, {
                onSuccess: onPreviewed,
                onError: onActionError,
              });
            }}
          >
            {preview.isPending ? "Building preview…" : "Preview reorganize"}
          </Button>
        ) : (
          <>
            <Button
              size="sm"
              disabled={start.isPending || otherRunning}
              onClick={() => {
                setMessage(null);
                start.mutate(scope, {
                  onSuccess: () => setPlan(null),
                  onError: onActionError,
                });
              }}
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
        {message != null && (
          <span
            role={message.kind === "error" ? "alert" : "status"}
            className={
              message.kind === "error"
                ? "text-destructive text-sm"
                : "text-muted-foreground text-sm"
            }
          >
            {message.text}
          </span>
        )}
      </div>
    </div>
  );
}
