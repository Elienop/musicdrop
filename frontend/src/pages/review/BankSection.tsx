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
import { RECOMMENDATION_LABEL } from "@/api/useImport";
import { Albums, Remove, Warning } from "@/components/icons";
import { AlbumRow } from "@/components/system/AlbumRow";
import { EmptyState } from "@/components/system/EmptyState";
import { ErrorState } from "@/components/system/ErrorState";
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
import { SEGMENT_SEP } from "@/lib/format";

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

/** Tri-state for the header checkbox: all eligible rows ticked, some, or none. */
function triState(
  allSelected: boolean,
  someSelected: boolean,
): boolean | "indeterminate" {
  if (allSelected) return true;
  if (someSelected) return "indeterminate";
  return false;
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
  // A load error with no cached page must SHOW the failure (with a retry), never
  // silently unmount: an errored probe vanishing reads as "backlog resolved", so
  // the calm empty state would hide banked rows still awaiting a decision.
  if (listQuery.isError && !data) {
    return (
      <section aria-label={heading} className="flex flex-col gap-3">
        <SectionLabel>{heading}</SectionLabel>
        <ErrorState
          variant="inline"
          message="Couldn’t load the review bank."
          onRetry={() => void listQuery.refetch()}
        />
      </section>
    );
  }
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

  const allSelected =
    selectableIds.length > 0 && selectableIds.every((id) => selected.has(id));
  const headerChecked = triState(allSelected, visibleSelected.length > 0);
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
        : "That didn’t go through; the row may have changed state. Try again.",
    );
  };

  // The empty-backlog view: which message/action depends on whether the
  // offset overshot, the default view is pristine, or a filter matched
  // nothing.
  const renderEmptyBacklog = () => {
    if (offset > 0) {
      // The page is empty but the offset is past the end — the backlog
      // shrank under a stale `bank_offset` (e.g. the last page's rows were
      // bulk-ignored). The Pagination control hides once total fits one
      // page, so this branch IS the way back (the BrowsePage recipe).
      return (
        <EmptyState
          bordered
          icon={Albums}
          title="This page is empty; the backlog changed under it."
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
      );
    }
    if (filter === "" && reasonFilter === "") {
      // The pristine "Needs attention" view is empty but rows still exist
      // (total_all > 0) — everything's resolved. Point at the filters so
      // the Imported/Ignored history stays discoverable, not a dead end.
      return (
        <p className="text-muted-foreground text-sm">
          Nothing needs attention. Switch the filter to see resolved history.
        </p>
      );
    }
    // An active status/reason filter matched nothing on this page.
    return <p className="text-muted-foreground text-sm">No rows match this filter.</p>;
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
                  forfeited; a re-sweep will NOT pick these folders up again.
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
        renderEmptyBacklog()
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
 * Open/Ignore/Remove. Under 28rem of row the action group drops to its own
 * line under the row (decisions 39).
 * The status chip names the lifecycle for settled rows; needs_review rows
 * show the REASON instead (what kind of decision awaits). Failed rows carry
 * their error on its own line below the row — below the action line, keeping
 * the reading order the one-line arm has. */

/** The row's recommendation tier, humanized. The bank contract types this
 * field as a bare `string` rather than the `Recommendation` enum the import
 * feed carries, so an unknown tier falls through as itself instead of
 * rendering `undefined`. */
const RECOMMENDATION_TEXT: Readonly<Record<string, string>> =
  RECOMMENDATION_LABEL;
function recommendationLabel(value: string | null | undefined): string | null {
  if (value == null) return null;
  return RECOMMENDATION_TEXT[value] ?? value;
}

/** Chip tone per status: the decision awaiting stands out, the attention
 * states recede to an outline, and the settled/quiet ones recede furthest. */
function bankBadgeVariant(
  status: BankStatus,
): "default" | "outline" | "secondary" {
  if (status === "needs_review") return "default";
  if (status === "failed" || status === "stale") return "outline";
  return "secondary";
}

function BankRow({
  row,
  selected,
  onSelect,
  onIgnore,
  onRemove,
  busy,
}: Readonly<{
  row: BankItemSummary;
  selected: boolean;
  onSelect: (checked: boolean) => void;
  onIgnore: () => void;
  onRemove: () => void;
  busy: boolean;
}> ) {
  const title = (row.album ?? lastSegment(row.folder)) || "Unknown album";
  const metaBits = [
    row.confidence != null ? `${Math.round(row.confidence)}%` : null,
    recommendationLabel(row.recommendation),
  ].filter((b): b is string => Boolean(b));
  // NOT in `meta`: that slot is `shrink-0`, so in the row arm its used width is
  // max-content and it can neither shrink nor wrap. This string is `str(exc)`
  // from the apply runner (`app/bank/apply_runner.py`) — unbounded — so in that
  // slot the meta cell's ink starved the sibling subtitle and painted across
  // the row's own controls. Measurements are in BACKLOG under "A failed bank
  // row's error overran the row"; keep them there, not here.
  // `line-clamp-2` bounds the row: the whole string is reached through the
  // row's own Open link, where `BankReviewPage` renders it untruncated in a
  // `role="alert"` banner. `title` is a hover extra, not that route — a <p>
  // takes no keyboard focus and touch has no hover.
  const failure = row.status === "failed" ? (row.error ?? "").trim() : "";
  return (
    // A GRID, not stacked flex wrappers: `items-center` centres each item in
    // its own row TRACK, so neither the dropped action line nor the error line
    // can re-centre the select checkbox against a taller box — the 16/26px
    // drift #220 fixed, which a second sibling line would have re-opened.
    // `/duplicates`' MemberRow takes the same shape for the same reason.
    // Column 3 is `auto`: in the dropped arm nothing is placed in it and it
    // collapses to 0, so one template serves both arms.
    <li className="@container/bankrow grid grid-cols-[auto_minmax(0,1fr)_auto] items-center">
      {row.status !== "applying" ? (
        <Checkbox
          className="ml-4"
          checked={selected}
          onCheckedChange={(checked) => onSelect(checked === true)}
          aria-label={`Select ${title}`}
        />
      ) : (
        <span className="ml-4 w-4" aria-hidden="true" />
      )}
      <AlbumRow
        cover={null}
        title={title}
        subtitle={row.artist ?? "Unknown artist"}
        meta={metaBits.join(SEGMENT_SEP) || undefined}
        badge={
          <Badge variant={bankBadgeVariant(row.status)}>
            {row.status === "needs_review" ? BANK_REASON_LABEL[row.reason] : BANK_STATUS_LABEL[row.status]}
          </Badge>
        }
      />
      {/* Not AlbumRow's `action` slot: from there the group cannot leave the
          row without growing AlbumRow's own box. Below 28rem of ROW (not of
          viewport — at 768px the sidebar opens and the row is NARROWER than at
          520px) it takes its own line under the row, inset to the cover's edge
          like the error line. The threshold is bounded on both sides: a
          needs_review row's fixed content is 412.76px (checkbox slot 32 +
          px-4 16 + cover 40 + gap 12 + badge 107.98 + gap 8 + gap 12 + actions
          168.78 + px-4 16), so under 424.09 (+ the 11.33px ellipsis glyph) the
          title cannot even ellipse — measured 0px up to a 409px row; and the
          NARROWEST row a desktop ever shows is 473px, at the 768px sidebar
          step, so anything above that would drop the actions on a desktop.
          28rem = 448px sits between: the title gets 34.23px there instead of 0.
          `-ml-1` gives back the 4px by which AlbumRow's px-4 exceeds its own
          gap-3, so the inline arm keeps today's 12px gap and 16px inset. */}
      <div
        className="col-start-2 row-start-2 mb-3 ml-4 flex items-center gap-1.5 @min-[28rem]/bankrow:col-start-3 @min-[28rem]/bankrow:row-start-1 @min-[28rem]/bankrow:mb-0 @min-[28rem]/bankrow:-ml-1 @min-[28rem]/bankrow:mr-4"
      >
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
                forfeited; a re-sweep will NOT pick this folder up again.
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
      {failure !== "" && (
        // Its own line, so the width it needs is the row's, not the meta slot's,
        // and `pl-12` re-states the row's own content inset (checkbox slot 32 +
        // AlbumRow's px-4) rather than inheriting it.
        // The padding is on the wrapper, not the clamped element: line-clamp
        // clips at the PADDING box, so a third line paints into any padding the
        // clamped element carries itself (measured at 360px).
        // `min-w-0` on the clamped span is load-bearing — it is a flex item of
        // the <p>, so without it an unbroken path sets its own floor and
        // `break-words` cannot lower it (same pair as SettingsTrashPage).
        // The icon carries the error register: the body stays muted so a long
        // reason does not shout, but a failure reason must not read as the
        // artist subtitle directly above it.
        <div className="col-span-3 row-start-3 pr-4 pb-3 pl-12 @min-[28rem]/bankrow:row-start-2">
          <p
            className="text-muted-foreground flex items-start gap-1.5 text-sm"
            title={failure}
          >
            <Warning
              className="text-destructive mt-0.5 size-3.5 shrink-0"
              aria-hidden="true"
            />
            <span className="line-clamp-2 min-w-0 break-words">{failure}</span>
          </p>
        </div>
      )}
    </li>
  );
}
