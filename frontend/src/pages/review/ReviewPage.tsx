import { AlertTriangle, Inbox } from "lucide-react";
import { useState } from "react";
import { Link, useNavigate } from "react-router";

import { useAcquisitionStatus } from "@/api/useAcquisitionStatus";
import { useActiveImport } from "@/api/useActiveImport";
import { useDuplicates } from "@/api/useDuplicates";
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
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";

/**
 * The Review page — the source-agnostic home for acquisition decisions.
 *
 * Three buckets, all read-only aggregators over existing state plus the two
 * inbox actions: (1) the live decision a running import is parked on, (2) the
 * per-item inbox backlog, (3) the recent tally. The detail screens
 * (candidate-review / duplicate-resolve) are reused as-is — this page just
 * routes into them. A count + link surfaces the (separate, expensive) library
 * duplicate finder without absorbing it.
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
  // The library finder is a full library scan (no cheap count) — fetched lazily
  // and cached; the link renders immediately and the count fills in when ready.
  const dupes = useDuplicates("strict");

  const decisions = (job.data?.albums ?? []).filter(
    (a) => a.status === "needs_review" || a.status === "needs_dup_resolution",
  );
  const items = inboxQuery.data?.items ?? [];
  const importActive = active?.active ?? false;
  // Only declare "nothing to review" once the probes have resolved, so the empty
  // state never flashes on first paint before the lists load.
  const settled = !activeQuery.isLoading && !inboxQuery.isLoading;
  const nothingPending = settled && decisions.length === 0 && items.length === 0;

  return (
    <section aria-label="Review" className="flex max-w-3xl flex-col gap-8">
      <header className="flex flex-col gap-1">
        <h2 className="text-2xl font-semibold tracking-tight">Review</h2>
        <p className="text-muted-foreground text-sm">
          Downloads and imports that need your decision — from every source, in
          one place.
        </p>
      </header>

      {decisions.length > 0 && active?.job_id && (
        <DecisionSection albums={decisions} jobId={active.job_id} />
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

      <DuplicatesLink count={dupes.data?.group_count} />
    </section>
  );
}

/** "Needs your decision" — the album a running import is parked on. Serial
 * import parks one at a time, but render whatever is pending. Routes to the
 * existing candidate-review or duplicate-resolve screen by status. */
function DecisionSection({
  albums,
  jobId,
}: {
  albums: ImportAlbumSummary[];
  jobId: string;
}) {
  return (
    <section aria-label="Needs your decision" className="flex flex-col gap-3">
      <h3 className="text-sm font-medium">Needs your decision</h3>
      <ul className="border-border divide-border divide-y rounded-xl border">
        {albums.map((album) => (
          <li key={album.index}>
            <DecisionRow album={album} jobId={jobId} />
          </li>
        ))}
      </ul>
    </section>
  );
}

function DecisionRow({
  album,
  jobId,
}: {
  album: ImportAlbumSummary;
  jobId: string;
}) {
  const needsDup = album.status === "needs_dup_resolution";
  const title = (album.album ?? lastSegment(album.folder)) || "Unknown album";
  return (
    <div className="bg-primary/5 flex min-w-0 items-center gap-3 px-4 py-3">
      <div className="flex min-w-0 flex-1 flex-col">
        <span className="truncate font-medium">{title}</span>
        <span className="text-muted-foreground truncate text-sm">
          {album.artist ?? "Unknown artist"}
          <span aria-hidden="true"> · </span>
          {needsDup
            ? "Already in your library"
            : `${Math.round(album.confidence)}% · ${RECOMMENDATION_LABEL[album.recommendation]}`}
        </span>
      </div>
      <Badge variant="default" className="shrink-0">
        {needsDup ? "Duplicate" : "Needs review"}
      </Badge>
      <Button size="sm" asChild>
        <Link
          to={
            needsDup
              ? `/import/albums/${album.index}/duplicate?job=${jobId}`
              : `/import/albums/${album.index}?job=${jobId}`
          }
        >
          {needsDup ? "Resolve" : "Review"}
        </Link>
      </Button>
    </div>
  );
}

/** "Waiting in the inbox" — the per-item set-aside backlog. Each row imports its
 * own folder; "Review all" imports the whole inbox. Both are disabled while an
 * import runs (the single slot is busy). */
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
  const disabledReason = importActive
    ? "An import is already running"
    : undefined;

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
        <h3 className="text-sm font-medium">Waiting in the inbox</h3>
        <Button
          type="button"
          variant="outline"
          size="sm"
          disabled={busy}
          title={disabledReason}
          onClick={() => start(() => reviewAll.mutate(undefined, mutateOpts))}
        >
          {reviewAll.isPending ? "Starting…" : "Review all"}
        </Button>
      </div>
      <ul className="border-border divide-border divide-y rounded-xl border">
        {items.map((item) => {
          const starting = reviewOne.isPending && reviewOne.variables === item.name;
          return (
            <li
              key={item.name}
              className="flex min-w-0 items-center gap-3 px-4 py-3"
            >
              <Inbox
                className="text-muted-foreground size-4 shrink-0"
                aria-hidden="true"
              />
              <div className="flex min-w-0 flex-1 flex-col">
                <span className="truncate font-medium">{item.name}</span>
                <span className="text-muted-foreground truncate text-sm">
                  {item.source}
                  {item.outcome === "set_aside" && " · set aside"}
                  {item.outcome === "failed" && " · import failed"}
                  <span aria-hidden="true"> · </span>
                  {item.track_count} {item.track_count === 1 ? "track" : "tracks"}
                </span>
              </div>
              <Button
                type="button"
                size="sm"
                disabled={busy}
                title={disabledReason}
                onClick={() => start(() => reviewOne.mutate(item.name, mutateOpts))}
              >
                {starting ? "Starting…" : "Review"}
              </Button>
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

/** "Recent" — the durable lifetime tally + last drain error. */
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
    <section aria-label="Recent" className="flex flex-col gap-2 border-t pt-4">
      <h3 className="text-sm font-medium">Recent</h3>
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
          <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
          <span>Last error: {error}</span>
        </p>
      )}
    </section>
  );
}

/** A link to the (separate, expensive) library duplicate finder. The count is
 * lazy — the link always shows; the cluster count fills in when the scan
 * returns (or never, on error). The whole phrase is the link, so its accessible
 * name never collides with an import-time "Resolve" decision above. */
function DuplicatesLink({ count }: { count: number | undefined }) {
  const label =
    count !== undefined && count > 0
      ? `Resolve ${count} duplicate ${count === 1 ? "cluster" : "clusters"} in your library`
      : "Find duplicate albums in your library";
  return (
    <p className="text-muted-foreground text-sm">
      <Link to="/duplicates" className="text-foreground underline">
        {label}
      </Link>
    </p>
  );
}

/** Last path segment of a folder, for a row with no parsed album title. */
function lastSegment(folder: string): string {
  const parts = folder.split("/").filter(Boolean);
  return parts.length > 0 ? parts[parts.length - 1] : folder;
}
