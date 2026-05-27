import { AlertCircle, CircleCheck, FolderInput, Loader2 } from "lucide-react";
import { useState } from "react";
import { Link, useSearchParams } from "react-router";

import type { ImportAlbumSummary, ImportJobState } from "@/api/useImport";
import {
  ImportConflictError,
  ImportJobNotFoundError,
  RECOMMENDATION_LABEL,
  useImportJob,
  useStartImport,
} from "@/api/useImport";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { useThrottledValue } from "@/lib/useThrottledValue";
import { cn } from "@/lib/utils";
import { announceMessage } from "@/pages/import/importStatus";

export function ImportPage() {
  const [searchParams] = useSearchParams();
  const jobId = searchParams.get("job") ?? undefined;

  // No active job in the URL -> the entry screen (path + Start).
  if (!jobId) {
    return <ImportEntry />;
  }
  return <ImportRun jobId={jobId} />;
}

/** Entry: a server-path input + Start. Surfaces the 409 (already running) and
 * the blank-path guard locally; on success the URL gains `?job=<id>` and the
 * page flips to the live run. */
function ImportEntry() {
  const [, setSearchParams] = useSearchParams();
  const [path, setPath] = useState("");
  const start = useStartImport();

  const trimmed = path.trim();
  const conflict = start.error instanceof ImportConflictError;
  // A non-conflict error is a generic start failure.
  const genericError = start.isError && !conflict;

  function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (trimmed.length === 0) {
      return; // Button is disabled too; guard the Enter key.
    }
    start.mutate(
      { path: trimmed },
      {
        onSuccess: (data) => {
          setSearchParams({ job: data.job_id });
        },
      },
    );
  }

  return (
    <section className="flex max-w-2xl flex-col gap-6" aria-label="Import music">
      <div className="flex flex-col gap-1">
        <h2 className="text-2xl font-semibold tracking-tight">Import music</h2>
        <p className="text-muted-foreground text-sm">
          Point beets at a folder on the server. It scans, matches each album
          against MusicBrainz, and imports what it finds.
        </p>
      </div>

      <form className="flex flex-col gap-3" onSubmit={onSubmit}>
        <label className="flex flex-col gap-2">
          <span className="text-sm font-medium">Folder path</span>
          <Input
            type="text"
            value={path}
            onChange={(e) => setPath(e.target.value)}
            placeholder="/music/incoming"
            aria-label="Folder path"
            aria-invalid={genericError || conflict}
          />
        </label>

        {conflict && (
          // The 409 carries no job id, so there's nowhere actionable to link to
          // (the user is already on /import). Plain, honest text instead of a
          // focusable link that navigates nowhere.
          <p className="text-destructive text-sm" role="alert">
            An import is already running. Reopen it from the tab that started it,
            or wait for it to finish.
          </p>
        )}
        {genericError && (
          <p className="text-destructive text-sm" role="alert">
            Couldn&rsquo;t start the import. Check the path and the backend, then
            try again.
          </p>
        )}

        <div>
          <Button type="submit" disabled={trimmed.length === 0 || start.isPending}>
            {start.isPending ? (
              <>
                <Loader2 className="animate-spin" aria-hidden="true" />
                Starting&hellip;
              </>
            ) : (
              <>
                <FolderInput aria-hidden="true" />
                Start import
              </>
            )}
          </Button>
        </div>
      </form>
    </section>
  );
}

/** The live run: one continuous, throttled spoken status + the phase view. */
function ImportRun({ jobId }: { jobId: string }) {
  const { data, isPending, isError, error, refetch } = useImportJob(jobId);
  const notFound = error instanceof ImportJobNotFoundError;
  // Mounted in every branch (incl. loading) so a screen reader has a stable
  // announcer; throttled so a fast scan's 1s poll doesn't spam it. Terminal
  // states announce immediately (bypass the throttle): the import won't change
  // again, and the user may navigate away inside the throttle window, which
  // would otherwise swallow the once-only outcome.
  const message = announceMessage({ isPending, isError, notFound, data });
  const throttled = useThrottledValue(message, 4000);
  const terminal =
    notFound || isError || data?.phase === "done" || data?.phase === "failed";
  const status = terminal ? message : throttled;
  const announcer = (
    <p className="sr-only" role="status" aria-live="polite">
      {status}
    </p>
  );

  if (isPending) {
    return (
      <ImportShell>
        {announcer}
        <FeedSkeleton />
      </ImportShell>
    );
  }

  // A 404 (unknown/expired job) is terminal — a dedicated notice, no retry.
  if (notFound) {
    return (
      <ImportShell>
        {announcer}
        <JobNotFound />
      </ImportShell>
    );
  }

  if (isError) {
    return (
      <ImportShell>
        {announcer}
        <JobError onRetry={() => void refetch()} />
      </ImportShell>
    );
  }

  if (data.phase === "failed") {
    return (
      <ImportShell>
        {announcer}
        <JobFailed error={data.error} />
      </ImportShell>
    );
  }

  if (data.phase === "done") {
    return (
      <ImportShell>
        {announcer}
        <JobDone state={data} jobId={jobId} />
      </ImportShell>
    );
  }

  // scanning / reviewing / applying: the live feed.
  return (
    <ImportShell>
      {announcer}
      <LiveFeed state={data} jobId={jobId} />
    </ImportShell>
  );
}

