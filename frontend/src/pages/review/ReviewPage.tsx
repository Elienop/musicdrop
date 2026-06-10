import { useState } from "react";
import { Link, useNavigate } from "react-router";

import {
  useAcquisitionStatus,
  type AcquisitionQueueStatus,
} from "@/api/useAcquisitionStatus";
import { useActiveImport } from "@/api/useActiveImport";
import {
  RECOMMENDATION_LABEL,
  useImportJob,
  type ImportAlbumSummary,
} from "@/api/useImport";
import {
  useImportInboxItem,
  useInboxItems,
  type InboxItem,
} from "@/api/useInbox";
import { useReviewInbox } from "@/api/useSlskd";
import type { AlbumOrigin } from "@/components/albums/album-grid";
import { Spinner, Warning } from "@/components/icons";
import { AlbumRow } from "@/components/system/AlbumRow";
import { PageBody, PageHeader } from "@/components/system/PageHeader";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";

/** Router state threaded into the decision screens (spec §1 origin
 * threading): their back link AND post-submit navigate() return here, not to
 * the import feed. */
const REVIEW_ORIGIN: { from: AlbumOrigin } = {
  from: { label: "Review", to: "/review" },
};

/**
 * The Review page — the source-agnostic pipeline for acquisition decisions.
 *
 * Sections, top to bottom: (1) "Needs your decision" — the album(s) a running
 * import is parked on, routing into the existing candidate/duplicate screens;
 * (2) "Importing now" — the live acquisition-queue snapshot (current folder +
 * queued count), only while non-idle; (3) "Waiting in the inbox" — the
 * per-item set-aside backlog; (4) "Recently landed" — the durable tally. The
 * library duplicate finder is a PLAIN link (no eager full-library scan).
 */
export function ReviewPage() {
  const navigate = useNavigate();
  const activeQuery = useActiveImport();
  const active = activeQuery.data;
  // Only fetch the job when an import is actually running (the hook is disabled
  // on an undefined id), so the page is a cheap aggregator when idle.
  const job = useImportJob(active?.active ? (active.job_id ?? undefined) : undefined);
  const inboxQuery = useInboxItems();
  const { data: status } = useAcquisitionStatus();

  const decisions = (job.data?.albums ?? []).filter(
    (a) => a.status === "needs_review" || a.status === "needs_dup_resolution",
  );
  const items = inboxQuery.data?.items ?? [];
  const importActive = active?.active ?? false;
  // Only declare "nothing to review" once the probes have resolved, so the empty
  // state never flashes on first paint before the lists load.
  const settled = !activeQuery.isLoading && !inboxQuery.isLoading;
  const nothingPending = settled && decisions.length === 0 && items.length === 0;
  const pendingCount = decisions.length + items.length;

  return (
    <PageBody variant="narrow">
      <PageHeader
        title="Review"
        meta={settled ? `${pendingCount} awaiting a decision` : undefined}
      />
      <p className="text-muted-foreground text-sm">
        Downloads and imports that need your decision — from every source, in
        one place.
      </p>

      {decisions.length > 0 && active?.job_id && (
        <DecisionSection albums={decisions} jobId={active.job_id} />
      )}

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
}: {
  albums: ImportAlbumSummary[];
  jobId: string;
}) {
  return (
    <section aria-label="Needs your decision" className="flex flex-col gap-3">
      <h2 className="text-base font-semibold">Needs your decision</h2>
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
function ImportingNowSection({ status }: { status: AcquisitionQueueStatus }) {
  return (
    <section aria-label="Importing now" className="flex flex-col gap-2">
      <h2 className="text-base font-semibold">Importing now</h2>
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
}: {
  items: InboxItem[];
  importActive: boolean;
  onStarted: (jobId: string) => void;
}) {
  const reviewOne = useImportInboxItem();
  const reviewAll = useReviewInbox();
  const [noneLeft, setNoneLeft] = useState(false);
  const busy = importActive || reviewOne.isPending || reviewAll.isPending;

  if (items.length === 0) return null;

  // A started import navigates away; a no-op start (the inbox emptied since the
  // last poll) shows a notice — the list also refetches (the hooks invalidate
  // it), so the stale rows clear on their own.
  const mutateOpts = {
    onSuccess: (res: { started?: boolean; job_id?: string | null }) => {
      if (res.started && res.job_id) onStarted(res.job_id);
      else setNoneLeft(true);
    },
  };
  const start = (run: () => void) => {
    setNoneLeft(false);
    run();
  };

  return (
    <section aria-label="Waiting in the inbox" className="flex flex-col gap-3">
      <div className="flex items-center justify-between gap-2">
        <h2 className="text-base font-semibold">Waiting in the inbox</h2>
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
          const outcomeTag =
            item.outcome === "set_aside"
              ? " · set aside"
              : item.outcome === "failed"
                ? " · import failed"
                : "";
          return (
            <li key={item.name}>
              <AlbumRow
                cover={null}
                title={item.name}
                subtitle={`${item.source}${outcomeTag}`}
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
                    {starting ? "Starting…" : "Review"}
                  </Button>
                }
              />
            </li>
          );
        })}
      </ul>
      {importActive && (
        <p className="text-muted-foreground text-xs">
          An import is already running — wait for it to finish before reviewing
          another.
        </p>
      )}
      {/* Always-mounted polite region so the no-op result is announced reliably
          (a region created together with its text reads inconsistently). */}
      <span
        role="status"
        aria-live="polite"
        className={noneLeft ? "text-muted-foreground text-sm" : "sr-only"}
      >
        {noneLeft ? "Nothing left to import — the inbox just cleared." : ""}
      </span>
      {(reviewOne.isError || reviewAll.isError) && (
        <p className="text-destructive text-sm" role="alert">
          Couldn’t start — it may have just been imported, or another import is
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
}: {
  imported: number;
  setAside: number;
  failed: number;
  processed: number;
  error: string | null;
}) {
  return (
    <section
      aria-label="Recently landed"
      className="flex flex-col gap-2 border-t pt-4"
    >
      <h2 className="text-base font-semibold">Recently landed</h2>
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

/** Last path segment of a folder, for a row with no parsed album title and the
 * importing-now line. */
function lastSegment(folder: string): string {
  const parts = folder.split("/").filter(Boolean);
  return parts.length > 0 ? parts[parts.length - 1] : folder;
}
