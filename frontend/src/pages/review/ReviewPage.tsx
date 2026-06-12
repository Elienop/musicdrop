import { useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router";
import { toast } from "sonner";

import {
  useAcquisitionStatus,
  type AcquisitionQueueStatus,
} from "@/api/useAcquisitionStatus";
import { useActiveImport } from "@/api/useActiveImport";
import {
  BankConflictError,
  useBankList,
  useBulkIgnoreBank,
  useDeleteBankItem,
  useIgnoreBankItem,
  type BankItemSummary,
  type BankStatus,
} from "@/api/useBank";
import {
  RECOMMENDATION_LABEL,
  useImportJob,
  usePauseSweep,
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
import { Albums, Pause, Remove, Spinner, Warning } from "@/components/icons";
import { AlbumRow } from "@/components/system/AlbumRow";
import { EmptyState } from "@/components/system/EmptyState";
import { PageBody, PageHeader } from "@/components/system/PageHeader";
import { Pagination, PAGE_SIZE } from "@/components/system/Pagination";
import { SectionLabel } from "@/components/system/SectionLabel";
import { StatusBanner } from "@/components/system/StatusBanner";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";

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
  // Sweep-origin jobs are excluded: a sweep banks decisions instead of parking
  // them, so its `albums` feed stays empty by design — the 1s job poll would
  // spend a whole multi-hour sweep computing decisions=[]. The sweep banner
  // deliberately rides the active probe's 5s cadence instead.
  const job = useImportJob(
    active?.active && active.origin !== "sweep"
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
  const nothingPending =
    settled && decisions.length === 0 && items.length === 0 && bankPendingTotal === 0;
  const pendingCount = decisions.length + items.length + bankPendingTotal;

  return (
    <PageBody>
      <PageHeader
        title="Review"
        meta={settled ? `${pendingCount} awaiting a decision` : undefined}
      />
      <p className="text-muted-foreground text-sm">
        Downloads and imports that need your decision — from every source, in
        one place.
      </p>

      {/* Sweep banner — counters ride the active probe's 5s cadence. */}
      {active?.active && active.origin === "sweep" && active.sweep && active.job_id && (
        <SweepBanner jobId={active.job_id} sweep={active.sweep} />
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
}: {
  albums: ImportAlbumSummary[];
  jobId: string;
}) {
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
function ImportingNowSection({ status }: { status: AcquisitionQueueStatus }) {
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
function SweepBanner({ jobId, sweep }: { jobId: string; sweep: SweepStatus }) {
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
          Sweeping — {sweep.processed} processed · {sweep.auto_applied} imported ·{" "}
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

/** The dropdown's value space: `""` = the needs-attention default (clean
 * URL), `"all"` = every status, otherwise one specific status. */
type BankFilter = "" | "all" | BankStatus;

const BANK_FILTERS: { value: BankFilter; label: string }[] = [
  { value: "", label: "Needs attention" },
  { value: "all", label: "All" },
  { value: "needs_review", label: "Needs review" },
  { value: "queued", label: "Queued" },
  { value: "applying", label: "Applying" },
  { value: "failed", label: "Failed" },
  { value: "stale", label: "Folder changed" },
  { value: "done", label: "Imported" },
  { value: "ignored", label: "Ignored" },
];

const BANK_STATUS_LABEL: Record<BankStatus, string> = {
  needs_review: "Needs review",
  queued: "Queued",
  applying: "Applying",
  done: "Imported",
  failed: "Failed",
  ignored: "Ignored",
  stale: "Folder changed",
};

const BANK_REASON_LABEL: Record<BankItemSummary["reason"], string> = {
  needs_review: "Uncertain match",
  needs_dup_resolution: "Already in library",
  no_match: "No match",
};

function isBankStatus(value: string | null): value is BankStatus {
  return (
    value !== null &&
    BANK_FILTERS.some((f) => f.value === value && f.value !== "" && f.value !== "all")
  );
}

/** Narrows a select value back into the filter space (no cast — the option
 * values all come from BANK_FILTERS). Unknown values fall to the default. */
function toBankFilter(value: string | null): BankFilter {
  return BANK_FILTERS.find((f) => f.value === value)?.value ?? "";
}

/**
 * "Waiting for review" — the durable bank backlog (spec §7), paginated from
 * day one. Default filter is "Needs attention" (`view=active`) so resolved
 * rows don't clutter the backlog while in-flight ones (a row just decided
 * shows back up as Queued) stay visible; All and the specific statuses remain
 * in the dropdown. Filter + offset live in the URL (`bank_status`/
 * `bank_offset`, default = clean URL, All = `bank_status=all`) so returning
 * from a row restores the page. Every action here is a bank store write —
 * NEVER gated on the import slot (only the stale re-scan inside the row page
 * needs the slot). The section hides entirely while the default view is
 * empty and unfiltered.
 */
function BankSection() {
  const [searchParams, setSearchParams] = useSearchParams();
  const filter = toBankFilter(searchParams.get("bank_status"));
  const status = isBankStatus(filter) ? filter : undefined;
  const offset = Math.max(0, Number(searchParams.get("bank_offset") ?? "0") || 0);

  const listQuery = useBankList({
    status,
    view: filter === "" ? "active" : undefined,
    offset,
    limit: PAGE_SIZE,
  });
  const ignore = useIgnoreBankItem();
  const bulkIgnore = useBulkIgnoreBank();
  const remove = useDeleteBankItem();
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set());

  const data = listQuery.data;
  if (!data || (data.total === 0 && filter === "" && offset === 0)) {
    return null;
  }

  // The selection pruned to rows still on screen: a row ignored via its own
  // button (or settled away by the 30s poll) keeps its id in `selected` until
  // the refetch lands — counting only visible ids keeps the bulk button's
  // count honest and the POST free of already-settled ids.
  const visibleSelected = data.items
    .filter((row) => selected.has(row.id))
    .map((row) => row.id);

  const setParams = (next: { filter: BankFilter; offset: number }) => {
    setSelected(new Set());
    setSearchParams((params) => {
      const copy = new URLSearchParams(params);
      if (next.filter === "") copy.delete("bank_status");
      else copy.set("bank_status", next.filter);
      if (next.offset === 0) copy.delete("bank_offset");
      else copy.set("bank_offset", String(next.offset));
      return copy;
    });
  };

  const conflictToast = (error: unknown) => {
    toast.error(
      error instanceof BankConflictError
        ? error.message
        : "That didn’t go through — the row may have changed state. Try again.",
    );
  };

  const toggle = (id: string, checked: boolean) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (checked) next.add(id);
      else next.delete(id);
      return next;
    });
  };

  return (
    <section aria-label="Waiting for review" className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <SectionLabel>Waiting for review · {data.total}</SectionLabel>
        <div className="flex items-center gap-2">
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={visibleSelected.length === 0 || bulkIgnore.isPending}
            onClick={() =>
              bulkIgnore.mutate(visibleSelected, {
                onSuccess: (res) => {
                  setSelected(new Set());
                  toast.success(`Ignored ${res.ignored} row${res.ignored === 1 ? "" : "s"}.`);
                },
                onError: conflictToast,
              })
            }
          >
            Ignore selected ({visibleSelected.length})
          </Button>
          {/* aria-label names the control (no room for a visible label in the
              toolbar row) — one accessible name, no redundant sr-only twin. */}
          <select
            aria-label="Filter by status"
            value={filter}
            onChange={(e) => setParams({ filter: toBankFilter(e.target.value), offset: 0 })}
            className="border-input bg-background focus-visible:border-ring focus-visible:ring-ring/50 h-8 appearance-none rounded-md border px-2 pr-7 text-sm shadow-xs focus-visible:ring-[3px] focus-visible:outline-none"
          >
            {BANK_FILTERS.map((f) => (
              <option key={f.value} value={f.value}>
                {f.label}
              </option>
            ))}
          </select>
        </div>
      </div>

      {data.items.length === 0 ? (
        offset > 0 ? (
          // The page is empty but the offset is past the end — the backlog
          // shrank under a stale `bank_offset` (e.g. the last page's rows were
          // bulk-ignored). The Pagination control hides once total fits one
          // page, so this branch IS the way back (the BrowsePage recipe).
          <EmptyState
            bordered
            icon={Albums}
            title="This page is empty — the backlog changed under it."
            action={
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => setParams({ filter, offset: 0 })}
              >
                Back to first page
              </Button>
            }
          />
        ) : (
          // At offset 0 an empty page can only mean an active filter matched
          // nothing (empty + unfiltered hides the whole section above).
          <p className="text-muted-foreground text-sm">No rows match this filter.</p>
        )
      ) : (
        <ul className="border-border divide-border divide-y overflow-hidden rounded-xl border">
          {data.items.map((row) => (
            <BankRow
              key={row.id}
              row={row}
              selected={selected.has(row.id)}
              onSelect={(checked) => toggle(row.id, checked)}
              onIgnore={() => ignore.mutate(row.id, { onError: conflictToast })}
              onRemove={() => remove.mutate(row.id, { onError: conflictToast })}
              busy={ignore.isPending || remove.isPending || bulkIgnore.isPending}
            />
          ))}
        </ul>
      )}

      {data.total > PAGE_SIZE && (
        <Pagination
          total={data.total}
          offset={offset}
          limit={PAGE_SIZE}
          busy={listQuery.isPlaceholderData}
          onOffsetChange={(next) => setParams({ filter, offset: next })}
        />
      )}
    </section>
  );
}

/** One backlog row: [checkbox (needs_review only)] AlbumRow + Open/Ignore/Remove.
 * The status chip names the lifecycle for settled rows; needs_review rows
 * show the REASON instead (what kind of decision awaits). Failed rows carry
 * their error in the meta line. */
function BankRow({
  row,
  selected,
  onSelect,
  onIgnore,
  onRemove,
  busy,
}: {
  row: BankItemSummary;
  selected: boolean;
  onSelect: (checked: boolean) => void;
  onIgnore: () => void;
  onRemove: () => void;
  busy: boolean;
}) {
  const title = (row.album ?? lastSegment(row.folder)) || "Unknown album";
  const metaBits = [
    row.confidence != null ? `${Math.round(row.confidence)}%` : null,
    row.recommendation ?? null,
    row.status === "failed" && row.error ? row.error : null,
  ].filter((b): b is string => Boolean(b));
  return (
    <li className="flex items-center gap-0">
      {row.status === "needs_review" ? (
        <Checkbox
          className="ml-4"
          checked={selected}
          onCheckedChange={(checked) => onSelect(checked === true)}
          aria-label={`Select ${title}`}
        />
      ) : (
        <span className="ml-4 w-4 shrink-0" aria-hidden="true" />
      )}
      <div className="min-w-0 flex-1">
        <AlbumRow
          cover={null}
          title={title}
          subtitle={row.artist ?? "Unknown artist"}
          meta={metaBits.join(" · ") || undefined}
          badge={
            <Badge variant={row.status === "needs_review" ? "default" : row.status === "failed" || row.status === "stale" ? "outline" : "secondary"}>
              {row.status === "needs_review" ? BANK_REASON_LABEL[row.reason] : BANK_STATUS_LABEL[row.status]}
            </Badge>
          }
          action={
            <div className="flex items-center gap-1.5">
              {row.status === "needs_review" && (
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  disabled={busy}
                  aria-label={`Ignore ${title}`}
                  onClick={onIgnore}
                >
                  Ignore
                </Button>
              )}
              <AlertDialog>
                <AlertDialogTrigger asChild>
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    disabled={busy}
                    aria-label={`Remove ${title}`}
                  >
                    <Remove aria-hidden="true" />
                  </Button>
                </AlertDialogTrigger>
                <AlertDialogContent>
                  <AlertDialogHeader>
                    <AlertDialogTitle>Remove this row?</AlertDialogTitle>
                    <AlertDialogDescription>
                      The files stay on disk, but the banked candidates are
                      forfeited — a re-sweep will NOT pick this folder up again.
                    </AlertDialogDescription>
                  </AlertDialogHeader>
                  <AlertDialogFooter>
                    <AlertDialogCancel>Cancel</AlertDialogCancel>
                    <AlertDialogAction onClick={onRemove}>Remove</AlertDialogAction>
                  </AlertDialogFooter>
                </AlertDialogContent>
              </AlertDialog>
              <Button size="sm" asChild>
                <Link to={`/review/bank/${row.id}`} aria-label={`Open ${title}`}>
                  Open
                </Link>
              </Button>
            </div>
          }
        />
      </div>
    </li>
  );
}

/** Last path segment of a folder, for a row with no parsed album title and the
 * importing-now line. */
function lastSegment(folder: string): string {
  const parts = folder.split("/").filter(Boolean);
  return parts.length > 0 ? parts[parts.length - 1] : folder;
}
