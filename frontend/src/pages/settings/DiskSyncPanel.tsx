// frontend/src/pages/settings/DiskSyncPanel.tsx
import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import { invalidateLibraryContent } from "@/api/useEventStream";
import {
  type DiskSyncEmptiedAlbum,
  type DiskSyncPlan,
  type DiskSyncReadError,
  usePreviewDiskSync,
  useDiskSyncStatus,
  useStartDiskSync,
  useStopDiskSync,
} from "@/api/useDiskSync";
import { SettingsSection } from "@/components/system/SettingsSection";
import { Button } from "@/components/ui/button";

/** One clause of the plan headline; zero-count clauses are dropped so the
 * sentence only ever states what actually changed. */
function planHeadline(plan: DiskSyncPlan): string {
  const clauses: string[] = [];
  if (plan.will_remove > 0) {
    clauses.push(
      `${plan.will_remove} track${plan.will_remove === 1 ? "" : "s"} whose files are missing will be removed`,
    );
  }
  if (plan.emptied_total > 0) {
    clauses.push(
      `${plan.emptied_total} album${plan.emptied_total === 1 ? "" : "s"} become${plan.emptied_total === 1 ? "s" : ""} empty`,
    );
  }
  if (plan.will_update > 0) {
    clauses.push(
      `${plan.will_update} track${plan.will_update === 1 ? "" : "s"} ${plan.will_update === 1 ? "has" : "have"} changed tags`,
    );
  }
  return clauses.join(" · ");
}

/** The detail line under an emptied album's label. Two album rows can carry the
 * SAME label (a real album plus a phantom row holding a stray duplicate), so the
 * row's own track count and folder are what tell them apart. */
function emptiedDetail(album: DiskSyncEmptiedAlbum): string {
  const n = album.track_count;
  return `${n} track${n === 1 ? "" : "s"} · ${album.path}`;
}

/** The per-item read failures a terminal job carries (label + reason) — mirrors
 * ReorganizeControl's FailureList so the user sees WHICH file could not be read
 * and WHY. */
function FailureList({ failures }: Readonly<{ failures: DiskSyncReadError[] }>) {
  return (
    <ul
      role="alert"
      aria-label="Files that could not be read"
      className="text-destructive flex max-w-prose flex-col gap-1 text-sm"
    >
      {failures.map((f, i) => (
        <li key={`${f.label}-${i}`}>
          <span className="font-medium">{f.label}</span>
          <span className="text-muted-foreground">: {f.error}</span>
        </li>
      ))}
    </ul>
  );
}

function PlanView({ plan }: Readonly<{ plan: DiskSyncPlan }>) {
  return (
    <div className="flex flex-col gap-2" aria-label="Disk sync preview">
      <p className="text-sm font-medium">{planHeadline(plan)}</p>

      {plan.removals.length > 0 && (
        <ul className="max-h-72 overflow-auto rounded-md border px-3 text-sm">
          {plan.removals.map((r, i) => (
            <li
              key={`${r.label}-${i}`}
              className="flex flex-col gap-0.5 border-b py-1.5 last:border-b-0"
            >
              <span className="font-medium">{r.label}</span>
              <span className="text-muted-foreground truncate text-xs">
                {r.path}
              </span>
            </li>
          ))}
          {plan.truncated && plan.will_remove > plan.removals.length && (
            <li className="text-muted-foreground py-1.5 text-xs">
              + {plan.will_remove - plan.removals.length} more…
            </li>
          )}
        </ul>
      )}

      {plan.changes.length > 0 && (
        <div className="flex flex-col gap-1">
          <p className="text-muted-foreground text-xs">Tags to re-read:</p>
          <ul className="max-h-72 overflow-auto rounded-md border px-3 text-sm">
            {plan.changes.map((c, i) => (
              <li
                key={`${c.label}-${i}`}
                className="flex flex-col gap-0.5 border-b py-1.5 last:border-b-0"
              >
                <span className="font-medium">{c.label}</span>
                <span className="text-muted-foreground text-xs">
                  {c.fields.join(", ")}
                </span>
              </li>
            ))}
            {plan.truncated && plan.will_update > plan.changes.length && (
              <li className="text-muted-foreground py-1.5 text-xs">
                + {plan.will_update - plan.changes.length} more…
              </li>
            )}
          </ul>
        </div>
      )}

      {plan.emptied_albums.length > 0 && (
        <div className="flex flex-col gap-1">
          <p className="text-muted-foreground text-xs">
            Albums that become empty:
          </p>
          <ul
            aria-label="Albums that become empty"
            className="max-h-40 overflow-auto rounded-md border px-3 text-sm"
          >
            {plan.emptied_albums.map((a, i) => (
              <li
                key={`${a.path}-${i}`}
                className="flex flex-col gap-0.5 border-b py-1.5 last:border-b-0"
              >
                <span className="font-medium">{a.label}</span>
                <span className="text-muted-foreground truncate text-xs">
                  {emptiedDetail(a)}
                </span>
              </li>
            ))}
            {plan.truncated &&
              plan.emptied_total > plan.emptied_albums.length && (
                <li className="text-muted-foreground py-1.5 text-xs">
                  + {plan.emptied_total - plan.emptied_albums.length} more…
                </li>
              )}
          </ul>
        </div>
      )}

      {plan.read_errors.length > 0 && (
        <div className="flex flex-col gap-1">
          <p className="text-muted-foreground text-xs">Files that could not be read:</p>
          <FailureList failures={plan.read_errors} />
        </div>
      )}
    </div>
  );
}