/** Shared chrome for every run view: heading + a Start-over link. */
function ImportShell({ children }: { children: React.ReactNode }) {
  return (
    <section className="flex flex-col gap-6" aria-label="Import progress">
      <div className="flex items-center justify-between gap-4">
        <h2 className="text-2xl font-semibold tracking-tight">Import</h2>
        <Button variant="ghost" size="sm" asChild>
          <Link to="/import">Start over</Link>
        </Button>
      </div>
      {children}
    </section>
  );
}

/** scanning/reviewing/applying: a working line + the growing feed. */
function LiveFeed({ state, jobId }: { state: ImportJobState; jobId: string }) {
  const working = state.phase === "scanning" || state.phase === "applying";
  const scanningEmpty = working && state.albums.length === 0;
  return (
    <div className="flex flex-col gap-4">
      <p className="text-muted-foreground flex min-h-5 items-center gap-2 text-sm">
        {working && (
          <Loader2 className="size-3.5 animate-spin" aria-hidden="true" />
        )}
        {/* While scanning with nothing in the feed yet, the count line would
            read "0 albums imported" — say what's actually happening instead. */}
        {scanningEmpty ? (
          <span>Scanning your folder&hellip;</span>
        ) : (
          <span>
            {/* No known total (the feed grows as the worker reads) — count
                what's applied + flag whether one album awaits a decision.
                `needs_review` is at most 1 (review is sequential), but derive
                the count so that invariant is self-evident. */}
            {state.progress.applied}{" "}
            {state.progress.applied === 1 ? "album" : "albums"} imported
            {state.progress.skipped > 0 && ` · ${state.progress.skipped} skipped`}
            {state.progress.needs_review > 0 &&
              ` · ${state.progress.needs_review} album${state.progress.needs_review === 1 ? "" : "s"} needs review`}
          </span>
        )}
      </p>

      {state.albums.length === 0 ? (
        <FeedSkeleton />
      ) : (
        <FeedList albums={state.albums} jobId={jobId} />
      )}
    </div>
  );
}

/** The feed listing — shared by the live run and the done summary. Carries the
 * `jobId` so each row's Review link can hand it across the chunk-4 seam. */
function FeedList({
  albums,
  jobId,
}: {
  albums: ImportAlbumSummary[];
  jobId: string;
}) {
  return (
    <ul className="border-border divide-border divide-y rounded-xl border">
      {albums.map((album) => (
        <li key={album.index}>
          <FeedRow album={album} jobId={jobId} />
        </li>
      ))}
    </ul>
  );
}

/** One feed row. An `applied`/`skipped`/`decided` album is calm (a status
 * badge); the one `needs_review` row is highlighted and offers Review (→ the
 * seam). */
function FeedRow({
  album,
  jobId,
}: {
  album: ImportAlbumSummary;
  jobId: string;
}) {
  const needsReview = album.status === "needs_review";
  // Final fallback is non-empty: `album` may be null and `folder` may be ""/"/",
  // in which case folderName() returns "" — never show an empty title.
  const title =
    (album.album ?? folderName(album.folder)) || "Unknown album";
  return (
    <div
      className={cn(
        "flex min-w-0 items-center gap-3 px-4 py-3",
        needsReview && "bg-primary/5",
      )}
    >
      <div className="flex min-w-0 flex-1 flex-col">
        <span className="truncate font-medium">{title}</span>
        <span className="text-muted-foreground truncate text-sm">
          {album.artist ?? "Unknown artist"}
          <span aria-hidden="true"> · </span>
          {/* `confidence` is already a 0–100 percentage from the backend
              mapping (app/beets/import_mapping.py `_confidence` = round((1 -
              dist) * 100, 1)), so rounding is correct — not a 0–1 fraction. */}
          {Math.round(album.confidence)}% ·{" "}
          {RECOMMENDATION_LABEL[album.recommendation]}
        </span>
      </div>
      <StatusBadge status={album.status} />
      {needsReview && (
        <Button size="sm" asChild>
          {/* Carry the job id across the chunk-4 seam (the candidate-review
              hooks need it); consistent with the run page's `?job=` convention. */}
          <Link to={`/import/albums/${album.index}?job=${jobId}`}>Review</Link>
        </Button>
      )}
    </div>
  );
}

