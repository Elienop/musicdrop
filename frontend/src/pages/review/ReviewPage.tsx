import { useState } from "react";
import { Link, useNavigate } from "react-router";

import {
  useAcquisitionStatus,
  type AcquisitionQueueStatus,
} from "@/api/useAcquisitionStatus";
import { useActiveImport } from "@/api/useActiveImport";
import { useBankList } from "@/api/useBank";
import {
  RECOMMENDATION_LABEL,
  useImportJob,
  usePauseSweep,
  type FinishedSweep,
  type ImportAlbumSummary,
  type SweepStatus,
} from "@/api/useImport";
import {
  useImportInboxItem,
  useInboxItems,
  type InboxItem,
} from "@/api/useInbox";
import { useReviewInbox } from "@/api/useSlskd";
import type { AlbumOrigin } from "@/components/albums/album-grid";
import { Close, Pause, Spinner, Success, Warning } from "@/components/icons";
import { AlbumRow } from "@/components/system/AlbumRow";
import { PageBody, PageHeader } from "@/components/system/PageHeader";
import { SectionLabel } from "@/components/system/SectionLabel";
import { StatusBanner } from "@/components/system/StatusBanner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";

import { BankSection } from "./BankSection";
import { lastSegment } from "./lastSegment";

/** Router state threaded into the decision screens (spec §1 origin
 * threading): their back link AND post-submit navigate() return here, not to
 * the import feed. */
const REVIEW_ORIGIN: { from: AlbumOrigin } = {
  from: { label: "Review", to: "/review" },
};

/**
 * The Review page — the source-agnostic pipeline for acquisition decisions.
 *
 * Sections, top to bottom: a sweep banner (counters + Pause) while a sweep
 * owns the import slot; (1) "Needs your decision" — the album(s) a running
 * import is parked on, routing into the existing candidate/duplicate screens;
 * (2) "Waiting for review" — the durable bank backlog of swept decisions;
 * (3) "Importing now" — the live acquisition-queue snapshot (current folder +
 * queued count), only while non-idle; (4) "Waiting in the inbox" — the
 * per-item set-aside backlog; (5) "Recently landed" — the durable tally. The
 * library duplicate finder is a PLAIN link (no eager full-library scan).
 */
export function ReviewPage() {
  const navigate = useNavigate();
  const activeQuery = useActiveImport();
  const active = activeQuery.data;
  // Only fetch the job when an attended import is actually running (the hook is
  // disabled on an undefined id), so the page is a cheap aggregator when idle.
  // Sweep- AND bank_apply-origin jobs are excluded: both run unattended and bank
  // decisions instead of parking them, so their `albums` feed never holds a live
  // decision — the 1s job poll would only ever compute decisions=[], and a
  // background bank apply would otherwise flash a dead-end "Needs your decision"
  // ghost. They ride the active probe's 5s cadence instead.
  const job = useImportJob(
    active?.active && active.origin !== "sweep" && active.origin !== "bank_apply"
      ? (active.job_id ?? undefined)
      : undefined,
  );
  const inboxQuery = useInboxItems();
  const { data: status } = useAcquisitionStatus();
  // Bank backlog count — a limit-1 probe so the header meta + empty state
  // reflect banked decisions whatever the section's filter shows.
  const bankPending = useBankList({ status: "needs_review", offset: 0, limit: 1 });
  const bankPendingTotal = bankPending.data?.total ?? 0;

  const decisions = (job.data?.albums ?? []).filter(
    (a) => a.status === "needs_review" || a.status === "needs_dup_resolution",
  );
  const items = inboxQuery.data?.items ?? [];
  const importActive = active?.active ?? false;
  // Only declare "nothing to review" once the probes have resolved, so the empty
  // state never flashes on first paint before the lists load.
  const settled =
    !activeQuery.isLoading && !inboxQuery.isLoading && !bankPending.isLoading;
  // A probe that ERRORED reports empty data (items=[], total=0), so counting it
  // as settled would let a transient failure masquerade as a resolved backlog —
  // the exact state most likely to make the user think their sweep found nothing.
  // Exclude errored probes from both the empty-state verdict and the header count
  // (BankSection surfaces the bank failure itself, with a retry).
  const probesErrored =
    activeQuery.isError || inboxQuery.isError || bankPending.isError;
  const nothingPending =
    settled &&
    !probesErrored &&
    decisions.length === 0 &&
    items.length === 0 &&
    bankPendingTotal === 0;
  const pendingCount = decisions.length + items.length + bankPendingTotal;

  return (
    <PageBody>
      <PageHeader
        title="Review"
        meta={
          settled && !probesErrored
            ? `${pendingCount} awaiting a decision`
            : undefined
        }
      />
      <p className="text-muted-foreground text-sm">
        Downloads and imports that need your decision, from every source, in
        one place.
      </p>

      {/* Sweep banner — counters ride the active probe's 5s cadence. */}
      {active?.active && active.origin === "sweep" && active.sweep && active.job_id && (
        <SweepBanner jobId={active.job_id} sweep={active.sweep} />
      )}

      {/* Post-sweep recap — the live banner's terminal counterpart. Idle
          probe only: while a sweep runs, active=true and the banner above
          owns the slot. */}
      {active && !active.active && active.last_sweep && (
        <SweepRecap recap={active.last_sweep} />
      )}

      {decisions.length > 0 && active?.job_id && (
        <DecisionSection albums={decisions} jobId={active.job_id} />
      )}

      <BankSection />

      {status && status.phase !== "idle" && (
        <ImportingNowSection status={status} />
      )}

      <InboxSection
        items={items}
        importActive={importActive}
        onStarted={(jobId) => navigate(`/import?job=${jobId}`)}
      />

      {nothingPending && (
        <p className="text-muted-foreground text-sm" role="status">
          Nothing to review. Completed downloads that need a decision show up
          here.
        </p>
      )}

      {status && (
        <RecentSection
          imported={Math.max(status.processed - status.set_aside - status.failed, 0)}
          setAside={status.set_aside}
          failed={status.failed}
          processed={status.processed}
          error={status.error}
        />
      )}

      {/* A plain pointer to the (separate, expensive) library duplicate
          finder — static copy, NO eager scan for a count (spec §1). */}
      <p className="text-muted-foreground text-sm">
        <Link
          to="/duplicates"
          className="text-foreground focus-ring rounded-sm underline"
        >
          Find duplicate albums in your library
        </Link>
      </p>
    </PageBody>
  );
}

