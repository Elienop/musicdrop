import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router";

import { useActiveImport } from "@/api/useActiveImport";
import { invalidateLibraryContent } from "@/api/useEventStream";
import type { ImportAlbumSummary, ImportJobState } from "@/api/useImport";
import {
  ImportConflictError,
  ImportJobNotFoundError,
  ImportStartRejectedError,
  RECOMMENDATION_LABEL,
  isTerminalPhase,
  useImportJob,
  usePauseSweep,
  useStartImport,
} from "@/api/useImport";
import type { AlbumOrigin } from "@/components/albums/album-grid";
import {
  AddFromFolder,
  Albums,
  Error as ErrorIcon,
  Info,
  Pause,
  Resolved,
  Review as ReviewIcon,
  Spinner,
  Success,
} from "@/components/icons";
import { AlbumRow } from "@/components/system/AlbumRow";
import { EmptyState } from "@/components/system/EmptyState";
import { ErrorState } from "@/components/system/ErrorState";
import { PageBody, PageHeader } from "@/components/system/PageHeader";
import { StatTile } from "@/components/system/StatTile";
import { StatusBanner } from "@/components/system/StatusBanner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { SegmentedControl } from "@/components/ui/segmented-control";
import { Skeleton } from "@/components/ui/skeleton";
import { useThrottledValue } from "@/lib/useThrottledValue";
import { cn } from "@/lib/utils";
import { announceMessage } from "@/pages/import/importStatus";

/** Origin threaded onto every link that leaves the feed (decision screens,
 * applied-album links) so back links and post-submit navigation return to
 * THIS run (spec §1). */
function importOrigin(jobId: string): { from: AlbumOrigin } {
  return { from: { label: "Import", to: `/import?job=${jobId}` } };
}

export function ImportPage() {
  const [searchParams] = useSearchParams();
  const jobId = searchParams.get("job") ?? undefined;

  // No active job in the URL -> the entry screen (path + Start).
  if (!jobId) {
    return <ImportEntry />;
  }
  return <ImportRun jobId={jobId} />;
}

/** Entry: a server-path input + Start. Polls the active-import probe so a
 * running import the user navigated away from surfaces a Resume banner (and
 * Start is gated while one runs); the blank-path guard and the residual 409
 * (swap-lock / race) are surfaced locally. On success the URL gains
 * `?job=<id>` and the page flips to the live run. */
