import { useState } from "react";
import { Link, useSearchParams } from "react-router";
import { toast } from "sonner";

import {
  BankConflictError,
  useBankList,
  useBulkDeleteBank,
  useBulkIgnoreBank,
  useDeleteBankItem,
  useIgnoreBankItem,
  type BankItemSummary,
  type BankReason,
  type BankStatus,
} from "@/api/useBank";
import { Albums, Remove } from "@/components/icons";
import { AlbumRow } from "@/components/system/AlbumRow";
import { EmptyState } from "@/components/system/EmptyState";
import { Pagination, PAGE_SIZE } from "@/components/system/Pagination";
import { SectionLabel } from "@/components/system/SectionLabel";
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

import { lastSegment } from "./lastSegment";

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

const BANK_REASON_LABEL: Record<BankReason, string> = {
  needs_review: "Uncertain match",
  needs_dup_resolution: "Already in library",
  no_match: "No match",
};

/** The reason dropdown's value space: `""` = every reason (param absent),
 * otherwise one specific banked reason. */
type BankReasonFilter = "" | BankReason;

const BANK_REASON_FILTERS: { value: BankReasonFilter; label: string }[] = [
  { value: "", label: "All reasons" },
  { value: "needs_review", label: BANK_REASON_LABEL.needs_review },
  { value: "needs_dup_resolution", label: BANK_REASON_LABEL.needs_dup_resolution },
  { value: "no_match", label: BANK_REASON_LABEL.no_match },
];

/** Narrows a select value back into the reason-filter space (no cast — the
 * option values all come from BANK_REASON_FILTERS). Unknown falls to default. */
function toBankReason(value: string | null): BankReasonFilter {
  return BANK_REASON_FILTERS.find((f) => f.value === value)?.value ?? "";
}

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

/** Section heading for the active filter. The default view keeps the spec §7
 * section name; every other view names what it actually shows (statuses reuse
 * BANK_STATUS_LABEL), so resolved history is never presented as "waiting". */