/** "Needs your decision" — the album a running import is parked on. Serial
 * import parks one at a time, but render whatever is pending. Routes to the
 * existing candidate-review or duplicate-resolve screen by status, threading
 * the Review origin so both screens return here. */
function DecisionSection({
  albums,
  jobId,
}: Readonly<{
  albums: ImportAlbumSummary[];
  jobId: string;
}> ) {
  return (
    <section aria-label="Needs your decision" className="flex flex-col gap-3">
      <SectionLabel>Needs your decision</SectionLabel>
      <ul className="border-border divide-border divide-y overflow-hidden rounded-xl border">
        {albums.map((album) => {
          const needsDup = album.status === "needs_dup_resolution";
          const title =
            (album.album ?? lastSegment(album.folder)) || "Unknown album";
          const to = needsDup
            ? `/import/albums/${album.index}/duplicate?job=${jobId}`
            : `/import/albums/${album.index}?job=${jobId}`;
          return (
            <li key={album.index} className="bg-primary/5">
              <AlbumRow
                cover={null}
                title={title}
                subtitle={album.artist ?? "Unknown artist"}
                meta={
                  needsDup
                    ? undefined
                    : `${Math.round(album.confidence)}% · ${RECOMMENDATION_LABEL[album.recommendation]}`
                }
                badge={
                  <Badge variant="default">
                    {needsDup ? "Already in library" : "Needs review"}
                  </Badge>
                }
                action={
                  <Button size="sm" asChild>
                    <Link to={to} state={REVIEW_ORIGIN}>
                      {needsDup ? "Resolve" : "Review"}
                    </Link>
                  </Button>
                }
              />
            </li>
          );
        })}
      </ul>
    </section>
  );
}

/** "Importing now" — the so-far-unrendered live fields of the acquisition
 * status probe: what the unattended drain is importing and how many drops
 * wait behind it. Rendered only while the queue is non-idle. */
function ImportingNowSection({ status }: Readonly<{ status: AcquisitionQueueStatus }>) {
  return (
    <section aria-label="Importing now" className="flex flex-col gap-2">
      <SectionLabel>Importing now</SectionLabel>
      <p
        className="text-muted-foreground flex items-center gap-2 text-sm"
        role="status"
      >
        <Spinner className="size-4 shrink-0 animate-spin" aria-hidden="true" />
        <span>
          {status.current !== null
            ? `Importing ${lastSegment(status.current)}`
            : "Waiting for the import slot"}
          {` · ${status.queued} queued`}
        </span>
      </p>
    </section>
  );
}

/** "Waiting in the inbox" — the per-item set-aside backlog. Each row imports its
 * own folder; "Review all" imports the whole inbox. Both are disabled while an
 * import runs (the single slot is busy) — the visible helper line below carries
 * the reason (no disabled-button `title`, per the spec §4 rule). */