function ImportEntry() {
  const [, setSearchParams] = useSearchParams();
  const [path, setPath] = useState("");
  const [mode, setMode] = useState<"review" | "sweep">("review");
  const start = useStartImport();
  const queryClient = useQueryClient();
  const active = useActiveImport();

  // The active job's id (resume target) and whether an import owns the slot.
  // `active` and `job_id` are consistent server-side; guard both here so the
  // banner never renders a link to a null id.
  const activeJobId = active.data?.job_id ?? null;
  const importActive = (active.data?.active ?? false) && activeJobId !== null;
  // An inbox-origin import is the unattended slskd path: name it as such and,
  // when it set albums aside, surface the count so the user knows there's a
  // review to do once it finishes.
  const origin = active.data?.origin;
  const needsReview = active.data?.needs_review_count ?? 0;

  const trimmed = path.trim();
  const conflict = start.error instanceof ImportConflictError;
  // A 422 carries the backend guard's reason verbatim (e.g. the in-library
  // copy-mode refusal) — surfaced as its own alert below.
  const rejected =
    start.error instanceof ImportStartRejectedError ? start.error.message : null;
  // A non-conflict, non-rejected error is a generic start failure.
  const genericError = start.isError && !conflict && rejected === null;

  function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (trimmed.length === 0) {
      return; // Button is disabled too; guard the Enter key.
    }
    start.mutate(
      mode === "sweep"
        ? {
            path: trimmed,
            // All three fields: the generated ImportOptions marks defaulted
            // fields required. {sweep: true} alone is a complete sweep request
            // server-side (the session enforces `unattended or sweep`).
            options: { operation: "default", unattended: false, sweep: true },
          }
        : { path: trimmed },
      {
        onSuccess: (data) => {
          setSearchParams({ job: data.job_id });
        },
        onError: (err) => {
          // A 409 can mean an import started in another tab/session between
          // probes — refresh the active-import query so the Resume banner
          // appears with the running job's id instead of a dead-end.
          if (err instanceof ImportConflictError) {
            void queryClient.invalidateQueries({ queryKey: ["active-import"] });
          }
        },
      },
    );
  }

  return (
    <PageBody>
      <PageHeader title="Add from folder" />
      <p className="text-muted-foreground text-sm">
        Add music from a folder on the server. beets scans it, matches each
        album against MusicBrainz, and files what it finds into your library.
      </p>

      {importActive && activeJobId && (
        // A running import the user navigated away from — one click back in.
        // Resuming just navigates to `?job=<id>`; the run page routes to the
        // right phase view and pins any album awaiting a decision. The text's
        // id describes the disabled Start below (aria-describedby) so a
        // keyboard/SR user gets the "why" + the recovery action without
        // duplicate copy — and without a disabled-button title (spec §4 rule).
        <StatusBanner
          tone="neutral"
          action={
            <Button size="sm" asChild>
              <Link to={`/import?job=${activeJobId}`}>Resume</Link>
            </Button>
          }
        >
          <p id="resume-import-hint" className="flex items-center gap-3 font-medium">
            <Spinner
              className="text-muted-foreground size-5 shrink-0 animate-spin"
              aria-hidden="true"
            />
            <span>
              {origin === "sweep"
                ? "A sweep is running; uncertain albums are being banked for review."
                : origin === "inbox"
                  ? needsReview > 0
                    ? "An inbox import is running" // the set-aside clause completes the sentence
                    : "An inbox import is running."
                  : "An import is already running."}
              {origin === "inbox" && needsReview > 0 && (
                <span className="text-muted-foreground font-normal">
                  {" · "}
                  {needsReview} album{needsReview === 1 ? "" : "s"} set aside for
                  review.
                </span>
              )}
            </span>
          </p>
        </StatusBanner>
      )}

      <form className="flex flex-col gap-3" onSubmit={onSubmit}>
        <div className="flex flex-col gap-2">
          <span className="text-sm font-medium">Import mode</span>
          <SegmentedControl
            aria-label="Import mode"
            value={mode}
            onChange={(v) => setMode(v === "sweep" ? "sweep" : "review")}
            options={[
              { value: "review", label: "Review now" },
              { value: "sweep", label: "Sweep & bank" },
            ]}
          />
          <p className="text-muted-foreground text-xs">
            {mode === "sweep"
              ? "Unattended: strong matches import automatically; everything else is banked for review on the Review page. Re-running a sweep skips what's already handled."
              : "Interactive: each uncertain album waits for your decision before the import continues."}
          </p>
        </div>

        <label className="flex flex-col gap-2">
          <span className="text-sm font-medium">Folder path</span>
          <Input
            type="text"
            value={path}
            onChange={(e) => setPath(e.target.value)}
            placeholder="/music/incoming"
            aria-label="Folder path"
            aria-invalid={genericError || conflict || rejected !== null}
          />
        </label>

        {conflict && (
          <p className="text-destructive text-sm" role="alert">
            {activeJobId
              ? "An import is already running; use Resume above."
              : "Couldn't start; a library operation is in progress. Try again in a moment."}
          </p>
        )}
        {rejected !== null && (
          <p className="text-destructive text-sm" role="alert">
            {rejected}
          </p>
        )}
        {genericError && (
          <p className="text-destructive text-sm" role="alert">
            Couldn&rsquo;t start the import. Check the path and the backend,
            then try again.
          </p>
        )}

        <div>
          <Button
            type="submit"
            disabled={trimmed.length === 0 || start.isPending || importActive}
            aria-describedby={importActive ? "resume-import-hint" : undefined}
          >
            {start.isPending ? (
              <>
                <Spinner className="animate-spin" aria-hidden="true" />
                Starting&hellip;
              </>
            ) : (
              <>
                <AddFromFolder aria-hidden="true" />
                {mode === "sweep" ? "Start sweep" : "Start import"}
              </>
            )}
          </Button>
        </div>
      </form>
    </PageBody>
  );
}

