import { useState } from "react";
import { useLocation, useNavigate, useParams, useSearchParams } from "react-router";

import type { DuplicateAction, DuplicatePrompt } from "@/api/useImport";
import {
  importCoverUrl,
  useDuplicatePrompt,
  useResolveImportDuplicate,
} from "@/api/useImport";
import { albumOriginFromState, BackLink } from "@/components/albums/album-grid";
import {
  Close,
  Duplicates,
  Info,
  Merge as MergeIcon,
  Replace as ReplaceIcon,
  Spinner,
  type AppIcon,
} from "@/components/icons";
import { CoverArt } from "@/components/system/CoverArt";
import { EmptyState } from "@/components/system/EmptyState";
import { PageSkeleton } from "@/components/system/PageSkeleton";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";

type IncomingAlbum = DuplicatePrompt["incoming"];
type ExistingAlbum = DuplicatePrompt["existing"][number];

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
}: {
  backTo: string;
  backLabel: string;
  children: React.ReactNode;
}) {
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
}: {
  prompt: DuplicatePrompt;
  jobId: string;
  index: number;
  backTo: string;
}) {
  const navigate = useNavigate();
  const resolve = useResolveImportDuplicate(jobId);
  const [pending, setPending] = useState<DuplicateAction | null>(null);

  function decide(action: DuplicateAction) {
    setPending(action);
    resolve.mutate(
      { index, decision: { action } },
      { onSuccess: () => navigate(backTo), onError: () => setPending(null) },
    );
  }

  const incomingCover = prompt.incoming.has_current_art
    ? importCoverUrl(jobId, index)
    : null;

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-1">
        {/* THE page h1 — decision screens own their h1 directly (the Task-7
            detail-page idiom); tabIndex -1 keeps RouteAnnouncer's contract. */}
        <h1 tabIndex={-1} className="text-2xl font-bold tracking-tight">
          Already in your library
        </h1>
        <p className="text-muted-foreground text-sm">
          This album matches one you already have. Choose what to do before
          importing.
        </p>
      </div>

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <Panel
          id="incoming"
          heading="Importing (new)"
          accent
          album={prompt.incoming}
          coverUrl={incomingCover}
        />
        {prompt.existing.map((album) => (
          <Panel
            key={album.album_id}
            id={`existing-${album.album_id}`}
            heading="Already in library"
            album={album}
            coverUrl={`/api/albums/${album.album_id}/cover`}
          />
        ))}
      </div>

      {resolve.isError && (
        <p className="text-destructive text-sm" role="alert">
          Couldn&rsquo;t submit that — try again.
        </p>
      )}

      <div className="bg-background/80 sticky bottom-0 z-10 -mx-2 flex flex-wrap items-center gap-2 border-t px-2 py-3 backdrop-blur">
        <Button
          variant="ghost"
          size="sm"
          disabled={resolve.isPending}
          onClick={() => decide("skip_new")}
        >
          <ActionIcon action="skip_new" pending={pending} icon={Close} /> Skip new
        </Button>
        <Button
          variant="outline"
          size="sm"
          disabled={resolve.isPending}
          title="Import alongside the existing copy"
          onClick={() => decide("keep_both")}
        >
          <ActionIcon action="keep_both" pending={pending} icon={Duplicates} /> Keep both
        </Button>
        <Button
          variant="outline"
          size="sm"
          disabled={resolve.isPending}
          className="border-warning text-warning hover:bg-warning/10 hover:text-warning"
          title="Import the new album; move the existing copy to Trash (reversible)"
          aria-describedby="duplicate-footnote"
          onClick={() => decide("replace")}
        >
          <ActionIcon action="replace" pending={pending} icon={ReplaceIcon} /> Replace old
        </Button>
        <Button
          className="ml-auto"
          size="sm"
          disabled={resolve.isPending}
          title="Combine into one album, then review the merged result"
          onClick={() => decide("merge")}
        >
          <ActionIcon action="merge" pending={pending} icon={MergeIcon} />
          Merge
        </Button>
      </div>
      <p id="duplicate-footnote" className="text-muted-foreground text-xs">
        Replace moves the old copy to Trash (reversible) · Merge combines them,
        then reappears as a normal review.
      </p>
    </div>
  );
}

/** The clicked button shows a spinner in place of its own icon while the
 * resolution is in flight, so progress reads on the button the user pressed
 * (not only on Merge). `pending` is the action currently being submitted. */
function ActionIcon({
  action,
  pending,
  icon: Icon,
}: {
  action: DuplicateAction;
  pending: DuplicateAction | null;
  icon: AppIcon;
}) {
  if (pending === action) {
    return <Spinner className="animate-spin" aria-hidden="true" />;
  }
  return <Icon aria-hidden="true" />;
}

function Panel({
  id,
  heading,
  album,
  coverUrl,
  accent = false,
}: {
  id: string;
  heading: string;
  album: IncomingAlbum | ExistingAlbum;
  coverUrl: string | null;
  accent?: boolean;
}) {
  const headingId = `panel-heading-${id}`;
  const meta = [
    album.year?.toString() ?? null,
    `${album.track_count} ${album.track_count === 1 ? "track" : "tracks"}`,
    album.format,
    album.bitrate_kbps ? `${album.bitrate_kbps} kbps` : null,
  ].filter((b): b is string => Boolean(b));
  return (
    <section
      aria-labelledby={headingId}
      className="border-border flex flex-col gap-3 rounded-xl border p-4"
    >
      <p
        id={headingId}
        className={
          accent
            ? "text-primary text-xs font-medium tracking-wide uppercase"
            : "text-muted-foreground text-xs font-medium tracking-wide uppercase"
        }
      >
        {heading}
      </p>
      <CoverArt src={coverUrl} className="w-full rounded-lg" />
      <div className="flex flex-col gap-0.5">
        <p className="truncate font-medium">{album.album ?? "Unknown album"}</p>
        <p className="text-muted-foreground truncate text-sm">
          {album.album_artist ?? "Unknown artist"}
        </p>
        <p className="text-muted-foreground text-sm">{meta.join(" · ")}</p>
        <p
          className="text-muted-foreground truncate font-mono text-xs"
          title={album.folder}
        >
          {album.folder}
        </p>
      </div>
    </section>
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