function InboxSection({
  items,
  importActive,
  onStarted,
}: Readonly<{
  items: InboxItem[];
  importActive: boolean;
  onStarted: (jobId: string) => void;
}> ) {
  const reviewOne = useImportInboxItem();
  const reviewAll = useReviewInbox();
  // null = no no-op yet. Otherwise the number of folders the backend SKIPPED as
  // still-arriving: 0 means the inbox really did clear, >0 means "not yet".
  const [noOpInFlight, setNoOpInFlight] = useState<number | null>(null);
  const busy = importActive || reviewOne.isPending || reviewAll.isPending;

  if (items.length === 0) return null;

  // A started import navigates away. A no-op has TWO causes and they must not
  // read the same: the inbox emptied since the last poll (nothing left), or every
  // folder is still receiving files (`in_flight` > 0) — in which case the rows
  // the user is looking at are still there and telling them it cleared is a lie.
  const mutateOpts = {
    onSuccess: (res: { started?: boolean; job_id?: string | null; in_flight?: number }) => {
      if (res.started && res.job_id) onStarted(res.job_id);
      else setNoOpInFlight(res.in_flight ?? 0);
    },
  };
  const start = (run: () => void) => {
    setNoOpInFlight(null);
    run();
  };
  const noOpMessage =
    noOpInFlight === null
      ? ""
      : noOpInFlight > 0
        ? `Still downloading — ${noOpInFlight} ${noOpInFlight === 1 ? "folder is" : "folders are"} ` +
          "still receiving files. They'll be importable once they finish."
        : "Nothing left to import; the inbox just cleared.";

  return (
    <section aria-label="Waiting in the inbox" className="flex flex-col gap-3">
      <div className="flex items-center justify-between gap-2">
        <SectionLabel>Waiting in the inbox</SectionLabel>
        <Button
          type="button"
          variant="outline"
          size="sm"
          disabled={busy}
          onClick={() => start(() => reviewAll.mutate(undefined, mutateOpts))}
        >
          {reviewAll.isPending ? "Starting…" : "Review all"}
        </Button>
      </div>
      <ul className="border-border divide-border divide-y overflow-hidden rounded-xl border">
        {items.map((item) => {
          const starting =
            reviewOne.isPending && reviewOne.variables === item.name;
          // The inbox doesn't track where a folder came from (it just lists a
          // directory), so the only honest subtitle is its outcome — set-aside
          // or failed — and nothing for a fresh drop.
          // A folder still receiving files says so FIRST: "Review all" skips it,
          // and the per-row Review below is an explicit override — importing a
          // half-arrived album files a partial copy, so the choice must be
          // informed rather than blind.
          const subtitle = item.in_flight
            ? "Still downloading — importing now may catch only part of it"
            : item.outcome === "set_aside"
              ? "Set aside"
              : item.outcome === "failed"
                ? "Import failed"
                : undefined;
          return (
            <li key={item.name}>
              <AlbumRow
                cover={null}
                title={item.name}
                subtitle={subtitle}
                meta={`${item.track_count} ${item.track_count === 1 ? "track" : "tracks"}`}
                action={
                  <Button
                    type="button"
                    size="sm"
                    disabled={busy}
                    onClick={() =>
                      start(() => reviewOne.mutate(item.name, mutateOpts))
                    }
                  >
                    {starting ? "Starting…" : item.in_flight ? "Review anyway" : "Review"}
                  </Button>
                }
              />
            </li>
          );
        })}
      </ul>
      {importActive && (
        <p className="text-muted-foreground text-xs">
          An import is already running; wait for it to finish before reviewing
          another.
        </p>
      )}
      {/* Always-mounted polite region so the no-op result is announced reliably
          (a region created together with its text reads inconsistently). */}
      <span
        role="status"
        aria-live="polite"
        className={noOpMessage ? "text-muted-foreground text-sm" : "sr-only"}
      >
        {noOpMessage}
      </span>
      {(reviewOne.isError || reviewAll.isError) && (
        <p className="text-destructive text-sm" role="alert">
          Couldn’t start; it may have just been imported, or another import is
          running. Try again in a moment.
        </p>
      )}
    </section>
  );
}