/** Action-local feedback: an empty preview is informational (`role="status"`),
 * a failed action is an error (`role="alert"`) — the same inline-span idiom
 * ReorganizeControl uses so the result lives next to the button. */
type ActionMessage = { kind: "info" | "error"; text: string };

function DiskSyncControl() {
  const queryClient = useQueryClient();
  const status = useDiskSyncStatus();
  const preview = usePreviewDiskSync();
  const start = useStartDiskSync();
  const stop = useStopDiskSync();
  const [plan, setPlan] = useState<DiskSyncPlan | null>(null);
  const [message, setMessage] = useState<ActionMessage | null>(null);

  // The disclosure focus idiom (ReorganizeControl): opening the plan moves
  // focus into it; closing returns focus to the trigger, so keyboard focus
  // never drops to <body>.
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
  const running = phase === "running";
  const terminal =
    phase === "done" || phase === "stopped" || phase === "failed";
  const failures = terminal ? (job?.failures ?? []) : [];

  // The sync removed DB entries + refreshed tags — refresh the rosters that
  // show them once the job reaches a terminal state (same posture as
  // ReorganizeControl).
  useEffect(() => {
    if (phase === "done" || phase === "stopped" || phase === "failed") {
      invalidateLibraryContent(queryClient);
    }
  }, [phase, queryClient]);

  // A preview with changes opens the inline review; an all-clear preview shows
  // an inline info note instead (no plan, no confirm).
  function onPreviewed(result: DiskSyncPlan) {
    const nothingToDo =
      result.will_remove + result.will_update + result.emptied_total === 0 &&
      result.read_errors.length === 0;
    if (nothingToDo) {
      setMessage({ kind: "info", text: "Everything already matches the disk." });
    } else {
      setPlan(result);
    }
  }

  function onActionError(e: unknown) {
    setMessage({ kind: "error", text: (e as Error).message });
  }

  // Triggers are never `disabled` while focused (stranding keyboard focus on
  // <body>) — busy/open states use aria-disabled and swallow the click.
  const previewBlocked = preview.isPending || running || plan != null;
  const onTriggerPreview = () => {
    if (previewBlocked) return;
    setMessage(null);
    preview.mutate(undefined, { onSuccess: onPreviewed, onError: onActionError });
  };
  const syncCount = plan ? plan.will_remove + plan.will_update : 0;
  const onConfirmStart = () => {
    if (start.isPending || running) return;
    setMessage(null);
    start.mutate(undefined, {
      onSuccess: () => setPlan(null),
      onError: onActionError,
    });
  };
  const onStop = () => {
    if (stop.isPending) return;
    stop.mutate();
  };

  return (
    <div className="flex flex-col items-start gap-2">
      {plan != null && (
        <div ref={planRef} tabIndex={-1} className="outline-none">
          <PlanView plan={plan} />
        </div>
      )}
      <div className="flex flex-wrap items-center gap-3">
        {running ? (
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
            {preview.isPending ? "Building preview…" : "Preview sync…"}
          </Button>
        ) : (
          <>
            {syncCount > 0 && (
              <Button
                size="sm"
                aria-disabled={start.isPending || running || undefined}
                className="aria-disabled:opacity-50"
                onClick={onConfirmStart}
              >
                {start.isPending
                  ? "Starting…"
                  : `Sync ${syncCount} item${syncCount === 1 ? "" : "s"}`}
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
      {terminal && (
        <div className="flex flex-col gap-1">
          {phase === "failed" && job?.error != null && (
            <span role="alert" className="text-destructive text-sm">
              Sync failed: {job.error}
            </span>
          )}
          <p className="text-muted-foreground text-sm">
            {phase === "stopped" &&
              `Stopped early: ${job?.processed ?? 0} of ${job?.total ?? 0} processed · `}
            {job?.removed ?? 0} removed · {job?.updated ?? 0} updated ·{" "}
            {job?.unchanged ?? 0} unchanged · {job?.emptied_albums ?? 0} albums
            pruned
          </p>
        </div>
      )}
      {failures.length > 0 && <FailureList failures={failures} />}
    </div>
  );
}

export function DiskSyncPanel() {
  return (
    <SettingsSection
      title="Sync library with disk"
      description="Make the database match your files; removes entries whose files were deleted outside MusicDrop and re-reads tags changed by other tools. Never touches the files themselves; use Reorganize to rename/move files."
    >
      <DiskSyncControl />
    </SettingsSection>
  );
}