/** The status chip. Color + the text label both carry the state (not color
 * alone). `needs_review` reads "Needs review". */
function StatusBadge({ status }: { status: ImportAlbumSummary["status"] }) {
  const label: Record<ImportAlbumSummary["status"], string> = {
    applied: "Imported",
    decided: "Decided",
    skipped: "Skipped",
    needs_review: "Needs review",
  };
  const variant =
    status === "needs_review"
      ? "default"
      : status === "skipped"
        ? "outline"
        : "secondary";
  return (
    <Badge variant={variant} className="shrink-0">
      {label[status]}
    </Badge>
  );
}

/** Last path segment of a folder, for albums with no parsed album title. */
function folderName(folder: string): string {
  const parts = folder.split("/").filter(Boolean);
  return parts.at(-1) ?? folder;
}

/** done: a legible outcome — imported/skipped counts (counting auto-applied
 * albums), where each landed (the feed list), and a way into the library. */
function JobDone({ state, jobId }: { state: ImportJobState; jobId: string }) {
  const { applied, skipped } = state.progress;
  return (
    <div className="flex flex-col gap-4">
      <div className="border-border flex flex-col items-center gap-3 rounded-xl border py-12 text-center">
        <CircleCheck className="text-muted-foreground size-10" aria-hidden="true" />
        <div className="flex flex-col gap-1">
          <p className="font-medium">Import finished</p>
          <p className="text-muted-foreground text-sm">
            {applied} {applied === 1 ? "album" : "albums"} imported
            {` · ${skipped} skipped`}
          </p>
        </div>
        <Button variant="outline" size="sm" asChild>
          <Link to="/">View in library</Link>
        </Button>
      </div>
      {state.albums.length > 0 && <FeedList albums={state.albums} jobId={jobId} />}
    </div>
  );
}

/** failed: the worker's error + a way to start over. */
function JobFailed({ error }: { error: string | null }) {
  return (
    <div className="border-destructive/40 bg-destructive/5 flex flex-col items-center gap-3 rounded-xl border py-16 text-center">
      <AlertCircle className="text-destructive size-10" aria-hidden="true" />
      <div className="flex flex-col gap-1">
        <p className="font-medium">Import failed</p>
        <p className="text-muted-foreground text-sm">
          {error ?? "The import stopped unexpectedly."}
        </p>
      </div>
      {/* The shell chrome already renders a ghost "Start over" -> /import; this
          panel CTA uses a distinct label so the two aren't identical. */}
      <Button variant="outline" size="sm" asChild>
        <Link to="/import">Import another folder</Link>
      </Button>
    </div>
  );
}

/** The polled job id is unknown or expired (404). Distinct from a transient
 * load error: there's nothing to retry, so offer a fresh start instead. */
function JobNotFound() {
  return (
    <div className="border-border flex flex-col items-center gap-3 rounded-xl border py-16 text-center">
      <AlertCircle className="text-muted-foreground size-10" aria-hidden="true" />
      <div className="flex flex-col gap-1">
        <p className="font-medium">This import is no longer available</p>
        <p className="text-muted-foreground text-sm">
          It may have finished in another session, or the server restarted.
          Start a new import to continue.
        </p>
      </div>
      <Button variant="outline" size="sm" asChild>
        <Link to="/import">Start a new import</Link>
      </Button>
    </div>
  );
}

/** Transient error fetching the job state (not the same as a failed import). */
function JobError({ onRetry }: { onRetry: () => void }) {
  return (
    <div className="border-destructive/40 bg-destructive/5 flex flex-col items-center gap-3 rounded-xl border py-16 text-center">
      <AlertCircle className="text-destructive size-10" aria-hidden="true" />
      <div className="flex flex-col gap-1">
        <p className="font-medium">Couldn&rsquo;t load the import</p>
        <p className="text-muted-foreground text-sm">
          The backend didn&rsquo;t respond. Try again.
        </p>
      </div>
      <Button variant="outline" size="sm" onClick={onRetry}>
        Retry
      </Button>
    </div>
  );
}

/** A few feed-row placeholders so scanning (empty feed) doesn't look broken. */
function FeedSkeleton() {
  return (
    <div
      className="border-border divide-border divide-y rounded-xl border"
      aria-hidden="true"
    >
      {Array.from({ length: 3 }, (_, i) => (
        <div key={i} className="flex items-center gap-3 px-4 py-3">
          <div className="flex flex-1 flex-col gap-2">
            <Skeleton className="h-4 w-1/3" />
            <Skeleton className="h-3 w-1/2" />
          </div>
          <Skeleton className="h-5 w-20 rounded-full" />
        </div>
      ))}
    </div>
  );
}
