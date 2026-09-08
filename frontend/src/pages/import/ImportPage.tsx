import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router";

import { useActiveImport } from "@/api/useActiveImport";
import { invalidateLibraryContent } from "@/api/useEventStream";
import type {
  ImportAlbumSummary,
  ImportJobState,
  ImportProgress,
  SweepStatus,
} from "@/api/useImport";
import {
  ImportConflictError,
  ImportJobNotFoundError,
  ImportStartRejectedError,
  RECOMMENDATION_LABEL,
  isTerminalPhase,
  isWorking,
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
import {
  ELAPSED_AFTER_S,
  SEGMENT_SEP,
  announceMessage,
  elapsedLabel,
  spokenElapsed,
} from "@/pages/import/importStatus";

/** The status line's elapsed segment: the visible `· 12m` plus its spoken twin.
 * `elapsedLabel` is deliberately terse and a screen reader reads `12m` as a
 * letter, so the visible half is hidden and {@link spokenElapsed} carries the
 * words. Same sr-only/aria-hidden pair as NamingPanel's preview arrow.
 *
 * One helper, not three copies: this renders at every site that shows the
 * number as a middot segment (the live feed's status line, the sweep's, and the
 * done panel's body). The two terminal panels that need a whole sentence use
 * {@link elapsedSentence} instead. Both halves use the visible label's own
 * floor, so a rendered segment always has something behind it — pass
 * `ELAPSED_AFTER_S`, or the 60s default silently deletes the visible number
 * too, for every run in the 30-59s band. */
function ElapsedSegment({ seconds }: Readonly<{ seconds: number }>) {
  const label = elapsedLabel(seconds);
  const spoken = spokenElapsed(seconds, ELAPSED_AFTER_S);
  if (label === null || spoken === null) return null;
  return (
    <>
      <span aria-hidden="true">{`${SEGMENT_SEP}${label}`}</span>
      {/* The middot is hidden and is not spoken, so a bare space ran the two
          clauses together — measured AT text: "1 album imported · 1 album needs
          review 2 minutes." A full stop stands in for the glyph, the same
          substitution the resume banner makes. */}
      <span className="sr-only">{`. ${spoken}.`}</span>
    </>
  );
}

/** The run's duration as its OWN sentence, for the terminal panels — visible
 * glyph plus the spoken twin {@link ElapsedSegment} explains.
 *
 * Not the middot segment: these bodies open with a raw error sentence or carry
 * nothing else at all, and a leading `·` is a dangling separator.
 *
 * Returns `undefined`, not null, below {@link ELAPSED_AFTER_S}: a panel whose
 * whole body is this sentence passes the result straight to `EmptyState.body`,
 * which renders an empty `<p>` (and its `gap-1`) for any node that is not
 * `undefined`. A sweep of an empty folder finishes well under the threshold. */
function elapsedSentence(seconds: number): React.ReactNode | undefined {
  const label = elapsedLabel(seconds);
  const spoken = spokenElapsed(seconds, ELAPSED_AFTER_S);
  if (label === null || spoken === null) return undefined;
  return (
    <>
      <span aria-hidden="true">{`Ran for ${label}.`}</span>
      <span className="sr-only">{`Ran for ${spoken}.`}</span>
    </>
  );
}

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

/** Derive the resume-banner copy from the active job's origin + set-aside
 * count. Returns the headline sentence (without the set-aside clause, which
 * the JSX appends as a separate muted span). */
function resumeBannerText(
  origin: string | undefined,
  needsReview: number,
): string {
  if (origin === "sweep") {
    return "A sweep is running; uncertain albums are being banked for review.";
  }
  if (origin === "inbox") {
    // The set-aside clause completes the sentence when there's a count.
    return needsReview > 0
      ? "An inbox import is running"
      : "An inbox import is running.";
  }
  return "An import is already running.";
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

  function onSubmit(e: React.SubmitEvent) {
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
          {/* `items-start`, not `items-center`: at 360px the text column is
              167px and both banner strings wrap, which centred the spinner
              mid-paragraph (measured 20px below the first line's centre, three
              lines). No top margin here, unlike the status lines: this spinner
              is size-5 (20px) and the line-height is 20px, so the correction
              is zero — measured, not copied. */}
          <p id="resume-import-hint" className="flex items-start gap-3 font-medium">
            <Spinner
              className="text-muted-foreground size-5 shrink-0 animate-spin"
              aria-hidden="true"
            />
            <span>
              {resumeBannerText(origin, needsReview)}
              {origin === "inbox" && needsReview > 0 && (
                // This paragraph IS the Start button's aria-describedby, and a
                // middot is not spoken — the description ran the two clauses
                // together ("…is running 3 albums set aside…"). The glyph is
                // hidden and a full stop stands in for it. Not verified with a
                // real screen reader.
                <span className="text-muted-foreground font-normal">
                  <span aria-hidden="true">{SEGMENT_SEP}</span>
                  <span className="sr-only">{". "}</span>
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
function ImportRun({ jobId }: Readonly<{ jobId: string }>) {
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
        {/* Tested BEFORE the sweep branch below: a failed job is never shown
            as a running one, whatever its origin — JobFailed branches on the
            origin itself so a dead sweep still reads as a sweep. */}
        <JobFailed state={data} jobId={jobId} />
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
function ImportShell({ children }: Readonly<{ children: React.ReactNode }>) {
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
function LiveFeed({ state, jobId }: Readonly<{ state: ImportJobState; jobId: string }>) {
  // THE shared predicate — the same call the poll cadence makes (useImport), so
  // the spinner and the request rate can never disagree about who is working.
  const working = isWorking(state);
  // Gated on the feed alone, NOT on `working`: a park buffered before its row
  // exists leaves the state blocked with an empty feed, and `working` is false
  // there — which rendered the "0 albums imported" the branch below exists to
  // prevent. LiveFeed is only reached on an active phase.
  const scanningEmpty = state.albums.length === 0;
  // `progress` has no duplicate counter (backend), so derive the
  // duplicate-pending count from the feed rows for the cue line below.
  const needsDup = state.albums.filter(
    (a) => a.status === "needs_dup_resolution",
  ).length;
  return (
    <div className="flex flex-col gap-4">
      {/* `items-start`, not `items-center`: this line wraps to two lines at
          360px, and centring parked the spinner mid-paragraph (measured 10px
          below the first line's centre). */}
      <p className="text-muted-foreground flex min-h-5 items-start gap-2 text-sm">
        {/* Always mounted, only hidden: mounting/unmounting it on every park
            and unpark shifted the whole line ~24px sideways (size-4 + gap-2)
            each time. `invisible` keeps the box, and a hidden element must not
            animate. `mt-0.5` is (line-height 20px - size-4) / 2, which puts the
            icon on the FIRST line box however many the text takes. */}
        <Spinner
          className={cn(
            "mt-0.5 size-4 shrink-0",
            working ? "animate-spin" : "invisible",
          )}
          aria-hidden="true"
        />
        <span>
          {/* With nothing in the feed yet, the count line would read
              "0 albums imported" — say what's actually happening instead. */}
          {scanningEmpty ? (
            <>Scanning your folder&hellip;</>
          ) : (
            <>
              {/* No known total (the feed grows as the worker reads) — count
                  what's applied + flag whether one album awaits a decision.
                  `needs_review` is at most 1 (review is sequential), but derive
                  the count so that invariant is self-evident. */}
              {state.progress.applied}{" "}
              {state.progress.applied === 1 ? "album" : "albums"} imported
              {state.progress.skipped > 0 &&
                `${SEGMENT_SEP}${state.progress.skipped} skipped`}
              {state.progress.needs_review > 0 &&
                `${SEGMENT_SEP}${state.progress.needs_review} album${state.progress.needs_review === 1 ? "" : "s"} needs review`}
              {/* Import-time duplicates are named "already in library" so
                  "Duplicates" (the Manage page) names exactly one thing. */}
              {needsDup > 0 && `${SEGMENT_SEP}${needsDup} already in library`}
            </>
          )}
          {/* Same middot dialect as the counts, inside the one text flow so it
              reads as part of the line rather than a second column. Shown
              throughout, parked included: it is the whole run's duration, not a
              "since last progress" gauge, so hiding it while a decision is owed
              would make it vanish and come back carrying the operator's own
              thinking time. */}
          <ElapsedSegment seconds={state.elapsed_seconds} />
        </span>
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
 * the job goes done with `sweep.paused` still true — that flag is what titles
 * the panel below. */
/** Derive the sweep's live-status line from paused / current-folder state. */
function sweepStatusLabel(
  paused: boolean,
  currentFolder: string | null | undefined,
): string {
  if (paused) return "Pausing; finishing the current album…";
  if (currentFolder) return `Sweeping ${folderName(currentFolder)}…`;
  return "Sweeping your folder…";
}

/** The sweep's four counters, in the cardless stats dialect. A sweep counts
 * instead of accumulating feed rows (`state.albums` stays empty by design), so
 * these tiles are the whole record of the run — on the running panel, the
 * finished one, and the failed one alike. */
function SweepTiles({ sweep }: Readonly<{ sweep: SweepStatus }>) {
  return (
    <div className="grid grid-cols-2 gap-6 sm:grid-cols-4">
      <StatTile icon={Albums} label="Processed" value={String(sweep.processed)} />
      <StatTile icon={Success} label="Imported" value={String(sweep.auto_applied)} />
      <StatTile icon={ReviewIcon} label="Banked" value={String(sweep.banked)} />
      <StatTile icon={Resolved} label="Already known" value={String(sweep.skipped_known)} />
    </div>
  );
}

/** The one short line a finished sweep owes beyond its four tiles, or null.
 *
 * Both cases are things the tiles cannot say. A paused sweep is resumable and
 * the running panel is the only place that ever said how — the sentence was
 * gated on `!done`, so it vanished at the exact moment it applies. And four zero
 * tiles report that nothing happened without saying why. */
function sweepDoneNote(sweep: SweepStatus): string | null {
  if (sweep.paused) return "Resume later by sweeping the same folder again.";
  const touched =
    sweep.processed + sweep.auto_applied + sweep.banked + sweep.skipped_known;
  return touched === 0 ? "No albums found in that folder." : null;
}

/** The finished sweep panel's body: the note above, the duration, or neither.
 *
 * Returns `undefined` when there is nothing to say — `EmptyState.body` paints an
 * empty `<p>` and its `gap-1` for any node that is not `undefined`, and a fast
 * sweep with real counts has no note and no duration. */
function sweepDoneBody(
  sweep: SweepStatus,
  seconds: number,
): React.ReactNode | undefined {
  const ranFor = elapsedSentence(seconds);
  const note = sweepDoneNote(sweep);
  if (note === null) return ranFor;
  return (
    <>
      {note}
      {ranFor !== undefined && <span className="mt-1 block">{ranFor}</span>}
    </>
  );
}

/** A terminal sweep's single action, chosen by what the run produced — banked
 * albums to decide, else imported ones to look at, else a fresh run.
 *
 * Shared by the finished and failed panels: a crash does not change where the
 * albums went, and the failed panel already reports the counts, so withholding
 * the link only costs the user the navigation. `variant` is what differs — the
 * failed panel stays `outline`, since a solid CTA there reads as success. */
function SweepDoneCta({
  sweep,
  variant,
}: Readonly<{ sweep: SweepStatus; variant?: "outline" }>) {
  // One pair per outcome, picked in priority order: a decision owed outranks a
  // result to look at, which outranks starting over.
  let to = "/import";
  let label = "Import another folder";
  if (sweep.banked > 0) {
    to = "/review";
    label = "Review banked albums";
  } else if (sweep.auto_applied > 0) {
    to = "/browse?sort=added";
    label = "See them in the library";
  }
  return (
    <Button variant={variant} size="sm" asChild>
      <Link to={to}>{label}</Link>
    </Button>
  );
}

function SweepRun({ state, jobId }: Readonly<{ state: ImportJobState; jobId: string }>) {
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
          // A user-interrupted sweep is not a completion, so it does not wear
          // the success check; the Pause glyph names what actually happened.
          icon={sweep.paused ? Pause : Success}
          title={sweep.paused ? "Sweep paused" : "Sweep finished"}
          // The tiles below ARE the counts, and they are the app's own labels.
          // This body used to restate all four ~24px above them in a server-
          // built string ("swept 30, auto-applied 20, …"), so the panel said
          // every number twice. What the tiles cannot say is how long it took
          // and what to do next; that is all this body is now. The pause is
          // still in the title and on `sweep.paused`, so the word is not
          // repeated either.
          body={sweepDoneBody(sweep, state.elapsed_seconds)}
          // The CTA points at what the run actually produced. Banked albums
          // are decisions waiting, so they win. A sweep that only auto-applied
          // has no feed of its own and nothing to review, and sending it to
          // /import just repeated the shell chrome's own "Start over" — so it
          // goes to the library instead, newest first (`sort=added` is
          // descending by date added). Nothing produced falls through to a
          // fresh run.
          action={<SweepDoneCta sweep={sweep} />}
        />
      ) : (
        // Same alignment as the feed's status line: this one carries a folder
        // name, so it wraps sooner.
        <p className="text-muted-foreground flex min-h-5 items-start gap-2 text-sm">
          <Spinner
            className="mt-0.5 size-4 shrink-0 animate-spin"
            aria-hidden="true"
          />
          <span>
            {sweepStatusLabel(sweep.paused, sweep.current_folder)}
            {/* A sweep is the longest-running import there is and it returns
                before LiveFeed ever renders, so this is the only place its
                duration shows while it runs. Same threshold and middot dialect
                as the feed's status line. */}
            <ElapsedSegment seconds={state.elapsed_seconds} />
          </span>
        </p>
      )}

      <SweepTiles sweep={sweep} />

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

/** The feed listing — shared by the live run and the terminal panels. Carries
 * the `jobId` so each row's links can thread the run origin. `readOnly` drops
 * the per-row decision buttons: on a failed job nothing consumes a choice. */
function FeedList({
  albums,
  jobId,
  readOnly = false,
}: Readonly<{
  albums: ImportAlbumSummary[];
  jobId: string;
  readOnly?: boolean;
}> ) {
  // Pin the album awaiting action to the top — in sequential review it's the one
  // thing to act on (and always the latest), so its Review/Resolve button stays
  // in view without scrolling. A parked duplicate awaits action just the same.
  // Everything else lists newest-first below it (the bank-backlog order): the
  // most recently landed album is the one the user is watching for, so it must
  // not sink to the bottom of a long run. On the done screen nothing is
  // pending, so the whole feed reads newest-first.
  const pending = (s: ImportAlbumSummary["status"]) =>
    s === "needs_review" || s === "needs_dup_resolution";
  // ...but only while there is a button to keep in view. `readOnly` strips those
  // buttons, so on a failed job the pin reorders the record of the run for no
  // reason; there it reads newest-first like the done screen.
  const ordered = [...albums].sort((a, b) => {
    const pin = readOnly
      ? 0
      : Number(pending(b.status)) - Number(pending(a.status));
    return pin || b.index - a.index;
  });
  return (
    <ul className="border-border divide-border divide-y overflow-hidden rounded-xl border">
      {ordered.map((album) => (
        <li key={album.index}>
          <FeedRow album={album} jobId={jobId} readOnly={readOnly} />
        </li>
      ))}
    </ul>
  );
}

/** Derive the feed-row action button (Review / Resolve / none) from the
 * album's status. */
function feedRowAction(
  status: ImportAlbumSummary["status"],
  albumIndex: number,
  jobId: string,
  origin: { from: AlbumOrigin },
): React.ReactNode {
  if (status === "needs_review") {
    return (
      <Button size="sm" asChild>
        {/* Carry the job id (`?job=`) AND the origin state across the
            decision seam — the candidate page's back link + post-submit
            navigation use them. */}
        <Link to={`/import/albums/${albumIndex}?job=${jobId}`} state={origin}>
          Review
        </Link>
      </Button>
    );
  }
  if (status === "needs_dup_resolution") {
    return (
      <Button size="sm" asChild>
        <Link
          to={`/import/albums/${albumIndex}/duplicate?job=${jobId}`}
          state={origin}
        >
          Resolve
        </Link>
      </Button>
    );
  }
  return undefined;
}

/** One feed row on the shared AlbumRow. An `applied` album shows its library
 * cover and its title links to `/albums/{id}` (when the worker reported the
 * id); `skipped`/`decided` rows are calm; `needs_review` / parked-duplicate
 * rows are highlighted and offer Review / Resolve, threading the run origin. */
function FeedRow({
  album,
  jobId,
  readOnly = false,
}: Readonly<{
  album: ImportAlbumSummary;
  jobId: string;
  readOnly?: boolean;
}> ) {
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
          `${Math.round(album.confidence)}%${SEGMENT_SEP}${RECOMMENDATION_LABEL[album.recommendation]}`
        }
        badge={<StatusBadge album={album} />}
        href={linked ? `/albums/${albumId}` : undefined}
        hrefState={linked ? origin : undefined}
        action={
          readOnly
            ? undefined
            : feedRowAction(album.status, album.index, jobId, origin)
        }
      />
    </div>
  );
}

/** Derive the badge variant from the album's per-status label. */
function badgeVariant(
  status: ImportAlbumSummary["status"],
): "default" | "outline" | "secondary" {
  if (status === "needs_review" || status === "needs_dup_resolution") {
    return "default";
  }
  if (status === "skipped") return "outline";
  return "secondary";
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
function StatusBadge({ album }: Readonly<{ album: ImportAlbumSummary }>) {
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
  return (
    <Badge variant={badgeVariant(status)} className="shrink-0">
      {label[status]}
    </Badge>
  );
}

/** Last path segment of a folder, for albums with no parsed album title. */
function folderName(folder: string): string {
  const parts = folder.split("/").filter(Boolean);
  return parts.at(-1) ?? folder;
}

/** A feed-counting run's outcome line, in the page's middot dialect — shared by
 * the done panel and the failed one, which owe the same numbers.
 *
 * Owns up to albums that were decided/applied but never landed in the library
 * (the session died before beets ran task.add). `not_landed` is only ever
 * nonzero on a terminal job, so the clause drops out of a clean run. */
function countsLine(progress: ImportProgress): string {
  const { applied, skipped, not_landed } = progress;
  return (
    `${applied} ${applied === 1 ? "album" : "albums"} imported${SEGMENT_SEP}${skipped} skipped` +
    (not_landed > 0 ? `${SEGMENT_SEP}${not_landed} didn't land` : "")
  );
}

/** The failed panel's count line, or null when the run has nothing to own up to.
 *
 * The imported/skipped pair is gated on ITSELF, not on the three-way sum: a run
 * that landed and skipped nothing but lost five albums opened on
 * "0 albums imported · 0 skipped", exactly the noise the early-crash branch
 * exists to avoid. {@link JobDone} keeps the unconditional pair — a finished run
 * has landed/skipped counts worth stating even at zero. */
function failedCountsLine(progress: ImportProgress): string | null {
  const { applied, skipped, not_landed } = progress;
  if (applied + skipped > 0) return countsLine(progress);
  return not_landed > 0 ? `${not_landed} didn't land` : null;
}

/** done: a legible outcome — imported/skipped counts (counting auto-applied
 * albums) + the feed list, whose applied rows now link straight to their
 * library pages (replaces the old blanket "View in library", spec §1). */
function JobDone({ state, jobId }: Readonly<{ state: ImportJobState; jobId: string }>) {
  return (
    <div className="flex flex-col gap-4">
      <EmptyState
        bordered
        icon={Success}
        title="Import finished"
        body={
          <>
            {/* The finished summary carries the run's duration too (the owner's
                ruling): the number counts the whole run, so it must not vanish
                at the finish line. */}
            {countsLine(state.progress)}
            <ElapsedSegment seconds={state.elapsed_seconds} />
          </>
        }
      />
      {state.albums.length > 0 && (
        <FeedList albums={state.albums} jobId={jobId} />
      )}
    </div>
  );
}

/** failed: the worker's error, and what the run still earned before it died.
 *
 * A crash mid-apply is exactly when albums land or fail to land, so the server
 * keeps reporting this job's counters and computes `not_landed` BECAUSE the job
 * is terminal — a sweep that banked 200 albums and then died must not read as
 * though nothing happened. Sweeps count on `state.sweep` (their `albums` stays
 * empty by design); every other origin counts on `state.progress` and carries
 * the feed rows.
 *
 * `progress` does not partition the run. A set-aside row (`needs_review` /
 * `needs_dup_resolution`) is refused by the server's `_is_imported` AND its
 * `_is_skipped`, and never landed, so it falls out of all three counters — and
 * on a failed job its Review button is gone. `state.set_aside` is the count, and
 * it gets its own sentence: a failed run must not silently drop a category of
 * album it is still holding.
 *
 * An outcome notice on the EmptyState recipe (the recovery is a navigation, so
 * ErrorState's mandatory Retry would mislead — there is nothing to re-run), in
 * its `destructive` tone: on the neutral one this box was byte-identical to the
 * finished panel's, so "failed" in the title was the only thing carrying the
 * outcome — and this branch put earned counts beside it. */
function JobFailed({
  state,
  jobId,
}: Readonly<{ state: ImportJobState; jobId: string }>) {
  const sweep = state.origin === "sweep" ? (state.sweep ?? null) : null;
  // A run that died holding nothing has no counts to report and gains no line —
  // the terse early-crash panel is unchanged. A sweep's counts are its tiles,
  // never a second sentence.
  const counts = sweep === null ? failedCountsLine(state.progress) : null;
  // Set-aside albums are in none of the imported/skipped/lost buckets, so the
  // counts line above cannot own them and the rows below lose their buttons on
  // a failed job. Its own sentence, not a middot segment: it is a fact about
  // what is left in the source, not another entry in the count ledger.
  const setAside = sweep === null ? state.set_aside : 0;
  // A failure is a finish and the clock stops at both terminal transitions:
  // forty seconds versus forty minutes is a bad path versus a late crash.
  // (Under ELAPSED_AFTER_S no duration renders at all, so a 3s failure is
  // simply the untimed case.)
  const ranFor = elapsedSentence(state.elapsed_seconds);
  return (
    <div className={cn("flex flex-col", sweep !== null ? "gap-6" : "gap-4")}>
      <EmptyState
        bordered
        tone="destructive"
        icon={ErrorIcon}
        title={sweep !== null ? "Sweep failed" : "Import failed"}
        body={
          // `error` is the worker's `str(exc)` — a raw sentence with its own
          // punctuation. The middot dialect glued the duration onto the end of
          // it, which read as part of the message and could wrap a line open on
          // a bare "·". Each clause gets its own line, its own sentence.
          <>
            {state.error ?? "The import stopped unexpectedly."}
            {counts !== null && <span className="mt-1 block">{counts}</span>}
            {setAside > 0 && (
              <span className="mt-1 block">
                {setAside} album{setAside === 1 ? "" : "s"} set aside, not
                imported.
              </span>
            )}
            {ranFor !== undefined && <span className="mt-1 block">{ranFor}</span>}
          </>
        }
        action={
          // Same destinations as a finished sweep ({@link SweepDoneCta}): the
          // crash did not move the albums, and this panel already names the
          // counts. Outline, not solid — a solid CTA would read as a success
          // panel. A non-sweep failure has no feed to send anyone to, and its
          // label differs from the shell chrome's ghost "Start over" so the two
          // aren't identical.
          sweep !== null ? (
            <SweepDoneCta sweep={sweep} variant="outline" />
          ) : (
            <Button variant="outline" size="sm" asChild>
              <Link to="/import">Import another folder</Link>
            </Button>
          )
        }
      />
      {sweep !== null && <SweepTiles sweep={sweep} />}
      {/* Read-only. The rows are the record of what landed and — only ever on a
          terminal job — what didn't, but the worker is gone: a Review/Resolve
          button here would open a decision whose POST no worker will consume
          (`record_choice` has no terminal guard). */}
      {sweep === null && state.albums.length > 0 && (
        <FeedList albums={state.albums} jobId={jobId} readOnly />
      )}
    </div>
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
function JobError({ onRetry }: Readonly<{ onRetry: () => void }>) {
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
