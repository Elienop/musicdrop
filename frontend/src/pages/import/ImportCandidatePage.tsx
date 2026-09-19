import { useCallback, useEffect, useState } from "react";
import { useLocation, useNavigate, useParams, useSearchParams } from "react-router";

import type { Candidate, ImportSearch } from "@/api/useImport";
import {
  APPLY_NEXT_QUESTION_MS,
  CandidateNotFoundError,
  holdExit,
  importCoverUrl,
  ImportJobNotFoundError,
  postApplyStep,
  useImportCandidate,
  useImportDuplicates,
  useImportJob,
  useSubmitChoice,
} from "@/api/useImport";
import type { AlbumOrigin } from "@/components/albums/album-grid";
import { albumOriginFromState, BackLink } from "@/components/albums/album-grid";
import { Info } from "@/components/icons";
import { AlreadyInLibrary } from "@/components/import/AlreadyInLibrary";
import { CandidateReview } from "@/components/import/CandidateReview";
import { ReviewControlBar } from "@/components/import/ReviewControlBar";
import { EmptyState } from "@/components/system/EmptyState";
import { ErrorState } from "@/components/system/ErrorState";
import { PageSkeleton } from "@/components/system/PageSkeleton";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";
import { useDeferredH1Focus } from "@/lib/useDeferredH1Focus";

// As-tracks (singleton import) is a silent no-op until Slice B builds real
// per-track import, so hide it rather than offer a button that does nothing.
const AS_TRACKS_ENABLED = false;

/** What the screen is doing between a landing decision and leaving.
 *
 * `holding.since` is `Date.now()` at the 204: it both arms the bound and tells
 * a feed fetched AFTER the decision from the pre-decision snapshot the list
 * page left in the query cache. `reopened` is the stale-submit arm — the album
 * re-parked for review, so the screen unlocks in place and says so.
 *
 * `reopened.rereading` is that arm's own re-read of the candidate, and the ONLY
 * window in which a failed load is hidden: the screen is still showing the OLD
 * match, so it stays locked and silent until the new one lands. */
type HoldPhase =
  | { kind: "idle" }
  | { kind: "holding"; since: number }
  | { kind: "reopened"; rereading: boolean };

