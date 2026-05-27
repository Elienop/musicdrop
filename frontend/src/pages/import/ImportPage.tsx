import { AlertCircle, FolderInput, Loader2 } from "lucide-react";
import { useState } from "react";
import { Link, useSearchParams } from "react-router";

import type { ImportAlbumSummary, ImportJobState } from "@/api/useImport";
import {
  ImportConflictError,
  useImportJob,
  useStartImport,
} from "@/api/useImport";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";

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
          <p className="text-destructive text-sm" role="alert">
            An import is already running.{" "}
            {/* No job id in this error, so the link just returns to the page;
                a refresh with the active ?job= resumes the feed if present. */}
            <Link to="/import" className="underline underline-offset-4">
              View it
            </Link>
            .
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

/** The live run: polls the job and renders the phase-appropriate view. */
function ImportRun({ jobId }: { jobId: string }) {
  const { data, isPending, isError, refetch } = useImportJob(jobId);

  if (isPending) {
    return (
      <ImportShell>
        <p className="sr-only" role="status">
          Loading import&hellip;
        </p>
        <FeedSkeleton />
      </ImportShell>
    );
  }

  if (isError) {
    return (
      <ImportShell>
        <JobError onRetry={() => void refetch()} />
      </ImportShell>
    );
  }

  if (data.phase === "failed") {
    return (
      <ImportShell>
        <JobFailed error={data.error} />
      </ImportShell>
    );
  }

  if (data.phase === "done") {
    return (
      <ImportShell>
        <JobDone summary={data.summary} albums={data.albums} />
      </ImportShell>
    );
  }

  // scanning / reviewing / applying: the live feed.
  return (
    <ImportShell>
      <LiveFeed state={data} />
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
function LiveFeed({ state }: { state: ImportJobState }) {
  const working = state.phase === "scanning" || state.phase === "applying";
  return (
    <div className="flex flex-col gap-4">
      <p
        className="text-muted-foreground flex min-h-5 items-center gap-2 text-sm"
        aria-live="polite"
      >
        {working && (
          <Loader2 className="size-3.5 animate-spin" aria-hidden="true" />
        )}
        <span>
          {/* No known total (the feed grows as the worker reads) — count what's
              applied + flag whether one album awaits a decision. */}
          {state.progress.applied}{" "}
          {state.progress.applied === 1 ? "album" : "albums"} imported
          {state.progress.needs_review > 0 && " · 1 album needs review"}
          {working && state.albums.length === 0 && "Scanning your folder…"}
        </span>
      </p>

      {state.albums.length === 0 ? (
        <FeedSkeleton />
      ) : (
        <ul className="border-border divide-border divide-y rounded-xl border">
          {state.albums.map((album) => (
            <li key={album.index}>
              <FeedRow album={album} />
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** One feed row. An `applied`/`skipped`/`decided` album is calm (a status
 * badge); the one `needs_review` row is highlighted and offers Review (→ the
 * seam). */
function FeedRow({ album }: { album: ImportAlbumSummary }) {
  const needsReview = album.status === "needs_review";
  const title = album.album ?? folderName(album.folder);
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
          {Math.round(album.confidence)}% · {album.recommendation}
        </span>
      </div>
      <StatusBadge status={album.status} />
      {needsReview && (
        <Button size="sm" asChild>
          <Link to={`/import/albums/${album.index}`}>Review</Link>
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

/** done: a minimal summary + a link to the library (chunk 5 enriches this). */
function JobDone({
  summary,
  albums,
}: {
  summary: string | null;
  albums: ImportAlbumSummary[];
}) {
  return (
    <div className="flex flex-col gap-4">
      <div className="border-border flex flex-col items-center gap-3 rounded-xl border py-12 text-center">
        <FolderInput className="text-muted-foreground size-10" aria-hidden="true" />
        <div className="flex flex-col gap-1">
          <p className="font-medium">Import finished</p>
          <p className="text-muted-foreground text-sm">
            {summary ?? "Done."}
          </p>
        </div>
        <Button variant="outline" size="sm" asChild>
          <Link to="/">View in library</Link>
        </Button>
      </div>
      {albums.length > 0 && (
        <ul className="border-border divide-border divide-y rounded-xl border">
          {albums.map((album) => (
            <li key={album.index}>
              <FeedRow album={album} />
            </li>
          ))}
        </ul>
      )}
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
      <Button variant="outline" size="sm" asChild>
        <Link to="/import">Start over</Link>
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
