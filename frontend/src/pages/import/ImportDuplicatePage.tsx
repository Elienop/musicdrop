import { AlertCircle, CheckCheck, Copy, Loader2, Music, Replace, X } from "lucide-react";
import { useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router";

import type { DuplicateAction, DuplicatePrompt } from "@/api/useImport";
import {
  importCoverUrl,
  useDuplicatePrompt,
  useResolveImportDuplicate,
} from "@/api/useImport";
import { BackLink } from "@/components/albums/album-grid";
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

  // Without a job id (deep link lost the query) or a bad index there's nothing
  // to fetch — send the user back to the import feed.
  const enabled = Boolean(jobId) && validIndex;
  const { data, isPending, isError, refetch } = useDuplicatePrompt(
    jobId ?? "",
    // `validIndex ? index : 0` keeps the (disabled) query key out of NaN when the
    // index is invalid; the query never fires anyway since `enabled` is false.
    validIndex ? index : 0,
    enabled,
  );

  const backTo = jobId ? `/import?job=${jobId}` : "/import";

  if (!enabled) {
    return (
      <Shell backTo={backTo}>
        <Notice
          title="Nothing to resolve"
          body="This link is missing its import job. Go back to the import."
        />
      </Shell>
    );
  }
  if (isPending) {
    return (
      <Shell backTo={backTo}>
        <DuplicateSkeleton />
      </Shell>
    );
  }
  if (isError) {
    // A 404 here means the album is no longer parked (already resolved / the
    // worker advanced). Treat it as "return to the feed", not a hard error.
    return (
      <Shell backTo={backTo}>
        <Notice
          title="This album isn’t waiting for a decision"
          body="It may already be resolved. Head back to the import to see the feed."
          onRetry={() => void refetch()}
        />
      </Shell>
    );
  }

  return (
    <Shell backTo={backTo}>
      <DuplicateScreen
        prompt={data}
        jobId={jobId as string}
        index={index}
        backTo={backTo}
      />
    </Shell>
  );
}

/** Page chrome: the up-link to the feed. */
function Shell({ backTo, children }: { backTo: string; children: React.ReactNode }) {
  return (
    <section className="flex flex-col gap-6" aria-label="Resolve duplicate">
      <BackLink to={backTo} label="Import" />
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
        <h2 className="text-2xl font-semibold tracking-tight">
          Already in your library
        </h2>
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
          <ActionIcon action="skip_new" pending={pending} icon={X} /> Skip new
        </Button>
        <Button
          variant="outline"
          size="sm"
          disabled={resolve.isPending}
          title="Import alongside the existing copy"
          onClick={() => decide("keep_both")}
        >
          <ActionIcon action="keep_both" pending={pending} icon={Copy} /> Keep both
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
          <ActionIcon action="replace" pending={pending} icon={Replace} /> Replace old
        </Button>
        <Button
          className="ml-auto"
          size="sm"
          disabled={resolve.isPending}
          title="Combine into one album, then review the merged result"
          onClick={() => decide("merge")}
        >
          <ActionIcon action="merge" pending={pending} icon={CheckCheck} />
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
  icon: typeof Loader2;
}) {
  if (pending === action) {
    return <Loader2 className="animate-spin" aria-hidden="true" />;
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
      <Cover url={coverUrl} />
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

/** Serve-or-degrade cover (mirrors ImportCandidatePage.Cover). */
function Cover({ url }: { url: string | null }) {
  const [failed, setFailed] = useState(false);
  if (url === null || failed) {
    return (
      <div
        className="bg-muted flex aspect-square w-full items-center justify-center rounded-lg"
        aria-hidden="true"
      >
        <Music className="text-muted-foreground size-10" />
      </div>
    );
  }
  return (
    <img
      src={url}
      alt=""
      loading="lazy"
      onError={() => setFailed(true)}
      className="bg-muted aspect-square w-full rounded-lg object-cover"
    />
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
    <div className="border-border flex flex-col items-center gap-3 rounded-xl border border-dashed py-16 text-center">
      <AlertCircle className="text-muted-foreground size-10" aria-hidden="true" />
      <div className="flex flex-col gap-1">
        <p className="font-medium">{title}</p>
        <p className="text-muted-foreground text-sm">{body}</p>
      </div>
      {onRetry && (
        <Button variant="outline" size="sm" onClick={onRetry}>
          Try again
        </Button>
      )}
    </div>
  );
}

function DuplicateSkeleton() {
  return (
    <>
      {/* role="status" must sit OUTSIDE the aria-hidden skeleton, or screen
          readers never hear the loading announcement (mirrors ImportPage). */}
      <p className="sr-only" role="status">
        Loading the duplicate…
      </p>
      <div className="flex flex-col gap-6" aria-hidden="true">
        <Skeleton className="h-7 w-2/3" />
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <Skeleton className="h-72 rounded-xl" />
          <Skeleton className="h-72 rounded-xl" />
        </div>
      </div>
    </>
  );
}
