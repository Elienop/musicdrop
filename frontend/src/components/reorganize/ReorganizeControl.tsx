// frontend/src/components/reorganize/ReorganizeControl.tsx
import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState, type ReactNode } from "react";

import { invalidateLibraryContent } from "@/api/useEventStream";
import {
  type ReorganizeBackfillStatus,
  type ReorganizeMove,
  type ReorganizePlan,
  type ReorganizeScope,
  type ReorganizeUnitFailure,
  useDismissReorganize,
  usePreviewReorganize,
  useReorganizeStatus,
  useStartReorganize,
  useStopReorganize,
} from "@/api/useReorganize";
import { Reorganize, Spinner, Stop as StopIcon } from "@/components/icons";
import { IconAction } from "@/components/system/IconAction";
import { Button } from "@/components/ui/button";
import { formatTimestamp } from "@/lib/format";

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

/** The confirm button's label: a real move plan counts items; an orphan-only
 * plan (no moves, only husk folders to sweep) counts folders to clean up.
 * `null` when the plan has nothing to DO — an all-refusals preview, which opens
 * so the conflicts can be READ. `will_move` already excludes refused units, so
 * the count here can never promise to move one. */
function confirmLabel(plan: ReorganizePlan): string | null {
  if (plan.will_move > 0)
    return `Reorganize ${plan.will_move} item${plan.will_move === 1 ? "" : "s"}`;
  if (plan.orphans_total > 0)
    return `Clean up ${plan.orphans_total} folder${plan.orphans_total === 1 ? "" : "s"}`;
  return null;
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

/** Units the pre-flight REFUSES to move: their computed destination is already
 * taken, so moving would rename a file nobody asked to rename. Never a move row
 * — the backend keeps these out of `moves` AND out of `will_move` — so this
 * carries the same destructive treatment as FailureList rather than the neutral
 * MoveRow one. No `role="alert"` though: that is reserved for a terminal job's
 * failures, which arrive unbidden, whereas this sits inside the preview the
 * user just opened and focus has already moved into. */
function ConflictList({ plan }: { plan: ReorganizePlan }) {
  const hidden = plan.conflicts_total - plan.conflicts.length;
  return (
    <div className="flex flex-col gap-1">
      <p className="text-muted-foreground text-xs">
        Cannot be moved (the destination name is already taken):{" "}
        <span className="text-destructive font-medium">
          {plan.conflicts_total}
        </span>
      </p>
      <ul
        aria-label="Items that cannot be reorganized"
        className="text-destructive max-h-56 overflow-auto rounded-md border px-3 text-sm"
      >
        {plan.conflicts.map((c, i) => (
          <li
            key={`${c.label}-${i}`}
            className="flex flex-col gap-0.5 border-b py-1.5 last:border-b-0"
          >
            <span className="font-medium">{c.label}</span>
            <span className="text-muted-foreground truncate text-xs">
              {c.from_path}
            </span>
            {/* `detail` repeats its own path, so each sentence reads alone. */}
            <ul className="text-muted-foreground flex flex-col gap-0.5 text-xs">
              {c.collisions.map((col, j) => (
                <li key={`${col.path}-${j}`}>{col.detail}</li>
              ))}
            </ul>
          </li>
        ))}
        {hidden > 0 && (
          <li className="text-muted-foreground py-1.5 text-xs">
            + {hidden} more…
          </li>
        )}
      </ul>
    </div>
  );
}

/** The per-unit failures a terminal job carries (label + reason) — this is
 * what finally shows the user WHICH file is stuck and WHY.
 *
 * Dated and dismissable because the slot holds this result until the NEXT job
 * starts, and a clean preview starts no job: without the finish time the user
 * can't tell yesterday's failure from one they just caused, and without the
 * dismiss they'd have to run a reorganize they don't need to clear it. Only a
 * terminal job gets here — a running one keeps its live UI. */
function FailureList({
  failures,
  finishedAt,
  onDismiss,
  dismissPending,
}: {
  failures: ReorganizeUnitFailure[];
  finishedAt: string | null;
  onDismiss: () => void;
  dismissPending: boolean;
}) {
  return (
    <div className="flex max-w-prose flex-col gap-1">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        {finishedAt != null && (
          <p className="text-muted-foreground text-xs">
            From the reorganize that finished{" "}
            <time dateTime={finishedAt}>{formatTimestamp(finishedAt)}</time>:
          </p>
        )}
        {/* Never `disabled`: it holds focus at the moment it is pressed. */}
        <Button
          variant="ghost"
          size="sm"
          aria-disabled={dismissPending || undefined}
          className="aria-disabled:opacity-50"
          onClick={onDismiss}
        >
          {dismissPending ? "Dismissing…" : "Dismiss"}
        </Button>
      </div>
      <ul
        role="alert"
        aria-label="Files that could not be reorganized"
        className="text-destructive flex flex-col gap-1 text-sm"
      >
        {failures.map((f, i) => (
          <li key={`${f.label}-${i}`}>
            <span className="font-medium">{f.label}</span>
            <span className="text-muted-foreground">: {f.error}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function PlanView({ plan }: { plan: ReorganizePlan }) {
  return (
    <div className="flex flex-col gap-2" aria-label="Reorganize preview">
      {/* The three counts partition the scope:
          total == will_move + already_in_place + conflicts_total. */}
      <p className="text-sm">
        <span className="font-medium">{plan.will_move} will move</span> ·{" "}
        {plan.already_in_place} already in place
        {plan.conflicts_total > 0 && (
          <>
            {" · "}
            <span className="text-destructive font-medium">
              {plan.conflicts_total} cannot be moved
            </span>
          </>
        )}
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
      {plan.conflicts_total > 0 && <ConflictList plan={plan} />}
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
  const dismiss = useDismissReorganize();
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

  // Once THIS scope's job is terminal, surface its per-unit failures (label +
  // reason) under the controls — counts alone can't say which file is stuck.
  const isTerminal =
    phase === "done" || phase === "stopped" || phase === "failed";
  const failures = isThis && isTerminal ? (job?.failures ?? []) : [];
  // Non-null only on a terminal job, so the block can date itself.
  const finishedAt = isThis && isTerminal ? (job?.finished_at ?? null) : null;

  // Files moved + DB paths changed — refresh the album/artist rosters that show
  // those paths once THIS scope's job reaches a terminal state.
  useEffect(() => {
    if (isThis && isTerminal) {
      invalidateLibraryContent(queryClient);
    }
  }, [isThis, isTerminal, queryClient]);

  // A preview with moves, orphan husks to sweep, OR refused units opens the
  // inline review; only a truly empty preview shows the inline info note (no
  // plan, no Done). An all-refusals preview has nothing to confirm but is NOT
  // "nothing to reorganize" — the user has to see WHY those units are stuck.
  function onPreviewed(result: ReorganizePlan) {
    if (
      result.will_move > 0 ||
      result.orphans_total > 0 ||
      result.conflicts_total > 0
    ) {
      setPlan(result);
    } else {
      setMessage({
        kind: "info",
        text: "Nothing to reorganize; everything already matches your config.",
      });
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
  // Clearing the slot unmounts the button being pressed, so hand focus to the
  // control's own trigger first (the same place Cancel returns it to) —
  // otherwise the whole block vanishes and keyboard focus falls to <body>.
  // With a preview plan open the inline variant does not mount the trigger at
  // all (Confirm/Cancel stand in for it), so fall back to the plan container,
  // which is this file's other managed focus target.
  const onDismiss = () => {
    if (dismiss.isPending) return; // in flight — swallow
    setMessage(null);
    dismiss.mutate(undefined, {
      onSuccess: () => (triggerRef.current ?? planRef.current)?.focus(),
      onError: onActionError,
    });
  };

  // `null` on an all-refusals plan: nothing may be offered to run, so the plan
  // renders read-only with Cancel as its only exit.
  const confirmText = plan != null ? confirmLabel(plan) : null;

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
            {confirmText != null && (
              <Button
                size="sm"
                aria-disabled={start.isPending || otherRunning || undefined}
                className="aria-disabled:opacity-50"
                onClick={onConfirmStart}
              >
                {start.isPending ? "Starting…" : confirmText}
              </Button>
            )}
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
        {failures.length > 0 && (
          <FailureList
            failures={failures}
            finishedAt={finishedAt}
            onDismiss={onDismiss}
            dismissPending={dismiss.isPending}
          />
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
            {confirmText != null && (
              <Button
                size="sm"
                aria-disabled={start.isPending || otherRunning || undefined}
                className="aria-disabled:opacity-50"
                onClick={onConfirmStart}
              >
                {start.isPending ? "Starting…" : confirmText}
              </Button>
            )}
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
      {failures.length > 0 && (
        <FailureList
          failures={failures}
          finishedAt={finishedAt}
          onDismiss={onDismiss}
          dismissPending={dismiss.isPending}
        />
      )}
    </div>
  );
}
