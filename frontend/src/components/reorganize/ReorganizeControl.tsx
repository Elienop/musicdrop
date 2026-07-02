// frontend/src/components/reorganize/ReorganizeControl.tsx
import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState, type ReactNode } from "react";

import { invalidateLibraryContent } from "@/api/useEventStream";
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
import { Reorganize, Spinner, Stop as StopIcon } from "@/components/icons";
import { IconAction } from "@/components/system/IconAction";
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
      {plan.orphans.length > 0 && (
        <div className="flex flex-col gap-1">
          <p className="text-muted-foreground text-xs">
            Folders to clean up (move to Trash):{" "}
            <span className="font-medium">{plan.orphans_total}</span>
          </p>
          <ul className="max-h-40 overflow-auto rounded-md border px-3 text-sm">
            {plan.orphans.map((o, i) => (
              <li
                key={`${o.path}-${i}`}
                className="flex items-center justify-between gap-2 border-b py-1.5 last:border-b-0"
              >
                <span className="truncate">{o.name}</span>
                <span className="text-muted-foreground shrink-0 text-xs">
                  {o.file_count} file{o.file_count === 1 ? "" : "s"}
                </span>
              </li>
            ))}
            {plan.orphans_total > plan.orphans.length && (
              <li className="text-muted-foreground py-1.5 text-xs">
                + {plan.orphans_total - plan.orphans.length} more…
              </li>
            )}
          </ul>
        </div>
      )}
    </div>
  );
}

/** Action-local feedback: an empty-preview result is informational
 * (`role="status"`), a failed action is an error (`role="alert"`) — the
 * inline-span idiom LyricsBackfillPanel uses, so the result lives next to the
 * button that caused it instead of in app-wide chrome. */
type ActionMessage = { kind: "info" | "error"; text: string };

export function ReorganizeControl({
  scope,
  variant = "inline",
  railActions,
}: {
  scope: ReorganizeScope;
  /** "rail" renders the Koito-style large icon trigger in a centered row —
   * after the page's other IconActions, passed via `railActions` so the
   * whole row lives in one flex container — with the transient
   * preview/confirm UI expanding full-width below the row. "inline" keeps
   * the original wrap row of text buttons (settings panel). */
  variant?: "inline" | "rail";
  railActions?: ReactNode;
}) {
  const queryClient = useQueryClient();
  const status = useReorganizeStatus();
  const preview = usePreviewReorganize();
  const start = useStartReorganize();
  const stop = useStopReorganize();
  const [plan, setPlan] = useState<ReorganizePlan | null>(null);
  const [message, setMessage] = useState<ActionMessage | null>(null);

  // Spec §4 disclosure idiom (the album-edit-panel pattern): the plan opening
  // moves focus INTO it; closing (Cancel or a started job) returns focus to
  // the trigger. Without this keyboard focus drops to <body> both ways — the
  // plan renders unfocused, and Cancel/confirm unmount the button just pressed.
  const planRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const planWasOpen = useRef(false);
  useEffect(() => {
    if (plan != null && !planWasOpen.current) {
      planRef.current?.focus();
    } else if (plan == null && planWasOpen.current) {
      triggerRef.current?.focus();
    }
    planWasOpen.current = plan != null;
  }, [plan]);

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
      invalidateLibraryContent(queryClient);
    }
  }, [isThis, phase, queryClient]);

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

  // Triggers are never `disabled` while they hold focus (the Pagination rule:
  // disabling mid-flight strands keyboard focus on <body>) — busy/open states
  // are conveyed via aria-disabled and the click is swallowed instead.
  const previewBlocked = preview.isPending || otherRunning || plan != null;
  const onTriggerPreview = () => {
    if (previewBlocked) return; // busy/open — keep focus, swallow the re-click
    setMessage(null);
    preview.mutate(scope, {
      onSuccess: onPreviewed,
      onError: onActionError,
    });
  };
  const onConfirmStart = () => {
    if (start.isPending || otherRunning) return; // in flight — swallow
    setMessage(null);
    start.mutate(scope, {
      onSuccess: () => setPlan(null),
      onError: onActionError,
    });
  };
  const onStop = () => {
    if (stop.isPending) return; // in flight — swallow
    stop.mutate();
  };

  if (variant === "rail") {
    return (
      <div className="flex flex-col items-stretch gap-2">
        {/* w-fit wrapper: the hairline above the actions stretches exactly
            to the icon row's combined width, no further. Left-aligned — the
            page centers the whole rail text+actions unit and everything
            inside shares ONE left edge. */}
        <div className="flex w-fit flex-col gap-2">
          <div aria-hidden="true" className="border-border my-2 border-t" />
          <div className="flex flex-wrap items-center justify-center gap-1">
            {railActions}
            {runningThis ? (
              <IconAction
                ref={triggerRef}
                label="Stop reorganizing"
                aria-disabled={stop.isPending || undefined}
                className="aria-disabled:opacity-50"
                onClick={onStop}
              >
                <StopIcon weight="thin" className="size-10" aria-hidden="true" />
              </IconAction>
            ) : (
              <IconAction
                ref={triggerRef}
                label="Reorganize files"
                aria-disabled={previewBlocked || undefined}
                className="aria-disabled:opacity-50"
                onClick={onTriggerPreview}
              >
                {preview.isPending ? (
                  <Spinner weight="thin" className="size-10 animate-spin" aria-hidden="true" />
                ) : (
                  <Reorganize weight="thin" className="size-10" aria-hidden="true" />
                )}
              </IconAction>
            )}
          </div>
        </div>
        {plan != null && (
          <>
            <div ref={planRef} tabIndex={-1} className="outline-none">
              <PlanView plan={plan} />
            </div>
            <Button
              size="sm"
              aria-disabled={start.isPending || otherRunning || undefined}
              className="aria-disabled:opacity-50"
              onClick={onConfirmStart}
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
                ? "text-destructive text-center text-sm"
                : "text-muted-foreground text-center text-sm"
            }
          >
            {message.text}
          </span>
        )}
      </div>
    );
  }

  return (
    <div className="flex flex-col items-start gap-2">
      {plan != null && (
        <div ref={planRef} tabIndex={-1} className="outline-none">
          <PlanView plan={plan} />
        </div>
      )}
      <div className="flex flex-wrap items-center gap-3">
        {runningThis ? (
          <Button
            ref={triggerRef}
            variant="outline"
            size="sm"
            aria-disabled={stop.isPending || undefined}
            className="aria-disabled:opacity-50"
            onClick={onStop}
          >
            Stop
          </Button>
        ) : plan == null ? (
          <Button
            ref={triggerRef}
            variant="outline"
            size="sm"
            aria-disabled={previewBlocked || undefined}
            className="aria-disabled:opacity-50"
            onClick={onTriggerPreview}
          >
            {preview.isPending ? "Building preview…" : "Reorganize files…"}
          </Button>
        ) : (
          <>
            <Button
              size="sm"
              aria-disabled={start.isPending || otherRunning || undefined}
              className="aria-disabled:opacity-50"
              onClick={onConfirmStart}
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