export function ImportCandidatePage() {
  const { index: indexParam } = useParams<{ index: string }>();
  const [searchParams] = useSearchParams();
  const jobId = searchParams.get("job") ?? undefined;
  const index = Number(indexParam);
  const validIndex = Number.isInteger(index) && index >= 0;

  // Where this decision screen was entered from (Review threads
  // {label:'Review', to:'/review'}; the import feed threads its run URL). A
  // deep link with no state falls back to the job's feed.
  const origin = albumOriginFromState(useLocation().state);
  const backTo = origin?.to ?? (jobId ? `/import?job=${jobId}` : "/import");
  const backLabel = origin?.label ?? "Import";

  // Without a job id (deep link lost the query) or a bad index, there's nothing
  // to fetch — send the user back.
  const enabled = Boolean(jobId) && validIndex;
  // While a release search is in flight we hold the baseline search_revision and
  // poll the candidate; the worker re-parks with a bumped revision when it lands.
  const [searching, setSearching] = useState<{ baseline: number } | null>(null);
  // beets asks the duplicate question right after the match, so a landing
  // choice holds the screen and reads the job feed for it instead of dropping
  // the user on the list to go and find it. Held HERE, above the candidate
  // query's error branches: the choice's own onSettled invalidation re-reads an
  // album the worker has moved past, and that 404 must not unmount the wait.
  const [phase, setPhase] = useState<HoldPhase>({ kind: "idle" });
  const { data, error, isPending, isError, refetch } = useImportCandidate(
    jobId ?? "",
    // `validIndex ? index : 0` keeps the (disabled) query key out of NaN when the
    // index is invalid; the query never fires anyway since `enabled` is false.
    validIndex ? index : 0,
    enabled,
    searching ? 700 : false,
  );
  useEffect(() => {
    if (searching && data && data.search_revision > searching.baseline) {
      setSearching(null);
    }
  }, [searching, data]);
  // If the candidate 404s while a search/rescan is in flight (the album was
  // decided from another tab, or the job died mid-search), no revision bump ever
  // arrives — clear `searching` so the 700ms poll stops (refetchInterval -> false)
  // and the panel unfreezes, instead of hammering the 404 under a stale notice.
  useEffect(() => {
    if (searching && error instanceof CandidateNotFoundError) {
      setSearching(null);
    }
  }, [searching, error]);
  // The re-park re-read is in flight: hide the failure notice and keep the
  // screen locked until it lands (see HoldPhase).
  const rereading = phase.kind === "reopened" && phase.rereading;
  // Cold-load focus repair (see useDeferredH1Focus). The !enabled and error
  // notices render no h1, so the hook is a quiet no-op there.
  useDeferredH1Focus(!isPending && !isError);

  // The album re-parked for review (a stale submit). Release the screen AND
  // re-read the candidate: the choice's own invalidation already fired one GET,
  // and when that landed before the worker re-parked it 404'd, leaving nothing
  // to render but the not-found notice. Nothing else refetches this query — it
  // polls only while `searching`.
  const onReopened = useCallback(() => {
    setPhase({ kind: "reopened", rereading: true });
    void refetch().finally(() =>
      setPhase((p) => (p.kind === "reopened" ? { ...p, rereading: false } : p)),
    );
  }, [refetch]);
  const onDecided = useCallback(
    () => setPhase({ kind: "holding", since: Date.now() }),
    [],
  );

  if (!enabled) {
    return (
      <Shell backTo={backTo} backLabel={backLabel}>
        <Notice
          title="Nothing to review"
          body="This review link is missing its import job. Go back to the import."
        />
      </Shell>
    );
  }
  if (data === undefined) {
    // Nothing has ever loaded: the skeleton while the first fetch runs, the
    // failure notice once it errors.
    return (
      <Shell backTo={backTo} backLabel={backLabel}>
        {isPending ? <CandidateSkeleton /> : <LoadFailure error={error} onRetry={refetch} />}
      </Shell>
    );
  }
  // A refetch failed on an album that HAS rendered. Two states keep the screen
  // instead of swapping in the notice: a hold, where the job feed is what
  // routes; and the re-park arm's own re-read, which releases onto the 404 the
  // choice's own invalidation just fetched.
  //
  // NOT `!isFetching`: every other refetch of an errored candidate (the search
  // poll, the notice's own Try again, a window-focus read after staleTime)
  // would swap the notice for the stale screen for one round trip — measured at
  // 4 transitions in 2.2s under a 700ms poll.
  if (isError && phase.kind !== "holding" && !rereading) {
    return (
      <Shell backTo={backTo} backLabel={backLabel}>
        <LoadFailure error={error} onRetry={refetch} />
      </Shell>
    );
  }

  return (
    <ReviewScreen
      candidate={data}
      jobId={jobId as string}
      index={index}
      backTo={backTo}
      backLabel={backLabel}
      origin={origin}
      searching={searching !== null}
      phase={phase}
      onDecided={onDecided}
      onReopened={onReopened}
      onSearchStart={(baseline) => {
        setSearching({ baseline });
        // A new relookup makes "Match updated. Choose again." stale — it
        // described the PREVIOUS re-park.
        setPhase({ kind: "idle" });
      }}
      onSearchError={() => setSearching(null)}
    />
  );
}

/** The two ways loading this album can fail.
 *
 * A 404 (CandidateNotFoundError) means the album is no longer parked (already
 * decided / the worker advanced): the calm notice, not an error. ANY other
 * error is transient (5xx / network blip; `retry: false` means one is enough)
 * — surface a retryable error instead of wrongly telling the user their
 * still-parked decision is gone. */
function LoadFailure({
  error,
  onRetry,
}: Readonly<{ error: unknown; onRetry: () => unknown }>) {
  if (error instanceof CandidateNotFoundError) {
    return (
      <Notice
        title="This album isn’t waiting for review"
        body="It may already be decided. Head back to see what's pending."
        onRetry={() => void onRetry()}
      />
    );
  }
  return (
    <ErrorState
      message="Couldn’t load this album for review. The library didn’t respond. Check the backend and try again."
      onRetry={() => void onRetry()}
    />
  );
}

/** Page chrome: the up-link to wherever the user came from. */
function Shell({
  backTo,
  backLabel,
  children,
}: Readonly<{
  backTo: string;
  backLabel: string;
  children: React.ReactNode;
}> ) {
  return (
    <section className="flex flex-col gap-6" aria-label="Review album">
      <BackLink to={backTo} label={backLabel} />
      {children}
    </section>
  );
}