function bankHeading(filter: BankFilter): string {
  if (filter === "") return "Waiting for review";
  if (filter === "all") return "All imports";
  return BANK_STATUS_LABEL[filter];
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
export function BankSection() {
  const [searchParams, setSearchParams] = useSearchParams();
  const filter = toBankFilter(searchParams.get("bank_status"));
  const heading = bankHeading(filter);
  const status = isBankStatus(filter) ? filter : undefined;
  const reasonFilter = toBankReason(searchParams.get("bank_reason"));
  const reason = reasonFilter === "" ? undefined : reasonFilter;
  const offset = Math.max(0, Number(searchParams.get("bank_offset") ?? "0") || 0);

  const listQuery = useBankList({
    status,
    view: filter === "" ? "active" : undefined,
    reason,
    offset,
    limit: PAGE_SIZE,
  });
  const ignore = useIgnoreBankItem();
  const bulkIgnore = useBulkIgnoreBank();
  const bulkDelete = useBulkDeleteBank();
  const remove = useDeleteBankItem();
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set());

  const data = listQuery.data;
  // Hide only when the bank is truly empty (no rows of ANY status). Once any
  // row exists the section stays mounted — its filters are the ONLY path to
  // the resolved (Imported/Ignored) history, so an empty active view must not
  // take the whole section (and its dropdowns) down with it.
  if (!data || data.total_all === 0) {
    return null;
  }

  // Every on-screen row that can be acted on: only an `applying` row is
  // un-selectable (it can't be ignored or deleted). These ids drive both the
  // select-all toggle and its tri-state.
  const selectableIds = data.items
    .filter((row) => row.status !== "applying")
    .map((row) => row.id);

  // The selection pruned to rows still on screen: a row ignored via its own
  // button (or settled away by the 30s poll) keeps its id in `selected` until
  // the refetch lands — counting only visible ids keeps the bulk button's
  // count honest and the POST free of already-settled ids.
  const visibleSelected = data.items
    .filter((row) => selected.has(row.id))
    .map((row) => row.id);

  // "Ignore" only flips needs_review rows server-side, so its label + payload
  // count ONLY those — the wider checkboxes also select failed/done/stale/etc
  // (which Delete handles), and counting them would over-promise the ignore.
  const ignorableSelected = data.items
    .filter((row) => selected.has(row.id) && row.status === "needs_review")
    .map((row) => row.id);

  // Tri-state for the header checkbox: all eligible rows ticked, some, or none.
  const allSelected =
    selectableIds.length > 0 && selectableIds.every((id) => selected.has(id));
  const headerChecked: boolean | "indeterminate" = allSelected
    ? true
    : visibleSelected.length > 0
      ? "indeterminate"
      : false;
  const toggleAll = (checked: boolean) => {
    setSelected(checked ? new Set(selectableIds) : new Set());
  };

  const setParams = (next: {
    filter: BankFilter;
    reason: BankReasonFilter;
    offset: number;
  }) => {
    setSelected(new Set());
    setSearchParams((params) => {
      const copy = new URLSearchParams(params);
      if (next.filter === "") copy.delete("bank_status");
      else copy.set("bank_status", next.filter);
      if (next.reason === "") copy.delete("bank_reason");
      else copy.set("bank_reason", next.reason);
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
    <section aria-label={heading} className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <SectionLabel>{heading} · {data.total}</SectionLabel>
        <div className="flex flex-wrap items-center gap-2">
          <div className="flex items-center gap-2">
            <Checkbox
              id="bank-select-all"
              checked={headerChecked}
              onCheckedChange={(checked) => toggleAll(checked === true)}
              disabled={selectableIds.length === 0}
              aria-label="Select all"
            />
            <label htmlFor="bank-select-all" className="text-sm font-medium">
              Select all
            </label>
          </div>
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={ignorableSelected.length === 0 || bulkIgnore.isPending}
            onClick={() =>
              bulkIgnore.mutate(ignorableSelected, {
                onSuccess: (res) => {
                  setSelected(new Set());
                  toast.success(`Ignored ${res.ignored} row${res.ignored === 1 ? "" : "s"}.`);
                },
                onError: conflictToast,
              })
            }
          >
            Ignore selected ({ignorableSelected.length})
          </Button>
          <AlertDialog>
            <AlertDialogTrigger asChild>
              <Button
                type="button"
                variant="outline"
                size="sm"
                disabled={visibleSelected.length === 0 || bulkDelete.isPending}
              >
                Delete selected ({visibleSelected.length})
              </Button>
            </AlertDialogTrigger>
            <AlertDialogContent>
              <AlertDialogHeader>
                <AlertDialogTitle>
                  Remove {visibleSelected.length} row
                  {visibleSelected.length === 1 ? "" : "s"}?
                </AlertDialogTitle>
                <AlertDialogDescription>
                  The files stay on disk, but the banked candidates are
                  forfeited — a re-sweep will NOT pick these folders up again.
                </AlertDialogDescription>
              </AlertDialogHeader>
              <AlertDialogFooter>
                <AlertDialogCancel>Cancel</AlertDialogCancel>
                <AlertDialogAction
                  onClick={() =>
                    bulkDelete.mutate(visibleSelected, {
                      onSuccess: (res) => {
                        setSelected(new Set());
                        toast.success(
                          `Removed ${res.deleted} row${res.deleted === 1 ? "" : "s"}.`,
                        );
                      },
                      onError: conflictToast,
                    })
                  }
                >
                  Remove
                </AlertDialogAction>
              </AlertDialogFooter>
            </AlertDialogContent>
          </AlertDialog>
          {/* aria-label names the control (no room for a visible label in the
              toolbar row) — one accessible name, no redundant sr-only twin. */}
          <select
            aria-label="Filter by status"
            value={filter}
            onChange={(e) =>
              setParams({
                filter: toBankFilter(e.target.value),
                reason: reasonFilter,
                offset: 0,
              })
            }
            className="border-input bg-background focus-visible:border-ring focus-visible:ring-ring/50 h-8 appearance-none rounded-md border px-2 pr-7 text-sm shadow-xs focus-visible:ring-[3px] focus-visible:outline-none"
          >
            {BANK_FILTERS.map((f) => (
              <option key={f.value} value={f.value}>
                {f.label}
              </option>
            ))}
          </select>
          <select
            aria-label="Filter by reason"
            value={reasonFilter}
            onChange={(e) =>
              setParams({ filter, reason: toBankReason(e.target.value), offset: 0 })
            }
            className="border-input bg-background focus-visible:border-ring focus-visible:ring-ring/50 h-8 appearance-none rounded-md border px-2 pr-7 text-sm shadow-xs focus-visible:ring-[3px] focus-visible:outline-none"
          >
            {BANK_REASON_FILTERS.map((f) => (
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
                onClick={() => setParams({ filter, reason: reasonFilter, offset: 0 })}
              >
                Back to first page
              </Button>
            }
          />
        ) : filter === "" && reasonFilter === "" ? (
          // The pristine "Needs attention" view is empty but rows still exist
          // (total_all > 0) — everything's resolved. Point at the filters so
          // the Imported/Ignored history stays discoverable, not a dead end.
          <p className="text-muted-foreground text-sm">
            Nothing needs attention — switch the filter to see resolved history.
          </p>
        ) : (
          // An active status/reason filter matched nothing on this page.
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
          onOffsetChange={(next) =>
            setParams({ filter, reason: reasonFilter, offset: next })
          }
        />
      )}
    </section>
  );
}

/** One backlog row: [checkbox (every row except applying)] AlbumRow +
 * Open/Ignore/Remove.
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
      {row.status !== "applying" ? (
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
