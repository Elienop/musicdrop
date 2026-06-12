import { useState } from "react";
import { useLocation, useNavigate, useParams, useSearchParams } from "react-router";

import type { Candidate } from "@/api/useImport";
import {
  importCoverUrl,
  useImportCandidate,
  useSubmitChoice,
} from "@/api/useImport";
import { albumOriginFromState, BackLink } from "@/components/albums/album-grid";
import { Info, Spinner, Success } from "@/components/icons";
import { CandidateReview } from "@/components/import/CandidateReview";
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
  const { data, isPending, isError, refetch } = useImportCandidate(
    jobId ?? "",
    // `validIndex ? index : 0` keeps the (disabled) query key out of NaN when the
    // index is invalid; the query never fires anyway since `enabled` is false.
    validIndex ? index : 0,
    enabled,
  );
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
}: {
  candidate: Candidate;
  jobId: string;
  index: number;
  backTo: string;
}) {
  // The candidate index the user will Apply — defaults to the top match (0).
  const [selected, setSelected] = useState(0);

  return (
    <div className="flex flex-col gap-6">
      <CandidateReview
        candidate={candidate}
        nowCoverUrl={candidate.has_current_art ? importCoverUrl(jobId, index) : null}
        selected={selected}
        onSelect={setSelected}
      />
      <ReviewActions
        jobId={jobId}
        index={index}
        selected={selected}
        backTo={backTo}
      />
    </div>
  );
}

/** The beets choose_match actions, in a sticky bottom bar. Apply uses the
 * selected candidate index; on success we return to the feed (the worker
 * advances to the next album). */
function ReviewActions({
  jobId,
  index,
  selected,
  backTo,
}: {
  jobId: string;
  index: number;
  selected: number;
  backTo: string;
}) {
  const navigate = useNavigate();
  const submit = useSubmitChoice(jobId);

  function decide(action: "apply" | "skip" | "asis" | "astracks") {
    submit.mutate(
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

  return (
    <div className="bg-background/80 sticky bottom-0 z-10 -mx-2 flex flex-col gap-1.5 border-t px-2 py-3 backdrop-blur">
      {/* Picking an alternate candidate changes what Apply submits, but the diff
          above always reflects the top match — say so (there's no undo). */}
      {selected !== 0 && (
        <p className="text-muted-foreground text-sm" role="status">
          Showing the top match — Apply will import the selected release.
        </p>
      )}
      {/* useSubmitChoice swallows 404/409 (already-advanced, navigates anyway);
          a genuine transport error surfaces here instead of silently re-enabling
          the button. */}
      {submit.isError && (
        <p className="text-destructive text-sm" role="alert">
          Couldn&rsquo;t submit that choice — try again.
        </p>
      )}
      <div className="flex flex-wrap items-center gap-2">
        <Button
          variant="ghost"
          size="sm"
          disabled={submit.isPending}
          onClick={() => decide("skip")}
        >
          Skip
        </Button>
        <Button
          variant="outline"
          size="sm"
          disabled={submit.isPending}
          aria-describedby="review-actions-hint"
          onClick={() => decide("asis")}
        >
          Use as-is
        </Button>
        {AS_TRACKS_ENABLED && (
          <Button
            variant="outline"
            size="sm"
            disabled={submit.isPending}
            aria-describedby="review-actions-hint"
            onClick={() => decide("astracks")}
          >
            As tracks
          </Button>
        )}
        <Button
          className="ml-auto"
          disabled={submit.isPending}
          onClick={() => decide("apply")}
        >
          {submit.isPending ? (
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