function ReviewScreen({
  candidate,
  jobId,
  index,
  backTo,
  backLabel,
  origin,
  searching,
  phase,
  onDecided,
  onReopened,
  onSearchStart,
  onSearchError,
}: Readonly<{
  candidate: Candidate;
  jobId: string;
  index: number;
  backTo: string;
  backLabel: string;
  origin: AlbumOrigin | undefined;
  searching: boolean;
  phase: HoldPhase;
  onDecided: () => void;
  onReopened: () => void;
  onSearchStart: (baseline: number) => void;
  onSearchError: () => void;
}> ) {
  const navigate = useNavigate();
  // The candidate index the user will Apply — defaults to the top match (0).
  const [selected, setSelected] = useState(0);
  // Two independent choice mutations on the same album: `submit` drives the
  // no-revision-bump relookups (search / rescan); `applySubmit` drives the
  // terminal decide (apply / skip / as-is). Keeping both here lets the relookup
  // controls in the bar see when an Apply is in flight and lock out, closing the
  // concurrent-action window on this no-undo endpoint.
  const submit = useSubmitChoice(jobId);
  const applySubmit = useSubmitChoice(jobId);
  // A landed re-lookup resets the chosen option back to the new top match.
  useEffect(() => setSelected(0), [candidate.search_revision]);
  const dups = useImportDuplicates(jobId, index, selected, candidate.search_revision);
  const existing = dups.data?.existing ?? [];
  // The up-front check has ANSWERED, and its answer is "nothing in the library
  // matches this release" — the common case, where a landing choice returns to
  // the list on the 204 exactly as it did before. Pending or errored does not
  // count: an unanswered check is not a "no".
  //
  // This gate decides WHETHER to hold, never WHERE to go. When the hold runs,
  // the feed row routes (see `postApplyStep`). When this reads "none" and beets
  // then asks a duplicate question anyway, the user is on the list with a
  // Resolve row for the album (ReviewPage's DecisionSection renders one for a
  // needs_dup_resolution row) — the behaviour that shipped before this screen
  // learned to wait, not a worse one.
  const noCollisionPredicted = dups.isSuccess && dups.data.existing.length === 0;

  // The job feed, polled only while a landing choice waits for its next
  // question. `useImportJob(undefined)` is a disabled query, so the rest of the
  // time this screen costs no requests.
  const waiting = phase.kind === "holding";
  const decidedAt = phase.kind === "holding" ? phase.since : null;
  const job = useImportJob(waiting ? jobId : undefined);
  // Read only a feed fetched AFTER the choice landed. The list page the user
  // came through leaves its own poll in the cache, and that snapshot still has
  // this row parked for review — acting on it would cancel the wait at once.
  const feed =
    decidedAt !== null && job.data !== undefined && job.dataUpdatedAt >= decidedAt
      ? job.data
      : null;
  const step = feed === null ? "wait" : postApplyStep(feed, index);
  const duplicateTo = `/import/albums/${index}/duplicate?job=${jobId}`;
  // Only a job that is GONE ends the hold early, and 404 is the one answer
  // that says so: `useImportJob` raises this class for it and stops its loop,
  // while it keeps polling through every other error because they may
  // self-heal. A 5xx therefore rides the loop and the bound covers it.
  //
  // NOT `isError && job.data === undefined`: a hold starts with this query
  // disabled and empty, so that reads true on the FIRST error of every hold —
  // exactly the transient this is meant to survive.
  const feedGone = job.error instanceof ImportJobNotFoundError;
  // The bound, as state rather than a second `navigate`: one effect owns every
  // exit, so the arms cannot race (a duplicate committed in the last scheduling
  // gap before the deadline still outranks it — see `holdExit`).
  const [expired, setExpired] = useState(false);
  useEffect(() => {
    setExpired(false);
    if (decidedAt === null) return undefined;
    const timer = setTimeout(() => setExpired(true), APPLY_NEXT_QUESTION_MS);
    return () => clearTimeout(timer);
  }, [decidedAt]);

  const exit = holdExit({ step, feedGone, expired });
  useEffect(() => {
    if (!waiting) return;
    if (exit === "leave") {
      navigate(backTo, { replace: true });
      return;
    }
    if (exit === "duplicate") {
      // The same router state the Review list threads (`{from: {label, to}}`),
      // so the duplicate screen's back link still reads Review / the run.
      navigate(duplicateTo, {
        replace: true,
        state: origin === undefined ? undefined : { from: origin },
      });
      return;
    }
    // The worker re-parked this album for review (it read the submit as stale).
    if (exit === "stay") onReopened();
  }, [waiting, exit, navigate, backTo, duplicateTo, origin, onReopened]);

  function runSearch(search: ImportSearch) {
    onSearchStart(candidate.search_revision);
    submit.mutate(
      { index, choice: { action: "search", candidate_index: null, search } },
      // A failed POST never bumps search_revision, so clear the searching state
      // here or the panel + actions stay frozen forever (the error banner shows
      // but every retry control is disabled).
      { onError: () => onSearchError() },
    );
  }

  // Re-scan re-reads the folder from disk + re-runs beets' default lookup (no
  // search terms); the worker re-parks with a bumped search_revision, which
  // ends the searching state via the same effect a search uses.
  function runRescan() {
    onSearchStart(candidate.search_revision);
    submit.mutate(
      { index, choice: { action: "rescan", candidate_index: null } },
      { onError: () => onSearchError() },
    );
  }

  function decide(action: "apply" | "skip" | "asis" | "astracks") {
    applySubmit.mutate(
      {
        index,
        choice: {
          action,
          candidate_index: action === "apply" ? selected : null,
          // Apply echoes the RENDERED candidate's search_revision so the worker
          // can spot a stale submit (one racing a search re-park from another
          // tab) and re-park instead of importing a release the user never
          // chose. Non-apply actions are list-independent — no echo.
          ...(action === "apply"
            ? { search_revision: candidate.search_revision }
            : {}),
        },
      },
      {
        onSuccess: () => {
          // apply and as-is both hand the album to beets to land, and beets
          // asks the duplicate question for either (the prompt's "new" side is
          // built from the matched release OR the current files). Hold the
          // screen for that answer — unless the library check has already
          // answered that there is nothing to collide with. A skip never
          // reaches the question.
          //
          // `submitChoice` swallows 404/409 as "no longer awaiting this album",
          // so a choice another tab already answered also lands here and holds.
          // The feed then routes it like any other row: to that tab's duplicate
          // prompt if one is parked, otherwise back to the list.
          const lands = action === "apply" || action === "asis";
          if (lands && !noCollisionPredicted) {
            onDecided();
            return;
          }
          navigate(backTo, { replace: true });
        },
      },
    );
  }

  // A relookup (search / rescan) is busy while it's in flight OR while an Apply
  // is — both mutate the same parked album, so they lock each other out. The
  // wait that follows a landed choice counts too: the album is beets' now.
  const rereading = phase.kind === "reopened" && phase.rereading;
  const relookupBusy =
    searching || submit.isPending || applySubmit.isPending || waiting || rereading;
  // The decision buttons + primary Apply lock out while ANY choice is in flight
  // (a search/rescan or an Apply itself) — symmetric mutual exclusion on this
  // no-undo endpoint.
  // `rereading`: the re-park re-read is in flight and the OLD match is still on
  // screen — an Apply here submits a release the worker has already replaced.
  const busy =
    applySubmit.isPending || searching || submit.isPending || waiting || rereading;
  // Which action is in flight / being held for, so the PRESSED control is the
  // one wearing the pending posture. Read off the mutation's own last variables
  // rather than a second piece of state — and gated on THAT mutation's life,
  // not on `busy`: `busy` folds in the relookup mutation, and @tanstack/query
  // keeps `variables` after a settle (the reducer spreads `...state` on error),
  // so the union re-wore a failed Apply's "Applying…" during a later Rescan.
  const inFlight =
    applySubmit.isPending || waiting ? applySubmit.variables?.choice.action : undefined;
  const pendingAction = (action: string) => inFlight === action;


  return (
    <Shell backTo={backTo} backLabel={backLabel}>
      <div className="flex flex-col gap-6">
        <CandidateReview
          candidate={candidate}
          nowCoverUrl={candidate.has_current_art ? importCoverUrl(jobId, index) : null}
          selected={selected}
          onSelect={setSelected}
          // The release switcher is a decision control: changing it re-runs the
          // library check and re-words the status line while the POST already
          // carries the click-time candidate_index.
          disabled={busy}
        />
        {existing.length > 0 && (
          <AlreadyInLibrary
            existing={existing}
            blurb="This album matches one you already have. Applying will ask you to resolve it: Skip new, Keep both, Replace, or Merge, with a per-track comparison."
          />
        )}
        <ReviewControlBar
          decisions={[
            {
              key: "skip",
              label: "Skip",
              variant: "ghost",
              onClick: () => decide("skip"),
              disabled: busy,
              pending: pendingAction("skip"),
              pendingLabel: "Skipping…",
            },
            {
              key: "asis",
              label: "Use as-is",
              variant: "secondary",
              hinted: true,
              onClick: () => decide("asis"),
              disabled: busy,
              pending: pendingAction("asis"),
              pendingLabel: "Using as-is…",
            },
            ...(AS_TRACKS_ENABLED
              ? [
                  {
                    key: "astracks",
                    label: "As tracks",
                    variant: "secondary" as const,
                    hinted: true,
                    onClick: () => decide("astracks"),
                    disabled: busy,
                  },
                ]
              : []),
          ]}
          primary={{
            label: "Apply",
            pendingLabel: "Applying…",
            // Only when APPLY is the action in flight: `applySubmit` is shared
            // by every decision, so an unqualified `isPending` spun this button
            // for a Use as-is the user pressed elsewhere.
            pending: pendingAction("apply"),
            icon: true,
            onClick: () => decide("apply"),
            disabled: busy,
          }}
          rescan={{ onClick: runRescan, pending: false, disabled: relookupBusy }}
          search={{
            onSearch: runSearch,
            busy: relookupBusy,
            feedback: candidate.search_feedback ?? null,
            error: submit.isError,
          }}
          hint={
            "Use as-is keeps your current tags; no MusicBrainz match is applied." +
            (AS_TRACKS_ENABLED ? " As tracks imports files individually." : "")
          }
          messages={
            <BarMessages
              status={statusLine(phase, existing.length > 0, applySubmit.isPending)}
              failed={applySubmit.isError}
            />
          }
        />
      </div>
    </Shell>
  );
}