/** "Recently landed" — the durable lifetime tally + last drain error. */
function RecentSection({
  imported,
  setAside,
  failed,
  processed,
  error,
}: Readonly<{
  imported: number;
  setAside: number;
  failed: number;
  processed: number;
  error: string | null;
}> ) {
  return (
    <section
      aria-label="Recently landed"
      className="flex flex-col gap-2 border-t pt-4"
    >
      <SectionLabel>Recently landed</SectionLabel>
      {processed === 0 && !error ? (
        <p className="text-muted-foreground text-sm">
          No completed downloads have been imported yet.
        </p>
      ) : (
        <p className="text-muted-foreground text-sm">
          {imported} imported · {setAside} set aside · {failed} failed
        </p>
      )}
      {error && (
        <p className="text-destructive flex items-start gap-2 text-sm">
          <Warning className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
          <span>Last error: {error}</span>
        </p>
      )}
    </section>
  );
}

/** Top-level sweep notice (the post-redesign banner dialect): live counters,
 * the current folder, Pause and a link into the run. Decisions below are
 * store writes and stay fully usable while the sweep owns the import slot. */
function SweepBanner({ jobId, sweep }: Readonly<{ jobId: string; sweep: SweepStatus }>) {
  const pause = usePauseSweep(jobId);
  return (
    <StatusBanner
      tone="neutral"
      action={
        <div className="flex shrink-0 items-center gap-2">
          <Button
            type="button"
            variant="outline"
            size="sm"
            aria-disabled={pause.isPending || sweep.paused}
            onClick={() => {
              // In flight or already requested → swallow the re-click instead
              // of disabling (a mid-flight disable strands keyboard focus on
              // <body> — the Pagination posture). Pause is an idempotent 204
              // server-side, so a slipped repeat is harmless anyway.
              if (pause.isPending || sweep.paused) return;
              pause.mutate();
            }}
          >
            <Pause aria-hidden="true" />
            {pause.isPending || sweep.paused ? "Pausing…" : "Pause"}
          </Button>
          <Button variant="ghost" size="sm" asChild>
            <Link to={`/import?job=${jobId}`}>View</Link>
          </Button>
        </div>
      }
    >
      <p className="flex items-center gap-3 font-medium">
        <Spinner className="text-muted-foreground size-5 shrink-0 animate-spin" aria-hidden="true" />
        <span className="min-w-0">
          Sweeping: {sweep.processed} processed · {sweep.auto_applied} imported ·{" "}
          {sweep.banked} banked.
          {sweep.current_folder && !sweep.paused && (
            <span className="text-muted-foreground font-normal">
              {" "}Now: {lastSegment(sweep.current_folder)}
            </span>
          )}
          {sweep.paused && (
            <span className="text-muted-foreground font-normal">
              {" "}Finishing the current album…
            </span>
          )}
        </span>
      </p>
    </StatusBanner>
  );
}

/** localStorage key remembering the dismissed recap's job id (the
 * `musicdrop.pageSize` naming convention). A NEW sweep has a new job id, so
 * dismissing one recap never hides the next. */
const RECAP_DISMISSED_KEY = "musicdrop.sweepRecapDismissed";

/** Post-sweep recap — renders from the probe's `last_sweep`, so it survives
 * reloads and other tabs until a new import replaces the registry slot (the
 * "View run" target expires in the same moment, so the link never dangles).
 * Dismiss is per-browser. A PAUSED sweep ends phase=done with the flag set —
 * it reads "Sweep paused" plus the resume hint. */
function SweepRecap({ recap }: Readonly<{ recap: FinishedSweep }>) {
  const [dismissedId, setDismissedId] = useState<string | null>(() =>
    localStorage.getItem(RECAP_DISMISSED_KEY),
  );
  if (dismissedId === recap.job_id) return null;
  return (
    <StatusBanner
      tone="neutral"
      icon={Success}
      action={
        <div className="flex shrink-0 items-center gap-2">
          <Button variant="ghost" size="sm" asChild>
            <Link to={`/import?job=${recap.job_id}`}>View run</Link>
          </Button>
          <Button
            variant="ghost"
            size="icon"
            aria-label="Dismiss sweep recap"
            onClick={() => {
              localStorage.setItem(RECAP_DISMISSED_KEY, recap.job_id);
              setDismissedId(recap.job_id);
            }}
          >
            <Close aria-hidden="true" />
          </Button>
        </div>
      }
    >
      <p className="min-w-0 font-medium">
        {recap.paused ? "Sweep paused" : "Sweep finished"}: {recap.processed}{" "}
        processed · {recap.auto_applied} imported · {recap.banked} banked
        {recap.skipped_known > 0 ? ` · ${recap.skipped_known} already known` : ""}.
        {recap.paused && (
          <span className="text-muted-foreground font-normal">
            {" "}
            Resume by sweeping the same folder again.
          </span>
        )}
      </p>
    </StatusBanner>
  );
}