/** The live run: one continuous, throttled spoken status + the phase view. */
function ImportRun({ jobId }: { jobId: string }) {
  const { data, isPending, isError, error, refetch } = useImportJob(jobId);
  const notFound = error instanceof ImportJobNotFoundError;
  const queryClient = useQueryClient();

  // Imported albums landed in the library — once the run reaches a terminal
  // phase, refresh the cached library surfaces (grids, roster, browse, search,
  // stats) so they show the new albums within the 30s staleTime window.
  // "failed" counts too: albums apply sequentially, so a failed run may have
  // landed some before the error.
  const phase = data?.phase;
  useEffect(() => {
    if (phase !== undefined && isTerminalPhase(phase)) {
      invalidateLibraryContent(queryClient);
    }
  }, [phase, queryClient]);
  // Mounted in every branch (incl. loading) so a screen reader has a stable
  // announcer; throttled so a fast scan's 1s poll doesn't spam it. Terminal
  // states announce immediately (bypass the throttle): the import won't change
  // again, and the user may navigate away inside the throttle window, which
  // would otherwise swallow the once-only outcome.
  const message = announceMessage({ isPending, isError, notFound, data });
  const throttled = useThrottledValue(message, 4000);
  const terminal =
    notFound || isError || (data !== undefined && isTerminalPhase(data.phase));
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

  // Sweep jobs have no per-album feed (state.albums stays empty by design) —
  // LiveFeed/JobDone would render eternal skeletons / "0 albums imported".
  if (data.origin === "sweep") {
    return (
      <ImportShell>
        {announcer}
        <SweepRun state={data} jobId={jobId} />
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

/** Shared chrome for every run view: the page header + a Start-over action. */
function ImportShell({ children }: { children: React.ReactNode }) {
  return (
    <PageBody>
      <PageHeader
        title="Add from folder"
        actions={
          <Button variant="ghost" size="sm" asChild>
            <Link to="/import">Start over</Link>
          </Button>
        }
      />
      {children}
    </PageBody>
  );
}

/** scanning/reviewing/applying: a working line + the growing feed. */
function LiveFeed({ state, jobId }: { state: ImportJobState; jobId: string }) {
  const working = state.phase === "scanning" || state.phase === "applying";
  const scanningEmpty = working && state.albums.length === 0;
  // `progress` has no duplicate counter (backend), so derive the
  // duplicate-pending count from the feed rows for the cue line below.
  const needsDup = state.albums.filter(
    (a) => a.status === "needs_dup_resolution",
  ).length;
  return (
    <div className="flex flex-col gap-4">
      <p className="text-muted-foreground flex min-h-5 items-center gap-2 text-sm">
        {working && (
          <Spinner className="size-4 animate-spin" aria-hidden="true" />
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
            {state.progress.skipped > 0 &&
              ` · ${state.progress.skipped} skipped`}
            {state.progress.needs_review > 0 &&
              ` · ${state.progress.needs_review} album${state.progress.needs_review === 1 ? "" : "s"} needs review`}
            {/* Import-time duplicates are named "already in library" so
                "Duplicates" (the Manage page) names exactly one thing. */}
            {needsDup > 0 && ` · ${needsDup} already in library`}
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

/** The sweep's whole progress surface: counters (StatTile, the cardless
 * stats dialect), the current folder, Pause, and the Review hand-off. Rides
 * the existing 1s job poll. A paused sweep finishes its current album, then
 * the job goes done with a "- paused" summary and `sweep.paused` stays true. */
function SweepRun({ state, jobId }: { state: ImportJobState; jobId: string }) {
  const pause = usePauseSweep(jobId);
  const sweep = state.sweep;
  if (sweep == null) {
    // Defensive only: the backend always sets the block on sweep jobs.
    return <LiveFeed state={state} jobId={jobId} />;
  }
  const done = state.phase === "done";
  return (
    <div className="flex flex-col gap-6">
      {done ? (
        <EmptyState
          bordered
          icon={Success}
          title={sweep.paused ? "Sweep paused" : "Sweep finished"}
          body={
            state.summary ??
            `${sweep.processed} processed · ${sweep.auto_applied} imported · ${sweep.banked} banked`
          }
          action={
            <Button size="sm" asChild>
              <Link to="/review">Review banked albums</Link>
            </Button>
          }
        />
      ) : (
        <p className="text-muted-foreground flex min-h-5 items-center gap-2 text-sm">
          <Spinner className="size-4 animate-spin" aria-hidden="true" />
          <span>
            {sweep.paused
              ? "Pausing; finishing the current album…"
              : sweep.current_folder
                ? `Sweeping ${folderName(sweep.current_folder)}…`
                : "Sweeping your folder…"}
          </span>
        </p>
      )}

      <div className="grid grid-cols-2 gap-6 sm:grid-cols-4">
        <StatTile icon={Albums} label="Processed" value={String(sweep.processed)} />
        <StatTile icon={Success} label="Imported" value={String(sweep.auto_applied)} />
        <StatTile icon={ReviewIcon} label="Banked" value={String(sweep.banked)} />
        <StatTile icon={Resolved} label="Already known" value={String(sweep.skipped_known)} />
      </div>

      {!done && (
        <div className="flex flex-col gap-1.5">
          {pause.isError && (
            <p className="text-destructive text-sm" role="alert">
              Couldn&rsquo;t pause. Try again.
            </p>
          )}
          <div>
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={pause.isPending || sweep.paused}
              onClick={() => pause.mutate()}
            >
              <Pause aria-hidden="true" />
              {sweep.paused ? "Pausing…" : "Pause sweep"}
            </Button>
          </div>
          <p className="text-muted-foreground text-xs">
            Banked albums show up on the Review page as the sweep finds them;
            you can start deciding right away. Resume later by sweeping the
            same folder again.
          </p>
        </div>
      )}
    </div>
  );
}

/** The feed listing — shared by the live run and the done summary. Carries the
 * `jobId` so each row's links can thread the run origin. */
function FeedList({
  albums,
  jobId,
}: {
  albums: ImportAlbumSummary[];
  jobId: string;
}) {
  // Pin the album awaiting action to the top — in sequential review it's the one
  // thing to act on (and always the latest), so its Review/Resolve button stays
  // in view without scrolling. A parked duplicate awaits action just the same.
  // Everything else lists newest-first below it (the bank-backlog order): the
  // most recently landed album is the one the user is watching for, so it must
  // not sink to the bottom of a long run. On the done screen nothing is
  // pending, so the whole feed reads newest-first.
  const pending = (s: ImportAlbumSummary["status"]) =>
    s === "needs_review" || s === "needs_dup_resolution";
  const ordered = [...albums].sort(
    (a, b) =>
      Number(pending(b.status)) - Number(pending(a.status)) ||
      b.index - a.index,
  );
  return (
    <ul className="border-border divide-border divide-y overflow-hidden rounded-xl border">
      {ordered.map((album) => (
        <li key={album.index}>
          <FeedRow album={album} jobId={jobId} />
        </li>
      ))}
    </ul>
  );
}

/** One feed row on the shared AlbumRow. An `applied` album shows its library
 * cover and its title links to `/albums/{id}` (when the worker reported the
 * id); `skipped`/`decided` rows are calm; `needs_review` / parked-duplicate
 * rows are highlighted and offer Review / Resolve, threading the run origin. */
function FeedRow({
  album,
  jobId,
}: {
  album: ImportAlbumSummary;
  jobId: string;
}) {
  const needsReview = album.status === "needs_review";
  const needsDup = album.status === "needs_dup_resolution";
  // Final fallback is non-empty: `album` may be null and `folder` may be ""/"/",
  // in which case folderName() returns "" — never show an empty title.
  const title = (album.album ?? folderName(album.folder)) || "Unknown album";
  // Task 1's outcome plumbing: the id is only ever set once the album LANDED
  // in the library — for auto-applied strong matches (status "applied") AND
  // user-decided Applies (status "decided", id arrives via the follow-up).
  // So a non-null id is the link condition; status alone under-links.
  const albumId = album.album_id ?? null;
  const linked = albumId !== null;
  const origin = importOrigin(jobId);
  return (
    // Highlight stays with the caller (AlbumRow contract).
    <div className={cn((needsReview || needsDup) && "bg-primary/5")}>
      {/* ?size=thumb: AlbumRow renders the cover at size-10 (40 CSS px) and
          this feed shows dozens of rows at once — the densest cover consumer
          in the app. CoverArt never appends a query of its own, so a literal
          append is safe. */}
      <AlbumRow
        cover={albumId !== null ? `/api/albums/${albumId}/cover?size=thumb` : null}
        coverAssetKey={albumId !== null ? `album:${albumId}` : undefined}
        title={title}
        subtitle={album.artist ?? "Unknown artist"}
        meta={
          // `confidence` is already a 0–100 percentage from the backend
          // mapping (app/beets/import_mapping.py `_confidence`), so rounding
          // is correct — not a 0–1 fraction.
          `${Math.round(album.confidence)}% · ${RECOMMENDATION_LABEL[album.recommendation]}`
        }
        badge={<StatusBadge album={album} />}
        href={linked ? `/albums/${albumId}` : undefined}
        hrefState={linked ? origin : undefined}
        action={
          needsReview ? (
            <Button size="sm" asChild>
              {/* Carry the job id (`?job=`) AND the origin state across the
                  decision seam — the candidate page's back link + post-submit
                  navigation use them. */}
              <Link to={`/import/albums/${album.index}?job=${jobId}`} state={origin}>
                Review
              </Link>
            </Button>
          ) : needsDup ? (
            <Button size="sm" asChild>
              <Link
                to={`/import/albums/${album.index}/duplicate?job=${jobId}`}
                state={origin}
              >
                Resolve
              </Link>
            </Button>
          ) : undefined
        }
      />
    </div>
  );
}

/** The status chip. Color + the text label both carry the state (not color
 * alone). `needs_dup_resolution` reads "Already in library" — matching the
 * resolve screen's heading, so "Duplicates" names only the library finder.
 *
 * Precedence over the raw status (both landing outcomes the status alone can't
 * express): a row that never landed (only ever set on a TERMINAL job — the
 * backend gates the flag) flags the failure destructively; else a row that DID
 * land (an album_id arrived) reads as the positive "Imported" chip, upgrading a
 * user-decided Apply from the vague "Decided"; else the per-status label. */
function StatusBadge({ album }: { album: ImportAlbumSummary }) {
  if (album.did_not_land) {
    return (
      <Badge variant="destructive" className="shrink-0">
        Didn&apos;t land
      </Badge>
    );
  }
  if (album.album_id != null) {
    return (
      <Badge variant="secondary" className="shrink-0">
        Imported
      </Badge>
    );
  }
  const status = album.status;
  const label: Record<ImportAlbumSummary["status"], string> = {
    applied: "Imported",
    decided: "Decided",
    skipped: "Skipped",
    needs_review: "Needs review",
    needs_dup_resolution: "Already in library",
  };
  const variant =
    status === "needs_review" || status === "needs_dup_resolution"
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
 * albums) + the feed list, whose applied rows now link straight to their
 * library pages (replaces the old blanket "View in library", spec §1). */
function JobDone({ state, jobId }: { state: ImportJobState; jobId: string }) {
  const { applied, skipped, not_landed } = state.progress;
  // Own up to albums that were decided/applied but never landed in the library
  // (the session died before beets ran task.add) — only ever nonzero here on a
  // terminal job, so the clause simply drops out of a clean run.
  const body =
    `${applied} ${applied === 1 ? "album" : "albums"} imported · ${skipped} skipped` +
    (not_landed > 0 ? ` · ${not_landed} didn't land` : "");
  return (
    <div className="flex flex-col gap-4">
      <EmptyState bordered icon={Success} title="Import finished" body={body} />
      {state.albums.length > 0 && (
        <FeedList albums={state.albums} jobId={jobId} />
      )}
    </div>
  );
}

/** failed: the worker's error + a way to start over. An outcome notice on the
 * EmptyState recipe (the recovery is a navigation, so ErrorState's mandatory
 * Retry would mislead — there is nothing to re-run). */
function JobFailed({ error }: { error: string | null }) {
  return (
    <EmptyState
      bordered
      icon={ErrorIcon}
      title="Import failed"
      body={error ?? "The import stopped unexpectedly."}
      action={
        // The shell chrome already renders a ghost "Start over" -> /import;
        // this panel CTA uses a distinct label so the two aren't identical.
        <Button variant="outline" size="sm" asChild>
          <Link to="/import">Import another folder</Link>
        </Button>
      }
    />
  );
}

/** The polled job id is unknown or expired (404). Distinct from a transient
 * load error: there's nothing to retry, so offer a fresh start instead. */
function JobNotFound() {
  return (
    <EmptyState
      bordered
      icon={Info}
      title="This import is no longer available"
      body="It may have finished in another session, or the server restarted. Start a new import to continue."
      action={
        <Button variant="outline" size="sm" asChild>
          <Link to="/import">Start a new import</Link>
        </Button>
      }
    />
  );
}

/** Transient error fetching the job state (not the same as a failed import) —
 * the one genuinely retryable error, on the shared ErrorState. */
function JobError({ onRetry }: { onRetry: () => void }) {
  return (
    <ErrorState
      message="Couldn’t load the import. The backend didn’t respond. Try again."
      onRetry={onRetry}
    />
  );
}

/** A few feed-row placeholders so scanning (empty feed) doesn't look broken.
 * NOT wrapped in PageSkeleton: this page has its own dedicated run announcer
 * (one live region — adding PageSkeleton's role=status would double it). */
function FeedSkeleton() {
  return (
    <div
      className="border-border divide-border divide-y rounded-xl border"
      aria-hidden="true"
    >
      {Array.from({ length: 3 }, (_, i) => (
        <div key={i} className="flex items-center gap-3 px-4 py-3">
          <Skeleton className="size-10 shrink-0 rounded-md" />
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