/** The bar's one line, or "" when the screen has nothing to say.
 *
 * `predictedCollision` (the up-front library check) is allowed to WORD the
 * hold — never to decide where the user goes; the feed row does that. */
function statusLine(
  phase: HoldPhase,
  predictedCollision: boolean,
  submitting: boolean,
): string {
  if (phase.kind === "holding") {
    return predictedCollision
      ? "Loading the duplicate question…"
      : "Checking for a duplicate…";
  }
  // Only once the new match is on screen: during the re-read the screen still
  // shows the OLD one, so "updated" would name something not yet there.
  if (phase.kind === "reopened" && !phase.rereading && !submitting) {
    return "Match updated. Choose again.";
  }
  return "";
}

/** The control bar's message slot: one live region, mounted from the first
 * render and only ever swapping its TEXT — a region that APPEARS already
 * holding its sentence is not reliably announced (the RouteAnnouncer shape).
 * `sr-only` while empty keeps it out of the layout. */
function BarMessages({
  status,
  failed,
}: Readonly<{ status: string; failed: boolean }>) {
  return (
    <>
      <output
        className={cn(
          "text-sm block",
          status === "" ? "sr-only" : "text-muted-foreground",
        )}
      >
        {status}
      </output>
      {failed && (
        <p className="text-destructive text-sm" role="alert">
          Couldn’t submit that choice. Try again.
        </p>
      )}
    </>
  );
}

function Notice({
  title,
  body,
  onRetry,
}: Readonly<{
  title: string;
  body: string;
  onRetry?: () => void;
}> ) {
  return (
    <EmptyState
      bordered
      icon={Info}
      title={title}
      body={body}
      action={
        onRetry && (
          <Button variant="outline" size="sm" onClick={onRetry}>
            Try again
          </Button>
        )
      }
    />
  );
}

function CandidateSkeleton() {
  return (
    <PageSkeleton announce="Loading the proposed match…">
      <div className="flex flex-col gap-6">
        <div className="flex flex-col gap-2">
          <Skeleton className="h-7 w-2/3" />
          <Skeleton className="h-4 w-1/2" />
        </div>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <Skeleton className="h-64 rounded-xl" />
          <Skeleton className="h-64 rounded-xl" />
        </div>
      </div>
    </PageSkeleton>
  );
}
