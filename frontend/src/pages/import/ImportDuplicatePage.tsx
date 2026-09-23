import { useState } from "react";
import { useLocation, useNavigate, useParams, useSearchParams } from "react-router";

import type { DuplicateAction, DuplicatePrompt } from "@/api/useImport";
import {
  importCoverUrl,
  useDuplicatePrompt,
  useResolveImportDuplicate,
} from "@/api/useImport";
import { albumOriginFromState, BackLink } from "@/components/albums/album-grid";
import { Info } from "@/components/icons";
import {
  DuplicateActions,
  DuplicateComparison,
} from "@/components/import/DuplicateReview";
import { EmptyState } from "@/components/system/EmptyState";
import { PageSkeleton } from "@/components/system/PageSkeleton";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useDeferredH1Focus } from "@/lib/useDeferredH1Focus";

export function ImportDuplicatePage() {
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

  // Without a job id (deep link lost the query) or a bad index there's nothing
  // to fetch — send the user back.
  const enabled = Boolean(jobId) && validIndex;
  const { data, isPending, isError, refetch } = useDuplicatePrompt(
    jobId ?? "",
    // `validIndex ? index : 0` keeps the (disabled) query key out of NaN when the
    // index is invalid; the query never fires anyway since `enabled` is false.
    validIndex ? index : 0,
    enabled,
  );
  // Cold-load focus repair (see useDeferredH1Focus).
  useDeferredH1Focus(!isPending && !isError);

  if (!enabled) {
    return (
      <Shell backTo={backTo} backLabel={backLabel}>
        <Notice
          title="Nothing to resolve"
          body="This link is missing its import job. Go back to the import."
        />
      </Shell>
    );
  }
  if (isPending) {
    return (
      <Shell backTo={backTo} backLabel={backLabel}>
        <DuplicateSkeleton />
      </Shell>
    );
  }
  if (isError) {
    // A 404 here means the album is no longer parked (already resolved / the
    // worker advanced). Treat it as "return to the feed", not a hard error.
    return (
      <Shell backTo={backTo} backLabel={backLabel}>
        <Notice
          title="This album isn’t waiting for a decision"
          body="It may already be resolved. Head back to see what's pending."
          onRetry={() => void refetch()}
        />
      </Shell>
    );
  }

  return (
    <Shell backTo={backTo} backLabel={backLabel}>
      <DuplicateScreen
        prompt={data}
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
}: Readonly<{
  backTo: string;
  backLabel: string;
  children: React.ReactNode;
}> ) {
  return (
    <section className="flex flex-col gap-6" aria-label="Resolve duplicate">
      <BackLink to={backTo} label={backLabel} />
      {children}
    </section>
  );
}

function DuplicateScreen({
  prompt,
  jobId,
  index,
  backTo,
}: Readonly<{
  prompt: DuplicatePrompt;
  jobId: string;
  index: number;
  backTo: string;
}> ) {
  const navigate = useNavigate();
  const resolve = useResolveImportDuplicate(jobId);
  const [pending, setPending] = useState<DuplicateAction | null>(null);

  function decide(action: DuplicateAction) {
    setPending(action);
    resolve.mutate(
      { index, decision: { action } },
      {
        // `replace`, like every other post-decision exit in this flow: the
        // album this screen is about is gone once the decision lands, so Back
        // must not return to it.
        onSuccess: () => navigate(backTo, { replace: true }),
        onError: () => setPending(null),
      },
    );
  }

  return (
    <div className="flex flex-col gap-6">
      <DuplicateComparison
        prompt={prompt}
        incomingCoverUrl={
          prompt.incoming.has_current_art ? importCoverUrl(jobId, index) : null
        }
        // beets asks two questions per album, the match then the duplicate
        // (beets/importer/stages.py user_query). This screen is the second one
        // for an album whose match is already decided — however the user got
        // here, from the Apply they just made or from the list's Resolve.
        eyebrow="Next question for this album"
      />
      {resolve.isError && (
        <p className="text-destructive text-sm" role="alert">
          Couldn&rsquo;t submit that. Try again.
        </p>
      )}
      <DuplicateActions pending={pending} busy={resolve.isPending} onDecide={decide} />
    </div>
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

function DuplicateSkeleton() {
  return (
    <PageSkeleton announce="Loading the duplicate…">
      <div className="flex flex-col gap-6">
        <Skeleton className="h-7 w-2/3" />
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <Skeleton className="h-72 rounded-xl" />
          <Skeleton className="h-72 rounded-xl" />
        </div>
      </div>
    </PageSkeleton>
  );
}
