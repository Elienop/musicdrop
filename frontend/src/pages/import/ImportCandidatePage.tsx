import { useEffect, useState } from "react";
import { useLocation, useNavigate, useParams, useSearchParams } from "react-router";

import type { Candidate, ImportSearch } from "@/api/useImport";
import {
  CandidateNotFoundError,
  importCoverUrl,
  useImportCandidate,
  useImportDuplicates,
  useSubmitChoice,
} from "@/api/useImport";
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
import { useDeferredH1Focus } from "@/lib/useDeferredH1Focus";

// As-tracks (singleton import) is a silent no-op until Slice B builds real
// per-track import, so hide it rather than offer a button that does nothing.
const AS_TRACKS_ENABLED = false;

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
  // Cold-load focus repair (see useDeferredH1Focus). The !enabled and error
  // notices render no h1, so the hook is a quiet no-op there.
  useDeferredH1Focus(!isPending && !isError);

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
  if (isPending) {
    return (
      <Shell backTo={backTo} backLabel={backLabel}>
        <CandidateSkeleton />
      </Shell>
    );
  }
  if (isError) {
    // A 404 (CandidateNotFoundError) means the album is no longer parked
    // (already decided / the worker advanced). Treat it as "return to the feed"
    // with the calm notice. ANY other error is transient (5xx / network blip;
    // `retry: false` means one is enough) — surface a retryable error instead of
    // wrongly telling the user their still-parked decision is gone.
    if (error instanceof CandidateNotFoundError) {
      return (
        <Shell backTo={backTo} backLabel={backLabel}>
          <Notice
            title="This album isn’t waiting for review"
            body="It may already be decided. Head back to see what's pending."
            onRetry={() => void refetch()}
          />
        </Shell>
      );
    }
    return (
      <Shell backTo={backTo} backLabel={backLabel}>
        <ErrorState
          message="Couldn’t load this album for review. The library didn’t respond. Check the backend and try again."
          onRetry={() => void refetch()}
        />
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
      searching={searching !== null}
      onSearchStart={(baseline) => setSearching({ baseline })}
      onSearchError={() => setSearching(null)}
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
  searching,
  onSearchStart,
  onSearchError,
}: Readonly<{
  candidate: Candidate;
  jobId: string;
  index: number;
  backTo: string;
  backLabel: string;
  searching: boolean;
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
      { onSuccess: () => navigate(backTo) },
    );
  }

  // A relookup (search / rescan) is busy while it's in flight OR while an Apply
  // is — both mutate the same parked album, so they lock each other out.
  const relookupBusy = searching || submit.isPending || applySubmit.isPending;
  // The decision buttons + primary Apply lock out while ANY choice is in flight
  // (a search/rescan or an Apply itself) — symmetric mutual exclusion on this
  // no-undo endpoint.
  const busy = applySubmit.isPending || searching || submit.isPending;

  return (
    <Shell backTo={backTo} backLabel={backLabel}>
      <div className="flex flex-col gap-6">
        <CandidateReview
          candidate={candidate}
          nowCoverUrl={candidate.has_current_art ? importCoverUrl(jobId, index) : null}
          selected={selected}
          onSelect={setSelected}
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
            },
            {
              key: "asis",
              label: "Use as-is",
              variant: "secondary",
              hinted: true,
              onClick: () => decide("asis"),
              disabled: busy,
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
            pending: applySubmit.isPending,
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
            applySubmit.isError ? (
              <p className="text-destructive text-sm" role="alert">
                Couldn’t submit that choice. Try again.
              </p>
            ) : null
          }
        />
      </div>
    </Shell>
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
