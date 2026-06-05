// frontend/src/components/reorganize/ReorganizeControl.tsx
import { Loader2 } from "lucide-react";
import { useState } from "react";

import {
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
  job: { artist: string | null; album_id: number | null } | undefined,
  scope: ReorganizeScope,
): boolean {
  if (!job) return false;
  if (scope.scope === "album") return job.album_id === scope.albumId;
  if (scope.scope === "artist") return job.album_id === null && job.artist === scope.artist;
  return job.album_id === null && job.artist === null;
}

function MoveRow({ m }: { m: ReorganizeMove }) {
  const renameInPlace = m.from_path === m.to_path;
  return (
    <li className="flex flex-col gap-0.5 border-b py-1.5 last:border-b-0">
      <span className="font-medium">{m.label}</span>
      {renameInPlace ? (
        <span className="text-muted-foreground text-xs">
          {m.track_count} file{m.track_count === 1 ? "" : "s"} renamed in place · {m.to_path}
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
  const status = useReorganizeStatus();
  const preview = usePreviewReorganize();
  const start = useStartReorganize();
  const stop = useStopReorganize();
  const [plan, setPlan] = useState<ReorganizePlan | null>(null);

  const job = status.data;
  const runningThis = job?.phase === "running" && jobMatches(job, scope);
  const otherRunning = job?.phase === "running" && !jobMatches(job, scope);
  const terminalThis =
    jobMatches(job, scope) &&
    (job?.phase === "done" || job?.phase === "stopped" || job?.phase === "failed");

  if (runningThis && job) {
    return (
      <div className="flex flex-wrap items-center gap-3" role="status">
        <Loader2 className="text-muted-foreground size-4 shrink-0 animate-spin" aria-hidden="true" />
        <span className="text-muted-foreground text-sm">
          Reorganizing… {job.processed} / {job.total} · moved {job.moved} · skipped {job.skipped} · failed {job.failed}
        </span>
        <Button variant="outline" size="sm" onClick={() => stop.mutate()} disabled={stop.isPending}>
          Stop
        </Button>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-3">
      {plan == null ? (
        <div className="flex flex-wrap items-center gap-3">
          <Button
            variant="outline"
            size="sm"
            disabled={preview.isPending || otherRunning}
            onClick={() => preview.mutate(scope, { onSuccess: setPlan })}
          >
            {preview.isPending ? "Building preview…" : "Preview reorganize"}
          </Button>
          {otherRunning && (
            <span className="text-muted-foreground text-sm">another library job is running</span>
          )}
          {terminalThis && job && (job.phase === "done" || job.phase === "stopped") && (
            <span className="text-muted-foreground text-sm" role="status">
              {job.phase === "done" ? "Done" : "Stopped"} — moved {job.moved} · skipped {job.skipped} · failed {job.failed}
            </span>
          )}
          {terminalThis && job?.phase === "failed" && (
            <span className="text-destructive text-sm" role="alert">
              Reorganize failed{job.error ? `: ${job.error}` : "."}
            </span>
          )}
          {preview.isError && (
            <span className="text-destructive text-sm" role="alert">
              {(preview.error as Error).message}
            </span>
          )}
        </div>
      ) : (
        <>
          <PlanView plan={plan} />
          <div className="flex flex-wrap items-center gap-3">
            <Button
              size="sm"
              disabled={start.isPending || plan.will_move === 0 || otherRunning}
              onClick={() =>
                start.mutate(scope, { onSuccess: () => setPlan(null) })
              }
            >
              {start.isPending ? "Starting…" : `Reorganize ${plan.will_move} item${plan.will_move === 1 ? "" : "s"}`}
            </Button>
            <Button variant="ghost" size="sm" onClick={() => setPlan(null)} disabled={start.isPending}>
              Cancel
            </Button>
            {start.isError && (
              <span className="text-destructive text-sm" role="alert">
                {(start.error as Error).message}
              </span>
            )}
          </div>
        </>
      )}
    </div>
  );
}
