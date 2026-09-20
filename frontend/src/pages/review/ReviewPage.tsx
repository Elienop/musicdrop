import { type RefObject, useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router";

import {
  useAcquisitionStatus,
  type AcquisitionQueueStatus,
} from "@/api/useAcquisitionStatus";
import { useActiveImport } from "@/api/useActiveImport";
import { useBankList } from "@/api/useBank";
import {
  RECOMMENDATION_LABEL,
  startErrorSentence,
  useImportJob,
  useStopImport,
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
import { ErrorState } from "@/components/system/ErrorState";
import { PageBody, PageHeader } from "@/components/system/PageHeader";
import { SectionLabel } from "@/components/system/SectionLabel";
import { StatusBanner } from "@/components/system/StatusBanner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { SEGMENT_SEP, plural } from "@/lib/format";

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
  // The two inbox start mutations live at PAGE level, not inside the section
  // they belong to — see {@link useInboxStart} for the defect that forced it.
  // The listing itself goes IN because the focus rescue is decided by it: the
  // query's structural sharing hands back the SAME array while nothing changes,
  // so the effect only re-runs on a listing that actually moved.
  const inbox = useInboxStart(
    (jobId) => navigate(`/import?job=${jobId}`),
    inboxQuery.data?.items,
  );
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
  // A refusal the user has not dismissed is the opposite of an all-clear: the
  // misconfiguration it names ("That folder can't be read.") is also what drops
  // that folder from the listing, so the backlog under it is UNDERSTATED by at
  // least one. Both the verdict and the count are gated on it — a count of 0
  // beside a red sentence is the same all-clear one element up.
  const unreadRefusal = inbox.refusal !== null;
  const nothingPending =
    settled &&
    !probesErrored &&
    !unreadRefusal &&
    decisions.length === 0 &&
    items.length === 0 &&
    bankPendingTotal === 0;
  const pendingCount = decisions.length + items.length + bankPendingTotal;

  return (
    <PageBody>
      <PageHeader
        title="Review"
        meta={
          settled && !probesErrored && !unreadRefusal
            ? `${pendingCount} awaiting a decision`
            : undefined
        }
      />
      <p className="text-muted-foreground text-sm">
        Downloads and imports that need your decision, from every source, in
        one place.
      </p>

      {/* The inbox start refusal, ABOVE every section and outside all of them.
          It takes focus when the control that produced it VANISHED
          (useInboxStart), which is what makes it perceivable from a Review
          button ten rows down: `role="alert"` alone only serves screen readers,
          and nothing else scrolled or moved.

          Dismiss is the refusal's expiry. It has none of its own: a mutation
          error stands until that mutation re-runs, and the only controls that
          re-run it are the two start buttons — which is exactly what an
          unreadable folder takes off the page with the row. Without it the page
          has no way back: the count and "Nothing to review." stay gated for the
          session, and a folder fixed on the server comes back listed under a
          red sentence saying it cannot be read. An automatic expiry was weighed
          and rejected: the per-item refusal names no folder ("That folder can't
          be read.", import_jobs/runner.py), so "a listing that no longer names
          it" is not a predicate this page can evaluate — and the listing that
          DOES drop it is the very refetch the sentence has to survive.

          `max-w-prose` caps the measure: this is PageBody's DEFAULT width, the
          full shell, wider than the ~770px pane the Trash page capped for the
          same reason (SettingsTrashPage). No `w-full` beside it — that sibling
          needs one because its parent is `items-start`, and a stretched flex
          item is already full width. Measured 584.48px at a 1036px viewport.
          `break-words` because a carried 503 holds repr'd paths, which Chromium
          will not break at `/`: measured docScrollWidth 1036 at viewport 1036
          with a 96-character unbroken path. */}
      {inbox.refusal !== null && (
        <div className="flex max-w-prose items-start gap-2">
          <p
            ref={inbox.alertRef}
            tabIndex={-1}
            role="alert"
            id={INBOX_REFUSAL_ID}
            className="text-destructive focus-ring min-w-0 text-sm break-words"
          >
            {inbox.refusal}
          </p>
          {/* Outside the alert node, so it is not read out as part of the
              sentence. `-mt-1.5` is HALF the 12px by which a size-sm button
              (h-8, 32px) exceeds the 20px line it sits on, so the two labels
              share a centre; `items-start` keeps it on the FIRST line when the
              sentence wraps. Measured in Chromium: line centre 34 / button
              centre 34 on one line, 84 / 84 on three. */}
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="-mt-1.5 shrink-0"
            onClick={inbox.dismissRefusal}
          >
            Dismiss
          </Button>
        </div>
      )}

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

      {inboxQuery.isError && (
        // The listing probe can fail now, and a silent failure here reads as an
        // empty backlog. One line, one retry — the house error recipe. The
        // section below is called "Waiting in the inbox", so the sentence says
        // that and not "the inbox backlog", which is our word, not the user's.
        <ErrorState
          variant="inline"
          message="Couldn’t load what’s waiting in the inbox."
          onRetry={() => void inboxQuery.refetch()}
        />
      )}

      <InboxSection items={items} importActive={importActive} inbox={inbox} />

      {/* The no-op result, at PAGE level and immediately after the section, so
          it keeps the position it had inside it and survives the section's own
          unmount. Its "Nothing left to import; the inbox just cleared." branch
          fires exactly when the inbox emptied, which is what the start's
          `onSettled` refetch is about to discover — so nested it painted and
          was destroyed inside one round trip. The empty state only replaces the
          section when the bank and decision lists are empty too, so with one
          banked row pending the user watched the rows vanish with no sentence
          ever readable. Always mounted (the text toggles, never the element):
          a region created together with its text reads inconsistently. */}
      <span
        role="status"
        aria-live="polite"
        className={
          inbox.noOpMessage ? "text-muted-foreground text-sm" : "sr-only"
        }
      >
        {inbox.noOpMessage}
      </span>

      {nothingPending && (
        <output className="text-muted-foreground text-sm block">
          Nothing to review. Completed downloads that need a decision show up
          here.
        </output>
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
            // The same drop as the bank row (decisions 39): the defect
            // reproduces here, narrower — the title measures 0px at 320→344
            // and at 320→328 the "Already in library" badge's ink sits inside
            // Resolve's hit rectangle, so a tap there fired Resolve. A grid so
            // `items-center` centres each item in its OWN row track. This
            // row's fixed content is 285.37px (px-4 16 + cover 40 + gap 12 +
            // badge 107.98 + gap 8 + gap 12 + Resolve 73.39 + px-4 16), so
            // under 296.7 (+ the 11.33px ellipsis glyph) the title cannot
            // ellipse; the narrowest row a desktop shows is 473px, at the
            // 768px sidebar step. The switch is at 28rem — the owner's number
            // (2026-09-11), the same on all three rows. The floor is true AS a
            // floor, but at 20rem the phone band went inline and the title
            // collapsed: viewport 392/400/414/430 measured 42/50/66/74px,
            // 6/7/10/10 characters of a 46-character album, against
            // 127/135/151/159px at 28rem. 448 still clears the 473 ceiling,
            // so no desktop row drops; measured switch at viewport 513.
            <li
              key={album.index}
              className="@container/decisionrow bg-primary/5 grid grid-cols-[minmax(0,1fr)_auto] items-center"
            >
              <AlbumRow
                cover={null}
                title={title}
                subtitle={album.artist ?? "Unknown artist"}
                meta={
                  needsDup
                    ? undefined
                    : // Same string, same AlbumRow slot and now the same
                      // separator as the import feed builds at ImportPage's
                      // FeedRow — one dialect for the app's `%` · `match`
                      // line. Measured inert in this slot (see SEGMENT_SEP).
                      `${Math.round(album.confidence)}%${SEGMENT_SEP}${RECOMMENDATION_LABEL[album.recommendation]}`
                }
                badge={
                  <Badge variant="default">
                    {needsDup ? "Already in library" : "Needs review"}
                  </Badge>
                }
              />
              {/* `-ml-1` gives back the 4px by which AlbumRow's px-4 exceeds
                  its own gap-3, so the inline arm keeps today's 12px gap. */}
              <div className="col-start-1 row-start-2 mb-3 ml-4 flex items-center @min-[28rem]/decisionrow:col-start-2 @min-[28rem]/decisionrow:row-start-1 @min-[28rem]/decisionrow:mb-0 @min-[28rem]/decisionrow:-ml-1 @min-[28rem]/decisionrow:mr-4">
                <Button size="sm" asChild>
                  <Link to={to} state={REVIEW_ORIGIN}>
                    {needsDup ? "Resolve" : "Review"}
                  </Link>
                </Button>
              </div>
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
      <output
        className="text-muted-foreground flex items-center gap-2 text-sm"
      >
        <Spinner className="size-4 shrink-0 animate-spin" aria-hidden="true" />
        <span>
          {status.current !== null
            ? `Importing ${lastSegment(status.current)}`
            : "Waiting for the import slot"}
          {` · ${status.queued} queued`}
        </span>
      </output>
    </section>
  );
}

/** The no-op start message from the skipped-still-arriving count: `null`
 * = no attempt yet, 0 = the inbox really did clear, >0 = "not yet". */
function noOpInFlightMessage(skipped: number | null): string {
  if (skipped === null) return "";
  if (skipped > 0) {
    return (
      `Still downloading — ${skipped} ${plural(skipped, "folder is", "folders are")} ` +
      "still receiving files. They'll be importable once they finish."
    );
  }
  return "Nothing left to import; the inbox just cleared.";
}

/** Row subtitle: the folder still receiving files says so FIRST ("Review
 * all" skips it, and the per-row Review is an informed override), otherwise
 * the honest outcome — set-aside or failed — and nothing for a fresh drop. */
function inboxSubtitle(item: InboxItem): string | undefined {
  if (item.in_flight) {
    return "Still downloading — importing now may catch only part of it";
  }
  if (item.outcome === "set_aside") return "Set aside";
  if (item.outcome === "failed") return "Import failed";
  return undefined;
}

/** Row Review button label: the attempt state first, then the still-arriving
 * override, then the plain action. */
function inboxReviewLabel(starting: boolean, inFlight: boolean): string {
  if (starting) return "Starting…";
  if (inFlight) return "Review anyway";
  return "Review";
}

/** The generic start-failure sentence both inbox actions fall back to. It names
 * two outcomes; a refusal that carries a reason usually has a third (the swap
 * lock, a backfill, the share gone, the folder unreadable), so the server's own
 * sentence wins through {@link startErrorSentence}, the helper every other start
 * surface uses. */
const INBOX_START_GENERIC =
  "Couldn’t start; it may have just been imported, or another import is running. Try again in a moment.";

/** The id the refusal alert carries, so both start buttons can point their
 * `aria-describedby` at it — the shape ImportPage's Start and BankReviewPage's
 * "Review now" already use. A control that survives its own refusal keeps
 * focus, and this is what a keyboard user hears on coming back to it. */
const INBOX_REFUSAL_ID = "inbox-start-error";

/** The two inbox start actions as ONE surface: which one is running, what the
 * last one answered, and the single alert slot they share. */
interface InboxStart {
  /** "Review all" is running. */
  readonly allPending: boolean;
  /** The row name a per-item start is running for, or null. */
  readonly oneStarting: string | null;
  /** The sentence the LAST start failed with, or null. */
  readonly refusal: string | null;
  /** The no-op result, or "" when the last start was not a no-op. */
  readonly noOpMessage: string;
  /** The page-level alert node, focused when the pressed control vanished. */
  readonly alertRef: RefObject<HTMLParagraphElement | null>;
  readonly startOne: (name: string) => void;
  readonly startAll: () => void;
  /** Clear the refusal. The only way back once the listing it was about has
   * emptied and taken both start buttons with it. */
  readonly dismissRefusal: () => void;
}

/**
 * Both inbox start mutations, owned by the PAGE rather than by the section.
 *
 * Three defects forced the lift, and all three are about the alert slot:
 *
 * 1. {@link InboxSection} returns null on an empty list, and both mutations
 *    invalidate `["inbox-items"]` in `onSettled` — on failure too. The backend
 *    omits a folder it cannot walk from the listing, so the one misconfiguration
 *    the 422 was added to diagnose ("That folder can't be read.") is also the
 *    one that empties the list: the sentence rendered, the refetch landed, the
 *    section unmounted with the alert inside it, and the page finished on
 *    "Nothing to review." — an all-clear for the fault just reported.
 * 2. The alert sat several hundred px from the control that produced it, with
 *    nothing scrolling and nothing moving. It is focused here instead — but
 *    ONLY when the pressed control vanished with its row, which is the case
 *    defect 1 describes. Both start buttons are `aria-disabled` rather than
 *    `disabled` while they run, so a surviving one still holds focus and a
 *    focus move would be a theft: a 409 or a 503 leaves the whole list
 *    standing, and the walk back from a page-level alert is the rest of the
 *    page (the bank alone pages 48 rows). `aria-describedby` carries the
 *    sentence to the button in that case instead, the shape ImportPage's Start
 *    already uses.
 * 3. One slot, two mutations: TanStack keeps a mutation's error until THAT
 *    mutation re-runs, so `reviewOne.error ?? reviewAll.error` took the stale
 *    non-null first. Each start resets its SIBLING, so at most one of the two is
 *    ever non-null and the `??` below can only read the last press.
 *
 * `listing` is the inbox query's own array. It is a DEPENDENCY, not data: the
 * refusal lands before the refetch it triggers does, so at that first commit the
 * pressed button is still mounted and still focused — the rescue can only be
 * decided once the listing has moved. The query's structural sharing hands back
 * the same reference while nothing changes, so a refusal that left the list
 * intact never re-runs the effect at all.
 */
function useInboxStart(
  onStarted: (jobId: string) => void,
  listing: readonly InboxItem[] | undefined,
): InboxStart {
  const reviewOne = useImportInboxItem();
  const reviewAll = useReviewInbox();
  // null = no no-op yet. Otherwise the number of folders the backend SKIPPED as
  // still-arriving: 0 means the inbox really did clear, >0 means "not yet".
  const [noOpInFlight, setNoOpInFlight] = useState<number | null>(null);
  // Bumped on every failure so a SECOND press answering the same sentence still
  // re-focuses the alert — the string alone would not change.
  const [failures, setFailures] = useState(0);
  const alertRef = useRef<HTMLParagraphElement>(null);

  // A started import navigates away. A no-op has TWO causes and they must not
  // read the same: the inbox emptied since the last poll (nothing left), or every
  // folder is still receiving files (`in_flight` > 0) — in which case the rows
  // the user is looking at are still there and telling them it cleared is a lie.
  const mutateOpts = {
    onSuccess: (res: { started?: boolean; job_id?: string | null; in_flight?: number }) => {
      if (res.started && res.job_id) onStarted(res.job_id);
      else setNoOpInFlight(res.in_flight ?? 0);
    },
    onError: () => setFailures((n) => n + 1),
  };

  const refusal = startErrorSentence(
    reviewOne.error ?? reviewAll.error,
    reviewOne.isError || reviewAll.isError,
    INBOX_START_GENERIC,
  );

  // Three dependencies: the tick and the sentence can land in either order (and
  // usually in one commit), the alert only exists once `refusal` is set, and the
  // listing is what tells the gate below whether the pressed control survived.
  useEffect(() => {
    if (refusal === null) return;
    // `<body>` is the tell that the control the user pressed is GONE: it holds
    // focus while it lives (its pending half is aria-disabled, not disabled),
    // so anything else holding focus means there is still something to go back
    // to — and `aria-describedby` has already carried the sentence to it.
    if (document.activeElement !== document.body) return;
    alertRef.current?.focus();
  }, [refusal, failures, listing]);

  return {
    allPending: reviewAll.isPending,
    oneStarting: reviewOne.isPending ? (reviewOne.variables ?? null) : null,
    refusal,
    noOpMessage: noOpInFlightMessage(noOpInFlight),
    alertRef,
    startOne: (name) => {
      setNoOpInFlight(null);
      reviewAll.reset();
      reviewOne.mutate(name, mutateOpts);
    },
    startAll: () => {
      setNoOpInFlight(null);
      reviewOne.reset();
      reviewAll.mutate(undefined, mutateOpts);
    },
    // `reset()` on both, not a second piece of "dismissed" state: the error IS
    // the refusal, and clearing it is what the next start would do anyway. The
    // alert unmounts on this click and takes focus with it, so hand focus to
    // the page h1 — the landing useDeferredH1Focus uses, and the one element
    // guaranteed to be on the page.
    dismissRefusal: () => {
      reviewOne.reset();
      reviewAll.reset();
      document.querySelector<HTMLElement>('h1[tabindex="-1"]')?.focus();
    },
  };
}

/** "Waiting in the inbox" — the per-item set-aside backlog. Each row imports its
 * own folder; "Review all" imports the whole inbox. Both are unavailable while an
 * import runs (the single slot is busy) — the visible helper line below carries
 * the reason (no disabled-button `title`, per the spec §4 rule). NEITHER answer
 * a start can give lives here — the refusal alert and the no-op status line are
 * both the page's, because this section unmounts itself the moment the list
 * empties, which is the state both of them describe ({@link useInboxStart}). */
function InboxSection({
  items,
  importActive,
  inbox,
}: Readonly<{
  items: InboxItem[];
  importActive: boolean;
  inbox: InboxStart;
}> ) {
  const busy =
    importActive || inbox.allPending || inbox.oneStarting !== null;

  if (items.length === 0) return null;

  return (
    <section aria-label="Waiting in the inbox" className="flex flex-col gap-3">
      <div className="flex items-center justify-between gap-2">
        <SectionLabel>Waiting in the inbox</SectionLabel>
        <Button
          type="button"
          variant="outline"
          size="sm"
          // Two states, two attributes — the shape ImportPage's Start and
          // BankReviewPage's "Review now" already carry the measurement for. A
          // running import and the OTHER start are reasons this control cannot
          // be used at all, so they stay `disabled`; its OWN pending half would
          // disable the element on its own commit and strand keyboard focus on
          // <body>, so that half is `aria-disabled` and the click is swallowed.
          disabled={importActive || inbox.oneStarting !== null}
          aria-disabled={inbox.allPending}
          className="aria-disabled:opacity-50"
          // This button keeps focus through a failed start, so the sentence
          // saying why the last press failed is what a keyboard user hears on
          // coming back to it (ImportPage's Start, BankReviewPage's "Review
          // now"). It is also the return trip the page-level alert cannot
          // offer: it names no control and links to none.
          aria-describedby={
            inbox.refusal !== null ? INBOX_REFUSAL_ID : undefined
          }
          onClick={() => {
            if (busy) return;
            inbox.startAll();
          }}
        >
          {inbox.allPending ? "Starting…" : "Review all"}
        </Button>
      </div>
      <ul className="border-border divide-border divide-y overflow-hidden rounded-xl border">
        {items.map((item) => {
          const starting = inbox.oneStarting === item.name;
          const subtitle = inboxSubtitle(item);
          const label = inboxReviewLabel(starting, item.in_flight);
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
                    // Same split as "Review all" above: everything that makes
                    // this row unusable stays `disabled`, and only THIS row's
                    // own in-flight start is `aria-disabled`, so the button the
                    // user pressed keeps focus through the answer.
                    disabled={
                      importActive ||
                      inbox.allPending ||
                      (inbox.oneStarting !== null && !starting)
                    }
                    aria-disabled={starting}
                    className="aria-disabled:opacity-50"
                    // The row name, as the bank rows two sections up already do
                    // (`Ignore ${title}`): without it a screen-reader user hears
                    // a refusal naming a folder and then a list of buttons all
                    // called "Review". The visible label leads, so the
                    // accessible name still CONTAINS it (2.5.3 Label in Name).
                    aria-label={`${label} ${item.name}`}
                    aria-describedby={
                      inbox.refusal !== null ? INBOX_REFUSAL_ID : undefined
                    }
                    onClick={() => {
                      if (busy) return;
                      inbox.startOne(item.name);
                    }}
                  >
                    {label}
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
  // One route stops every import; the sweep's own word for it stays "pause",
  // because incremental history makes sweeping the same folder again a resume.
  const pause = useStopImport(jobId);
  return (
    <StatusBanner
      tone="neutral"
      action={
        <div className="flex shrink-0 items-center gap-2">
          <Button
            type="button"
            variant="outline"
            size="sm"
            aria-disabled={pause.isPending || sweep.stopped}
            // The app's recipe for an aria-disabled control: `disabled:` never
            // matches one, so without this the button swallows its click while
            // still looking pressable. This was the one site of the twenty that
            // had the posture and not the dimming.
            className="aria-disabled:opacity-50"
            onClick={() => {
              // In flight or already requested → swallow the re-click instead
              // of disabling (a mid-flight disable strands keyboard focus on
              // <body> — the Pagination posture). Pause is an idempotent 204
              // server-side, so a slipped repeat is harmless anyway.
              if (pause.isPending || sweep.stopped) return;
              pause.mutate();
            }}
          >
            <Pause aria-hidden="true" />
            {pause.isPending || sweep.stopped ? "Pausing…" : "Pause"}
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
          {sweep.current_folder && !sweep.stopped && (
            <span className="text-muted-foreground font-normal">
              {" "}Now: {lastSegment(sweep.current_folder)}
            </span>
          )}
          {sweep.stopped && (
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
        {recap.stopped ? "Sweep paused" : "Sweep finished"}: {recap.processed}{" "}
        processed · {recap.auto_applied} imported · {recap.banked} banked
        {recap.skipped_known > 0 ? ` · ${recap.skipped_known} already known` : ""}.
        {recap.stopped && (
          <span className="text-muted-foreground font-normal">
            {" "}
            Resume by sweeping the same folder again.
          </span>
        )}
      </p>
    </StatusBanner>
  );
}
