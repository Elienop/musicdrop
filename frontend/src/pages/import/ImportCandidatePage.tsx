import { useEffect, useState } from "react";
import { useLocation, useNavigate, useParams, useSearchParams } from "react-router";

import type { Candidate, ImportSearch } from "@/api/useImport";
import {
  importCoverUrl,
  useImportCandidate,
  useImportDuplicates,
  useSubmitChoice,
} from "@/api/useImport";
import { albumOriginFromState, BackLink } from "@/components/albums/album-grid";
import { Info, Spinner, Success } from "@/components/icons";
import { AlreadyInLibrary } from "@/components/import/AlreadyInLibrary";
import { CandidateReview } from "@/components/import/CandidateReview";
import { ReleaseSearchPanel } from "@/components/import/ReleaseSearchPanel";
import { EmptyState } from "@/components/system/EmptyState";
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
  const { data, isPending, isError, refetch } = useImportCandidate(
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
    // A 404 here means the album is no longer parked (already decided / the
    // worker advanced). Treat it as "return to the feed", not a hard error.
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
      <ReviewScreen
        candidate={data}
        jobId={jobId as string}
        index={index}
        backTo={backTo}
        searching={searching !== null}
        onSearchStart={(baseline) => setSearching({ baseline })}
        onSearchError={() => setSearching(null)}
      />
    </Shell>
  );
}

/** Page chrome: the up-link to wherever the user came from. */
function Shell({
  backTo,
  backLabel,
  children,
}: {
  backTo: string;
  backLabel: string;
  children: React.ReactNode;
}) {
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
  searching,
  onSearchStart,
  onSearchError,
}: {
  candidate: Candidate;
  jobId: string;
  index: number;
  backTo: string;
  searching: boolean;
  onSearchStart: (baseline: number) => void;
  onSearchError: () => void;
}) {
  const navigate = useNavigate();
  // The candidate index the user will Apply — defaults to the top match (0).
  const [selected, setSelected] = useState(0);
  // Two independent choice mutations on the same album: `submit` drives the
  // no-revision-bump relookups (search / rescan); `applySubmit` drives the
  // terminal decide (apply / skip / as-is). Keeping the decide mutation here —
  // not inside ReviewActions — lets the relookup controls see when an Apply is
  // in flight and lock out, closing the concurrent-action window on this
  // no-undo endpoint.
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
        },
      },
      { onSuccess: () => navigate(backTo) },
    );
  }

  // A relookup (search / rescan) is busy while it's in flight OR while an Apply
  // is — both mutate the same parked album, so they lock each other out.
  const relookupBusy = searching || submit.isPending || applySubmit.isPending;

  return (
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
          blurb="This album matches one you already have. Applying will ask you to resolve it — Skip new, Keep both, Replace, or Merge — with a per-track comparison."
        />
      )}
      <ReleaseSearchPanel
        onSearch={runSearch}
        busy={relookupBusy}
        feedback={candidate.search_feedback ?? null}
        error={submit.isError}
      />
      <div className="flex items-center gap-3">
        <Button
          variant="outline"
          size="sm"
          disabled={relookupBusy}
          onClick={runRescan}
        >
          Rescan folder
        </Button>
        <p className="text-muted-foreground text-xs">
          Changed the files on disk? Re-reads the folder and matches it again.
        </p>
      </div>
      <ReviewActions
        onDecide={decide}
        applyPending={applySubmit.isPending}
        applyError={applySubmit.isError}
        // Apply is blocked while a search/rescan is running too, keeping the
        // mutual exclusion symmetric.
        disabled={searching || submit.isPending}
      />
    </div>
  );
}

/** The beets choose_match actions, in a sticky bottom bar. Presentational: the
 * decide mutation lives in ReviewScreen so the relookup controls can lock out
 * while an Apply is in flight. `disabled` covers a search/rescan running;
 * `applyPending` covers the Apply itself. */
function ReviewActions({
  onDecide,
  applyPending,
  applyError,
  disabled,
}: {
  onDecide: (action: "apply" | "skip" | "asis" | "astracks") => void;
  applyPending: boolean;
  applyError: boolean;
  disabled: boolean;
}) {
  const busy = applyPending || disabled;
  return (
    <div className="bg-background/80 sticky bottom-0 z-10 -mx-2 flex flex-col gap-1.5 border-t px-2 py-3 backdrop-blur">
      {/* useSubmitChoice swallows 404/409 (already-advanced, navigates anyway);
          a genuine transport error surfaces here instead of silently re-enabling
          the button. */}
      {applyError && (
        <p className="text-destructive text-sm" role="alert">
          Couldn&rsquo;t submit that choice — try again.
        </p>
      )}
      <div className="flex flex-wrap items-center gap-2">
        <Button
          variant="ghost"
          size="sm"
          disabled={busy}
          onClick={() => onDecide("skip")}
        >
          Skip
        </Button>
        <Button
          variant="outline"
          size="sm"
          disabled={busy}
          aria-describedby="review-actions-hint"
          onClick={() => onDecide("asis")}
        >
          Use as-is
        </Button>
        {AS_TRACKS_ENABLED && (
          <Button
            variant="outline"
            size="sm"
            disabled={busy}
            aria-describedby="review-actions-hint"
            onClick={() => onDecide("astracks")}
          >
            As tracks
          </Button>
        )}
        <Button className="ml-auto" disabled={busy} onClick={() => onDecide("apply")}>
          {applyPending ? (
            <>
              <Spinner className="animate-spin" aria-hidden="true" /> Applying…
            </>
          ) : (
            <>
              <Success aria-hidden="true" /> Apply
            </>
          )}
        </Button>
      </div>
      <p id="review-actions-hint" className="text-muted-foreground text-xs">
        Use as-is imports with your current tags — no MusicBrainz match is
        applied.
        {AS_TRACKS_ENABLED &&
          " As tracks imports each file as a standalone track, not grouped as an album."}
      </p>
    </div>
  );
}

function Notice({
  title,
  body,
  onRetry,
}: {
  title: string;
  body: string;
  onRetry?: () => void;
}) {
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
