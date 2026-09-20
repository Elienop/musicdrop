import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
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
  startErrorSentence,
  useImportJob,
  useStartImport,
  useStopImport,
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
  Stop,
  Success,
  Warning,
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
import { SEGMENT_SEP } from "@/lib/format";
import { useThrottledValue } from "@/lib/useThrottledValue";
import { cn } from "@/lib/utils";
import {
  ELAPSED_AFTER_S,
  announceMessage,
  elapsedLabel,
  pendingDuplicates,
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

/** The import page's live status line: a spinner, then wrapping text. Two
 * callers — the feed's count line and the sweep's folder line. Their `<p>`
 * class strings were byte-identical before this existed, which is why the
 * alignment fix below needed a pass per copy. The `<svg>` ones were NOT: the
 * feed's was `cn("mt-0.5 size-4 shrink-0", working ? "animate-spin" :
 * "invisible")` and the sweep's the literal `"mt-0.5 size-4 shrink-0
 * animate-spin"`. They render the same only while the feed is working, which is
 * what the `spinning` prop is for.
 *
 * `items-start`, not `items-center`: at 360px both callers' text takes two
 * lines (measured) and centring parked the spinner mid-paragraph, 10px below
 * the first line's centre. `mt-0.5` puts it back — (line-height 20px − size-4
 * 16px) / 2 = 2px. Measured after the extraction: the icon's box centre sits
 * 0px from the first line box's centre, at 1280 and at 360, for both callers.
 *
 * The spinner box is always rendered and only hidden: mounting and unmounting
 * it on every park and unpark shifted the whole line ~24px sideways (size-4 +
 * gap-2) each time. `invisible` keeps the box, and a hidden element must not
 * animate.
 *
 * The resume banner is a third line of this shape and is deliberately NOT a
 * caller. Measured, it differs in six properties rather than one: gap 12px vs
 * 8px, icon 20px vs 16px, top correction 0 vs 2px (its icon matches the line
 * box exactly), `font-medium` vs inherited, the muted colour on the icon
 * rather than on the line, and no `min-h-5`. It also owns the `id` that the
 * Start button's `aria-describedby` points at. Both shapes satisfy the same
 * invariant today — offset 0 from the first line box, measured at 1280 and
 * 360 — and the banner's own comment carries its numbers. */
function StatusLine({
  spinning,
  children,
}: Readonly<{ spinning: boolean; children: React.ReactNode }>) {
  return (
    <p className="text-muted-foreground flex min-h-5 items-start gap-2 text-sm">
      <Spinner
        className={cn(
          "mt-0.5 size-4 shrink-0",
          spinning ? "animate-spin" : "invisible",
        )}
        aria-hidden="true"
      />
      <span>{children}</span>
    </p>
  );
}

/** Origin threaded onto every link that leaves the feed (decision screens,
 * applied-album links) so back links and post-submit navigation return to
 * THIS run (spec §1). */
function importOrigin(jobId: string): { from: AlbumOrigin } {
  return { from: { label: "Import", to: `/import?job=${jobId}` } };
}

/** Focus the page h1 when the run pointer changes on the same pathname.
 *
 * RouteAnnouncer moves focus to the h1 on a PATHNAME change, and `?job=A` ->
 * `?job=B` is not one — nor is the entry screen's own hand-off into a new run.
 * The control that started the run unmounts with the panel it sat in, so
 * keyboard focus falls to <body> and the next Tab restarts at "Skip to content"
 * (measured in Chromium (Orca), 2026-09-18: document.activeElement is BODY
 * after "Import them again").
 *
 * The {@link useDeferredH1Focus} shape: a ref sentinel skips the first run, so a
 * cold load keeps the browser's own focus, and a focus HELD by a live element is
 * never taken. */
function useJobChangeH1Focus(jobId: string | undefined): void {
  // null is "no effect has run yet" — distinct from an absent `?job=`, which is
  // `undefined` and is a real value to compare against.
  const previous = useRef<string | null | undefined>(null);
  useEffect(() => {
    const prior = previous.current;
    previous.current = jobId;
    if (prior === null || prior === jobId) return;
    focusH1IfNobodyHasFocus();
  }, [jobId]);
}

/** Move focus to the page h1, unless a live element is holding it.
 *
 * `activeElement === body` is what "the focused node was removed" looks like
 * after the fact: the browser falls back to <body> the moment the element goes,
 * and nothing in the app chose that. So the guard doubles as the condition —
 * a focus HELD by something still mounted is never taken.
 *
 * `options` because the two callers want different scroll behaviour, and
 * `focus()` scrolls its target into view by default. A job change is a
 * navigation, so it takes the scroll (RouteAnnouncer's posture); a run ending
 * under the reader is a data change on the page they are already on, so it
 * passes `{ preventScroll: true }` — the in-page convention at
 * `SearchPage.tsx`, `ArtistAlbumsPage.tsx` and `BrowsePage.tsx`. */
function focusH1IfNobodyHasFocus(options?: FocusOptions): void {
  if (document.activeElement !== document.body) return;
  document.querySelector<HTMLElement>('h1[tabindex="-1"]')?.focus(options);
}

/** Catch keyboard focus when a run ends under the user's hands.
 *
 * The two controls that end a run live in the active branches only — the
 * header's {@link StopRunButton} and {@link SweepRun}'s Pause — so the poll that
 * reports the terminal phase unmounts the button the presser is standing on and
 * focus lands on <body> (measured in Chromium (Orca), 2026-09-19: BODY on the
 * first poll after `phase: "done"`, so the next Tab restarts at "Skip to
 * content"). The `aria-disabled` posture protects the pending window and this
 * protects its end.
 *
 * Keyed on the page's own `terminal` rather than the phase, so it covers the
 * other two ends the run view has: a poll that 404s (the job expired, or the
 * server restarted) and a poll that errors both unmount the control the same
 * way, and both land on a panel of their own. `undefined` = the first poll is
 * still in flight, so a cold load onto a finished job reads as a first reading
 * and takes nothing; only false -> true is an end.
 *
 * {@link focusH1IfNobodyHasFocus} means a user reading the feed with focus on a
 * row link keeps it, and it is passed `{ preventScroll: true }`: the trigger is
 * wider than the presser (any end reached with focus on <body>, including a run
 * that finishes on its own while the page is scrolled down a long feed), and a
 * scrolling focus move would jump such a reader to the top.
 *
 * The h1 rather than the panel's CTA: the CTA is not on every terminal panel (a
 * plain finished run has none), it is the target the page already uses for the
 * same drop on a job change, and landing on the title leaves the whole outcome
 * panel ahead of the cursor instead of behind it. */
function useRunEndH1Focus(terminal: boolean | undefined): void {
  const previous = useRef<boolean | undefined>(undefined);
  useEffect(() => {
    const prior = previous.current;
    previous.current = terminal;
    if (prior !== false || terminal !== true) return;
    focusH1IfNobodyHasFocus({ preventScroll: true });
  }, [terminal]);
}

export function ImportPage() {
  const [searchParams] = useSearchParams();
  const jobId = searchParams.get("job") ?? undefined;
  useJobChangeH1Focus(jobId);

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

/** The id linking the entry screen's failure sentence to Start. One screen, one
 * alert (the branches below are exclusive), so a constant is enough — the same
 * shape as {@link IMPORT_AGAIN_ERROR_ID} and the `resume-import-hint` above. */
const START_ERROR_ID = "start-import-error";

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
  // The ONE sentence this screen owns: a 409 with a resumable import names the
  // Resume control above it, which no shared copy can do. Everything else goes
  // through {@link startErrorSentence} like the other two start surfaces, so a
  // 422 guard refusal AND a 503 layout refusal reach the user verbatim — this
  // screen is where most imports start, and it was the last one still throwing
  // the server's reason away in favour of "check the path and the backend".
  const resumeConflict =
    start.error instanceof ImportConflictError && activeJobId !== null;
  const failure = resumeConflict
    ? "An import is already running; use Resume above."
    : startErrorSentence(
        start.error,
        start.isError,
        "Couldn’t start the import. Check the path and the backend, then try again.",
      );

  // A refusal must not outlive the input it was about. `start.error` survives
  // until the next `mutate`, so after "That folder doesn't exist." the user
  // fixed the typo and the field stayed red with the stale sentence still wired
  // into Start's aria-describedby. Same shape as RenameArtistAction's
  // `onNameChange`, which drops a preview the moment its target changes.
  function onPathChange(value: string) {
    setPath(value);
    if (start.isError) start.reset();
  }

  function onSubmit(e: React.SubmitEvent) {
    e.preventDefault();
    if (trimmed.length === 0) {
      return; // Button is disabled too; guard the Enter key.
    }
    // The pending half of the button is `aria-disabled`, so the form still
    // submits while a start is in flight — swallow it here, the same way the
    // Pause button swallows its own click.
    if (start.isPending) {
      return;
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
          {/* The third line of the spinner + wrapping-text shape, and the one
              {@link StatusLine} does not cover — six measured properties
              apart, listed in that component's docstring. Change one, read the
              other.

              `items-start`, not `items-center`: at 360px the text column is
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
              ? "Unattended: strong matches import automatically; everything else is banked for review on the Review page. Re-running a sweep skips what’s already handled."
              : "Interactive: each uncertain album waits for your decision before the import continues."}
          </p>
        </div>

        <label className="flex flex-col gap-2">
          <span className="text-sm font-medium">Folder path</span>
          <Input
            type="text"
            value={path}
            onChange={(e) => onPathChange(e.target.value)}
            placeholder="/music/incoming"
            aria-label="Folder path"
            // The contract's own bound (`StartImportRequest.path`,
            // max_length=4096 — Linux PATH_MAX, so no real path reaches it).
            // Past it the server answers FastAPI's array-shaped 422, which is
            // machine copy and falls through to the page's generic sentence
            // (see ImportStartRejectedError): a refusal that never says the
            // word "long". The field refuses the overlong paste instead.
            maxLength={4096}
            // Only a 422 is about what is IN this field ("That folder doesn't
            // exist.", the in-library guard). A 409, a 503 or a dead backend
            // says nothing is wrong with the path, and reddening it there sends
            // the user off to edit the one thing that was fine.
            aria-invalid={start.error instanceof ImportStartRejectedError}
          />
        </label>

        {failure !== null && (
          // `break-words`: these sentences carry repr'd filesystem paths, and
          // Chromium gives no wrap opportunity at `/` or `_`. Measured for the
          // same family on the Trash page (SettingsTrashPage.tsx), which also
          // caps the measure — this form is a stretched child of a full-shell
          // PageBody, so without `max-w-prose` a carried path runs the whole
          // pane. No `w-full` beside it: the Trash sibling needs one because
          // its parent is `items-start`, and a stretched flex item is already
          // full width.
          <p
            id={START_ERROR_ID}
            className="text-destructive max-w-prose text-sm break-words"
            role="alert"
          >
            {failure}
          </p>
        )}

        <div>
          <Button
            type="submit"
            // Two states, two attributes. A blank path and a running import are
            // reasons the control cannot be used at all, so they stay
            // `disabled`. Pending is the button's OWN commit: disabling it there
            // strands keyboard focus on <body> (the Pagination rule, measured on
            // the Pause button below), so it goes `aria-disabled` and the submit
            // handler swallows the repeat.
            disabled={trimmed.length === 0 || importActive}
            aria-disabled={start.isPending}
            className="aria-disabled:opacity-50"
            // Both descriptions, joined: this button keeps focus through a
            // failed start (it is only `aria-disabled` while pending), so the
            // sentence saying why the last press failed is what a keyboard user
            // hears on coming back to it — and the resume hint still explains a
            // Start that is disabled outright.
            aria-describedby={
              [
                importActive ? "resume-import-hint" : null,
                failure !== null ? START_ERROR_ID : null,
              ]
                .filter((id) => id !== null)
                .join(" ") || undefined
            }
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
  // Every end this page knows: an unknown job, a failed poll, or a terminal
  // phase. Each renders a panel of its own and drops the header's control, and
  // both readers below owe all three the same treatment.
  const terminal =
    notFound || isError || (data !== undefined && isTerminalPhase(data.phase));
  // The end of a run unmounts whichever control ended it — see the hook.
  // `undefined` while the first poll is in flight: nothing is known about the
  // run yet, so the reading that follows it is not a transition.
  useRunEndH1Focus(isPending ? undefined : terminal);
  // Mounted in every branch (incl. loading) so a screen reader has a stable
  // announcer; throttled so a fast scan's 1s poll doesn't spam it. Terminal
  // states announce immediately (bypass the throttle): the import won't change
  // again, and the user may navigate away inside the throttle window, which
  // would otherwise swallow the once-only outcome.
  const message = announceMessage({ isPending, isError, notFound, data });
  const throttled = useThrottledValue(message, 4000);
  // An accepted stop bypasses the throttle too. Pressing the button changed the
  // visible line within a poll but left the announcer up to ~5s behind it (a 1s
  // poll plus the 4s window), and the button goes inert on click, so the one
  // thing that acknowledged the press was silent the longest. It is a
  // user-initiated change, and the person who just pressed a button is owed an
  // answer.
  //
  // `data.stopped` — the JOB's flag, not the sweep's and not the mutation's, so
  // one term covers both controls (one request sets both flags,
  // `registry.py` `request_stop`) and it needs no state from SweepRun or the
  // header. It is set once, when the stop is accepted, and nothing clears it for
  // the life of the job. That also makes the bypass STICKY instead of a
  // one-render pulse — a pulse would hand the announcer back a stale throttled
  // message on the very next render.
  //
  // Sticky means the throttle is OFF for the rest of the run, so what bounds
  // the announcer after a stop is the message itself, not the window: both
  // stopped branches in `announceMessage` are one fixed string, with no counters
  // and no elapsed clause, so it cannot change until the run ends. React writes
  // the same string, the DOM does not change, and nothing is re-read. Carrying
  // the counters here instead gave four announcements in ~5s, closest pair
  // 974ms, because the last album's two outcome records keep the numbers moving
  // after the stop is accepted.
  const stopping = data?.stopped === true;
  const status = terminal || stopping ? message : throttled;
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

  // scanning / reviewing / applying: the live feed. This is the only branch
  // that owns a header action — the one place a run is still going AND the
  // sweep (which keeps its own Pause) has already been routed away above.
  return (
    <ImportShell
      action={
        offersStop(data.origin) ? (
          <StopRunButton jobId={jobId} stopped={data.stopped} />
        ) : undefined
      }
    >
      {announcer}
      <LiveFeed state={data} jobId={jobId} />
    </ImportShell>
  );
}

/** Which runs the page offers a stop for: the ones a person started here.
 *
 * `manual` only. The API accepts a stop on any origin, but the two queue-driven
 * origins start the next item as soon as this job ends — the inbox drain and the
 * bank drain both pick up the next queued row (`apply_runner.py` `_drain`), and
 * a stopped bank-apply row goes back to the bank to be decided again. "Stop this
 * run" cannot keep its promise there: the user would read "the rest stayed in
 * the folder" while the next folder was already importing in a job this page
 * never shows. A sweep is routed away before this and keeps its own Pause.
 *
 * The same predicate gates the stopped panel's remedy — one origin makes that
 * promise, one origin is told how to undo it ({@link abortedFromThisPage}) —
 * and the line that answers a press nothing came of ({@link
 * stopCutNothingShort}). */
function offersStop(origin: ImportJobState["origin"]): boolean {
  return origin === "manual";
}

/** Shared chrome for every run view: the page header plus whatever action that
 * view owns. Only a live manual feed passes one ({@link StopRunButton},
 * {@link offersStop}); the terminal panels carry their own CTA and leave the
 * slot empty, and the sweep keeps its Pause inside {@link SweepRun}, so no view
 * offers two ways to end the same run.
 *
 * `undefined`, not `null`: PageHeader tests the slot with `!== undefined`, so
 * `null` would still paint the (empty) actions row. */
function ImportShell({
  children,
  action,
}: Readonly<{ children: React.ReactNode; action?: React.ReactNode }>) {
  return (
    <PageBody>
      <PageHeader title="Add from folder" actions={action} />
      {children}
    </PageBody>
  );
}

/** The run page's one stop control: end this import at the album it is on.
 *
 * Replaces the old "Start over" link, which only navigated back to the entry
 * screen and left the run going. Stopping is reversible — nothing is deleted,
 * what already landed stays, and adding the folder again asks about the rest —
 * so there is no confirm step.
 *
 * `aria-disabled`, not `disabled` — the Pagination rule the Pause button
 * records: this button holds focus when it is clicked, and disabling it on the
 * click's own commit strands keyboard focus on <body>. The click is swallowed
 * instead, and the stop is an idempotent 204 server-side so a slipped repeat is
 * harmless. That posture holds focus for the pending window; the end of the run
 * unmounts the control, and {@link useRunEndH1Focus} catches it there.
 *
 * `stopped` comes from the job, so a reload mid-stop reads pending too. A 404
 * or 409 resolves quietly in the hook (the run already ended, and the poll
 * shows it); only a transport failure reaches the sentence above the button. */
function StopRunButton({
  jobId,
  stopped,
}: Readonly<{ jobId: string; stopped: boolean }>) {
  const stop = useStopImport(jobId);
  const pending = stop.isPending || stopped;
  return (
    // The failure stacks ABOVE the button, the recipe {@link SweepRun} uses for
    // the same sentence beside the same mutation. Inline-left of the button was
    // a new placement, and it made the header's `shrink-0` slot as wide as
    // sentence + gap + button; stacked, the slot is only as wide as the
    // sentence. The header is `items-start`, so neither shape can move the h1.
    // `items-end` because this slot is the page's right-aligned one.
    <div className="flex flex-col items-end gap-1.5">
      {stop.isError && (
        <p className="text-destructive text-sm" role="alert">
          Couldn&rsquo;t stop. Try again.
        </p>
      )}
      <Button
        type="button"
        // Outline — the app's stop register. Every other Stop/Pause button is
        // outline (the sweep's Pause, in this file, on this mutation); `ghost
        // sm` is the navigation register here and all six of its other sites
        // are links. It also gives the control a boundary of its own, which is
        // what the glyph beside it no longer has to carry.
        variant="outline"
        size="sm"
        aria-disabled={pending}
        className="aria-disabled:opacity-50"
        onClick={() => {
          if (pending) return;
          stop.mutate();
        }}
      >
        <Stop aria-hidden="true" />
        {pending ? "Stopping…" : "Stop this run"}
      </Button>
    </div>
  );
}

/** What the live status line says INSTEAD of the counts, or null when the
 * counts own it. The feed's twin of {@link sweepStatusLabel}.
 *
 * An accepted stop wins: without it the header read "Stopping…" over a line
 * still counting and still naming the decision the run was parked on —
 * asserting work the run had left.
 *
 * It echoes the button's own word rather than expanding on it. The sweep's
 * pair adds information ("Pausing…" over "Pausing; finishing the current
 * album…"); a manual stop abandons the album it is on, so the longer sentences
 * that pair offers are not true here and the shortest honest line is the
 * button's word. Two visible nodes now carry it, so a test that wants this one
 * names its element.
 *
 * Then the empty feed: with nothing in it the count line reads "0 albums
 * imported", so say what is actually happening instead. */
function liveStatusCue(state: ImportJobState): string | null {
  if (state.stopped) return "Stopping…";
  return state.albums.length === 0 ? "Scanning your folder…" : null;
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
  const cue = liveStatusCue(state);
  // `progress` has no duplicate counter (backend), so derive the
  // duplicate-pending count from the feed rows for the cue line below. The
  // announcer's twin, not a second copy of the filter: the two disagreed about
  // a refused Replace on a bank apply, which wears `needs_dup_resolution` and
  // `did_not_land` at once.
  const needsDup = pendingDuplicates(state);
  return (
    <div className="flex flex-col gap-4">
      {/* Spinning through a stop as well: the server accepts it and the worker
          then unwinds, which is an active phase, and a parked run has `working`
          false — the one moment the line must not look idle. */}
      <StatusLine spinning={working || state.stopped}>
        {cue ?? (
          <>
            {/* No known total (the feed grows as the worker reads) — count
                what's applied + flag whether one album awaits a decision.
                `needs_review` is at most 1 (review is sequential), but derive
                the count so that invariant is self-evident.
                Every clause goes through {@link segment}, like both terminal
                panels: the rule is that a wrap may fall only BETWEEN segments,
                and a line that kept it for one clause in five was the same
                defect that rule was written for. */}
            {segment(
              `${state.progress.applied} ${state.progress.applied === 1 ? "album" : "albums"} imported`,
            )}
            {state.progress.skipped > 0 &&
              tailSegment(`${state.progress.skipped} skipped`)}
            {/* A refused Replace is lost the moment it is refused, and the
                server stops counting it as applied there and then — so without
                this the album simply left the line, and the headline read
                "0 albums imported" above a row saying nothing was imported.
                Same words as the two terminal panels ({@link notLandedText}). */}
            {state.progress.not_landed > 0 &&
              tailSegment(notLandedText(state.progress.not_landed))}
            {state.progress.needs_review > 0 &&
              tailSegment(
                `${state.progress.needs_review} album${state.progress.needs_review === 1 ? "" : "s"} needs review`,
              )}
            {/* Import-time duplicates are named "already in library" so
                "Duplicates" (the Manage page) names exactly one thing. */}
            {needsDup > 0 && tailSegment(`${needsDup} already in library`)}
          </>
        )}
        {/* Same middot dialect as the counts, inside the one text flow so it
            reads as part of the line rather than a second column. Shown
            throughout, parked included: it is the whole run's duration, not a
            "since last progress" gauge, so hiding it while a decision is owed
            would make it vanish and come back carrying the operator's own
            thinking time. */}
        <ElapsedSegment seconds={state.elapsed_seconds} />
      </StatusLine>

      {state.albums.length === 0 ? (
        <FeedSkeleton />
      ) : (
        <FeedList
          albums={state.albums}
          jobId={jobId}
          // `state.stopped` — the ACCEPTED flag, not `aborted`. This is the
          // live branch: from the moment the stop is accepted the parked slot
          // is released, so Review would open a decision the worker no longer
          // consumes, and whether the abort ends up landing is not settled
          // until the run is over. The header said the run was ending while the
          // row still offered a live button. The done panel reads `aborted`
          // instead, because by then the answer exists.
          readOnly={feedIsReadOnly(state.origin) || state.stopped}
        />
      )}
    </div>
  );
}

/** The sweep's whole progress surface: counters (StatTile, the cardless
 * stats dialect), the current folder, Pause, and the Review hand-off. Rides
 * the existing 1s job poll. A paused sweep finishes its current album, then
 * the job goes done with `sweep.stopped` still true — that flag is what titles
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
 * finished one, and the failed one alike.
 *
 * Counts of 1/2/4 — never 3, which would orphan the fourth tile beside three
 * empty columns. A StatTile spends 4.25rem of its column before the label
 * starts (size-14 icon + gap-3), and "Already known" is 92px at `text-sm`, so
 * a tile under ~10.25rem truncates it. The `grid-cols-2 sm:grid-cols-4` this
 * replaces clipped that label by 15px at 360, 29px at 640 and 54px at 768 —
 * the well is not monotonic in the viewport, since the sidebar opens at `md`
 * and takes 162px back. Thinnest headroom now is 8px, at 1024. Same base/sm/lg
 * shape as the dashboard's own TILE_GRID. */
function SweepTiles({ sweep }: Readonly<{ sweep: SweepStatus }>) {
  return (
    <div className="grid grid-cols-1 gap-6 sm:grid-cols-2 lg:grid-cols-4">
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
  if (sweep.stopped) return "Resume later by sweeping the same folder again.";
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
  // The same route the run page's Stop uses. A sweep asks its question at album
  // boundaries only and incremental history makes a re-sweep a resume, so this
  // surface still says "pause".
  const pause = useStopImport(jobId);
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
          icon={sweep.stopped ? Pause : Success}
          title={sweep.stopped ? "Sweep paused" : "Sweep finished"}
          // The tiles below ARE the counts, and they are the app's own labels.
          // This body used to restate all four ~24px above them in a server-
          // built string ("swept 30, auto-applied 20, …"), so the panel said
          // every number twice. What the tiles cannot say is how long it took
          // and what to do next; that is all this body is now. The pause is
          // still in the title and on `sweep.stopped`, so the word is not
          // repeated either.
          body={sweepDoneBody(sweep, state.elapsed_seconds)}
          // The CTA points at what the run actually produced. Banked albums
          // are decisions waiting, so they win. A sweep that only auto-applied
          // has no feed of its own and nothing to review, and at the time this
          // was written the shell carried its own /import link, so sending it
          // there said the same thing twice — it goes to the library instead,
          // newest first (`sort=added` is descending by date added). Nothing
          // produced falls through to a fresh run.
          action={<SweepDoneCta sweep={sweep} />}
        />
      ) : (
        // A sweep is never idle while this branch renders — it runs until a
        // terminal phase, which the branch above owns — so the spinner spins
        // throughout, unlike the feed's.
        <StatusLine spinning>
          {sweepStatusLabel(sweep.stopped, sweep.current_folder)}
          {/* A sweep is the longest-running import there is and it returns
              before LiveFeed ever renders, so this is the only place its
              duration shows while it runs. Same threshold and middot dialect
              as the feed's status line. */}
          <ElapsedSegment seconds={state.elapsed_seconds} />
        </StatusLine>
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
              // `aria-disabled`, never `disabled` — the Pagination rule: this
              // button holds focus when it is clicked, and disabling it on the
              // click's own commit strands keyboard focus on <body> (measured:
              // the next Tab restarts at "Skip to content"). The Review page's
              // Pause, the same mutation on the same state, already reads this
              // way; the click is swallowed instead, and pause is an idempotent
              // 204 server-side so a slipped repeat is harmless.
              aria-disabled={pause.isPending || sweep.stopped}
              className="aria-disabled:opacity-50"
              onClick={() => {
                if (pause.isPending || sweep.stopped) return;
                pause.mutate();
              }}
            >
              <Pause aria-hidden="true" />
              {/* Same expression as the state above: keyed on `sweep.stopped`
                  alone the button read "Pause sweep" while already inert for
                  the whole in-flight window. */}
              {pause.isPending || sweep.stopped ? "Pausing…" : "Pause sweep"}
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

/** Can a decision posted from this feed reach a worker?
 *
 * No, on a bank-apply run: the banked decision IS the answer, so the session
 * answers every hook from its directive and never parks
 * (`import_session.py` `_directive_choice`), and `ReviewPage.tsx` excludes the
 * same origin from its decision list. The entry screen's Resume links
 * `/import?job=<id>` whatever the origin, so such a feed is reachable here and a
 * `Resolve` button would open a decision nothing consumes — the dead end
 * {@link JobFailed} passes `readOnly` to avoid. Rows keep their badge and note.
 *
 * `origin` is the server's, not a guess from the rows: a directive run's
 * duplicate row reads `needs_dup_resolution` exactly like a parked one. */
function feedIsReadOnly(origin: ImportJobState["origin"]): boolean {
  return origin === "bank_apply";
}

/** The feed listing — shared by the live run and the terminal panels. Carries
 * the `jobId` so each row's links can thread the run origin. `readOnly` drops
 * the per-row decision buttons, in the three cases where nothing consumes a
 * choice: a failed job, a bank-apply run ({@link feedIsReadOnly}), and a run
 * whose stop has been accepted. It also drops the badge's fill — see
 * {@link badgeVariant}. */
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

/** The row's own problem line: why a Replace the user asked for imported
 * nothing (`ImportAlbumSummary.note`, up to three short sentences from the
 * backend). Null on every other row.
 *
 * The page's dialect for a row-level problem is SettingsTrashPage's
 * `RestoreOutlook` warning arm (`SettingsTrashPage.tsx:211-247`): an amber
 * `Warning` glyph beside `text-muted-foreground text-xs`, with the WORDS
 * carrying the meaning so the colour is never the only signal. Its bold run-in
 * label is dropped — these notes carry their own verdict.
 *
 * `min-w-0` on the inner span is load-bearing: it is a flex item of the <p>, so
 * it takes its floor from its longest unbreakable token (measured at
 * `SettingsTrashPage.tsx:220-227`).
 *
 * No `role`/live region: the feed is polled list content read in order with its
 * row, and the page already owns one `role="status"` for the run.
 *
 * `mx-4`, so the glyph starts at the artwork's left edge (x=16) rather than the
 * title's (x=68 = px-4 16 + cover 40 + gap-3 12), which is also the inset the
 * dropped action below uses (`ml-4`).
 *
 * `col-span-full row-start-2` because the wrapper is a grid whenever this line
 * renders — see the placement note in {@link FeedRow}. Full-width, not
 * `col-start-1`: at 28rem and up the action sits in column 2, and a note
 * confined to column 1 would stop short of a track it may run under. */
function ReplaceNote({ note }: Readonly<{ note: string }>) {
  return (
    <p className="text-muted-foreground col-span-full row-start-2 mx-4 mb-3 flex min-w-0 items-start gap-1.5 text-xs">
      <Warning className="text-warning mt-0.5 size-3.5 shrink-0" aria-hidden="true" />
      {/* `break-words` (overflow-wrap) chooses where LINES break; it does not
          lower this span's min-content floor, which is `min-w-0`'s job. Both
          are needed and neither substitutes: without the floor a long token
          pans the page, and without the wrap the list's `overflow-hidden`
          CLIPS it. Every note the backend can send today is prose — measured
          across the `_REPLACE_*` constants, `_replace_partial_note` and the
          `LibraryRootUnavailableError` sentences `_replace_refused` forwards,
          the longest unbreakable token is 12 characters ("unavailable.") — so
          this is insurance for the `strerror` those sentences interpolate. */}
      <span className="min-w-0 break-words">{note}</span>
    </p>
  );
}

/** One feed row on the shared AlbumRow. An `applied` album shows its library
 * cover and its title links to `/albums/{id}` (when the worker reported the
 * id); `skipped`/`decided` rows are calm; `needs_review` / parked-duplicate
 * rows are highlighted and offer Review / Resolve, threading the run origin.
 *
 * A row can also carry a `note` — a refused Replace. The note does not touch the
 * row's STATUS, but the server flags such a row `did_not_land` in every phase,
 * so the badge reads the destructive "Didn't land" from the moment of the
 * refusal and the note under it says why. Nothing here reads `note` to decide a
 * status; that is the server's call and the FE must not second-guess it. */
function FeedRow({
  album,
  jobId,
  readOnly = false,
}: Readonly<{
  album: ImportAlbumSummary;
  jobId: string;
  readOnly?: boolean;
}> ) {
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
  const action = readOnly
    ? undefined
    : feedRowAction(album.status, album.index, jobId, origin);
  const note = album.note ?? null;
  return (
    // Highlight stays with the caller (AlbumRow contract).
    //
    // Under 28rem of ROW the action drops to its own line under it
    // (decisions 39, extended to this feed by the owner 2026-09-11). The
    // defect is the decision row's, measured here: the title is 0px wide at
    // viewport 320→344 on a parked-duplicate row and at 320→328 on a
    // needs_review one. The grid arrives ONLY on a row that carries a button,
    // so every other feed row keeps the plain block box it has today.
    //
    // 28rem is the owner's number (2026-09-11), one threshold for all three
    // rows. The floor is still 296.70 — fixed content 285.37px (px-4 16 +
    // cover 40 + gap-3 12 + "Already in library" 107.98 + gap-2 8 + gap-3 12
    // + Resolve 73.39 + px-4 16) plus an 11.33px ellipsis glyph — and it is
    // true AS a floor, but it is not what sets the switch. At 20rem the real
    // phone band went inline and the title collapsed: viewport
    // 392/400/414/430 measured 42/50/66/74px of title, 6/7/10/10 characters
    // of a 46-character album. At 28rem the same widths give
    // 127/135/151/159px, 18/19/22/23 characters. The ceiling is the 473px
    // narrowest desktop row, at the 768px sidebar step, which 448 clears — so
    // no desktop row ever drops. The switch measures at viewport 513 (row
    // 448); 512 (row 447) is the last dropped width.
    //
    // A grid, like both /review rows: `items-center` then centres each item
    // in its OWN row track, so the dropped line cannot re-centre anything
    // above it.
    //
    // A note takes row 2, and takes the grid for the same reason: as a plain
    // block sibling it would be a second line inside the row's box, which
    // re-centres every `items-center` neighbour beside it.
    //
    // A row can carry BOTH: the note is attached without touching the status,
    // which on a directive (bank-apply) run is `needs_dup_resolution`, for which
    // `feedRowAction` returns Resolve. Two explicitly placed grid items in one
    // area paint over each other, so the action takes row 3 in the narrow arm
    // whenever a note is present — conditionally, since a row with no note must
    // keep the exact tracks it took before.
    //
    // The tint is keyed on the ACTION, not on the status: it means "this row
    // needs you", and a read-only feed has nothing to press — a bank-apply
    // duplicate wore it with no control, and so did every parked row on a
    // failed job.
    <div
      className={cn(
        action !== undefined && "bg-primary/5",
        (action !== undefined || note !== null) &&
          "@container/feedrow grid grid-cols-[minmax(0,1fr)_auto] items-center",
      )}
    >
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
        badge={<StatusBadge album={album} readOnly={readOnly} />}
        href={linked ? `/albums/${albumId}` : undefined}
        hrefState={linked ? origin : undefined}
      />
      {/* Before the action in the DOM, so a screen reader meets the row, then
          why it refused, then whatever is left to do about it. */}
      {note !== null && <ReplaceNote note={note} />}
      {action !== undefined && (
        // Not AlbumRow's `action` slot: from inside it the button cannot take
        // a line of its own without growing AlbumRow's box. `-ml-1` gives back
        // the 4px by which AlbumRow's px-4 exceeds its own gap-3, so the
        // inline arm keeps today's 12px gap and 16px inset.
        //
        // The narrow arm's row is the note's only coupling to this box: row 2
        // is the note's when there is one. The wide arm is untouched either
        // way — there the action is in column 2 of row 1 and the note spans
        // row 2 beneath it.
        <div
          className={cn(
            "col-start-1 mb-3 ml-4 flex items-center @min-[28rem]/feedrow:col-start-2 @min-[28rem]/feedrow:row-start-1 @min-[28rem]/feedrow:mb-0 @min-[28rem]/feedrow:-ml-1 @min-[28rem]/feedrow:mr-4",
            note !== null ? "row-start-3" : "row-start-2",
          )}
        >
          {action}
        </div>
      )}
    </div>
  );
}

/** Derive the badge variant from the album's per-status label.
 *
 * The primary fill is the app's "needs you" signal, so a read-only feed takes
 * the outline instead: the words stay true — that album really was never decided
 * — but the colour claims an action the row no longer offers. Scoped to the
 * feeds that have no button at all ({@link FeedList}'s `readOnly`), so a live
 * row keeps the cue that pairs with its Review button. */
function badgeVariant(
  status: ImportAlbumSummary["status"],
  readOnly: boolean,
): "default" | "outline" | "secondary" {
  if (status === "needs_review" || status === "needs_dup_resolution") {
    return readOnly ? "outline" : "default";
  }
  if (status === "skipped") return "outline";
  return "secondary";
}

/** The status chip. Color + the text label both carry the state (not color
 * alone). `needs_dup_resolution` reads "Already in library" — matching the
 * resolve screen's heading, so "Duplicates" names only the library finder.
 *
 * Precedence over the raw status (both landing outcomes the status alone can't
 * express): a row that never landed flags the failure destructively — the flag
 * is the server's, in every phase, so a refused Replace reads this way from the
 * moment it is refused and not only once the job ends; else a row that DID
 * land (an album_id arrived) reads as the positive "Imported" chip, upgrading a
 * user-decided Apply from the vague "Decided"; else the per-status label. */
function StatusBadge({
  album,
  readOnly,
}: Readonly<{ album: ImportAlbumSummary; readOnly: boolean }>) {
  if (album.did_not_land) {
    return (
      <Badge variant="destructive" className="shrink-0">
        Didn&rsquo;t land
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
    <Badge variant={badgeVariant(status, readOnly)} className="shrink-0">
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
 * Owns up to albums that were decided/applied but never landed in the library —
 * the session died before beets ran task.add, or a Replace the user asked for
 * was refused and answered SKIP. The second kind is known the moment it
 * happens, so `not_landed` can be nonzero while the run is still going; the
 * clause drops out of a clean run, not out of a live one.
 *
 * `already_known` is the run's history skips — beets skips those folders before
 * tagging, so they reach no outcome record and `skipped` does not hold them.
 * "Already known" is the sweep tile's word for the same number.
 *
 * The apostrophe is the page's typographic one. `importStatus`' spoken twin
 * keeps the straight one; that string is never seen. */
function countsLine(progress: ImportProgress): string {
  const { applied, skipped, not_landed, already_known } = progress;
  return (
    segment(`${applied} ${applied === 1 ? "album" : "albums"} imported`) +
    SEGMENT_SEP +
    segment(`${skipped} skipped`) +
    (not_landed > 0 ? SEGMENT_SEP + segment(notLandedText(not_landed)) : "") +
    (already_known > 0
      ? SEGMENT_SEP + segment(`${already_known} already known`)
      : "")
  );
}

/** The lost-album segment's words, one source for all three surfaces that owe
 * them: the finished panel, the failed panel and — since a refused Replace is
 * lost the moment it is refused — the LIVE status line. Typographic apostrophe,
 * like every visible string on this page; `importStatus`' spoken twin keeps the
 * straight one. */
function notLandedText(notLanded: number): string {
  return `${notLanded} didn’t land`;
}

/** One segment of a counts line, made unbreakable: a wrap may fall only
 * BETWEEN segments, never between a number and the words it counts.
 *
 * SEGMENT_SEP already owns the other half of the rule — its ordinary space is
 * the only break point and its trailing one is non-breaking, so a wrapped line
 * opens with the middot, which is this page's dialect. What was missing is the
 * inside of a segment: measured in Chromium (Orca) at 360px and 320px,
 * 2026-09-18, the finished panel broke as "0 albums imported · 0 skipped · 1" /
 * "already known".
 *
 * Every clause of all three counting lines goes through this — the two terminal
 * panels and the live status line. The live one had it on one clause of five
 * for a round, which is the state the rule exists to forbid. Adding the other
 * four cost no test changes: RTL normalises NBSP in the ELEMENT, so a query
 * written with ordinary spaces still matches.
 *
 * Visible text only. The spoken twins in `importStatus.ts` keep ordinary
 * spaces — nothing wraps inside a live region. */
function segment(text: string): string {
  return text.replaceAll(" ", "\u00A0");
}

/** A counts clause that is not the first: the separator plus {@link segment}.
 * Both halves of the rule in one call, so a clause added later cannot take one
 * and forget the other \u2014 which is how the live line ended up with four
 * breakable clauses and one unbreakable. */
function tailSegment(text: string): string {
  return `${SEGMENT_SEP}${segment(text)}`;
}

/** The failed panel's count line, or null when the run has nothing to own up to.
 *
 * The imported/skipped pair is gated on ITSELF, not on the three-way sum: a run
 * that landed and skipped nothing but lost five albums opened on
 * "0 albums imported · 0 skipped", exactly the noise the early-crash branch
 * exists to avoid. {@link JobDone} keeps the unconditional pair — a finished run
 * has landed/skipped counts worth stating even at zero.
 *
 * So the two trailing counts are gated on themselves here, the same shape the
 * failed ANNOUNCEMENT uses (`importStatus.ts` `failedMessage`): a run whose only
 * news is a history skip says it, rather than being announced a number the panel
 * never shows. */
function failedCountsLine(progress: ImportProgress): string | null {
  const { applied, skipped, not_landed, already_known } = progress;
  if (applied + skipped > 0) return countsLine(progress);
  const rest = [
    not_landed > 0 ? segment(notLandedText(not_landed)) : null,
    already_known > 0 ? segment(`${already_known} already known`) : null,
  ].filter((part) => part !== null);
  return rest.length > 0 ? rest.join(SEGMENT_SEP) : null;
}

/** "The run did nothing but skip folders beets' import history already has."
 *
 * ONE predicate for the two things this panel says about such a run — its title
 * ({@link doneTitle}) and whether it offers the folder again
 * ({@link importAgainPath}). They read the same outcome with different terms
 * before: the title counted `not_landed`, the button did not.
 *
 * `progress` does NOT partition the run. A set-aside row (`needs_review` /
 * `needs_dup_resolution`) is refused by the server's `_is_imported` AND its
 * `_is_skipped`, and it never landed, so it sits in none of the three counters —
 * `state.set_aside` is its count ({@link JobFailed} says the same of its own
 * line). Without that term a finished unattended run with an album set aside was
 * titled "Nothing new to import" directly above the row holding its Review
 * button.
 *
 * The status does not decide the bucket on its own: a row carrying a `note` was
 * answered SKIP and can never land, so the server counts it in `not_landed` and
 * leaves it out of `set_aside` — including the bank-apply row that wears
 * `needs_dup_resolution`. */
function onlySkippedKnown(state: ImportJobState): boolean {
  const { applied, skipped, not_landed, already_known } = state.progress;
  return (
    already_known > 0 && applied + skipped + not_landed + state.set_aside === 0
  );
}

/** The folder a finished run can offer again, or null when it cannot.
 *
 * Only a run that did nothing BUT skip known folders ({@link onlySkippedKnown}):
 * with `keep downloads` on, MusicDrop turns beets' import history on for runs
 * that leave the files in place, so re-adding a kept folder skips every album it
 * already imported. That dead-ends a folder whose album has since left the
 * library, and the way past it is beets' own `-I` ({@link ImportAgainButton}).
 * In a mixed run the user picks the album's own folder instead (design note 13),
 * so offering the parent here would re-import what just landed.
 *
 * `origin === "manual"` is the review-run half — the only origin this button's
 * attended re-run matches. `path` is null for a multi-folder start (the inbox
 * hands over several toppaths), and a sweep never reaches this panel. Those two
 * are the button's own terms, so an all-known inbox or multi-folder run still
 * takes the title above with nothing to press. */
function importAgainPath(state: ImportJobState): string | null {
  if (state.origin !== "manual" || state.path == null) return null;
  return onlySkippedKnown(state) ? state.path : null;
}

/** The id linking the failure sentence to the button it belongs to. One panel,
 * one button, so a constant is enough. */
const IMPORT_AGAIN_ERROR_ID = "import-again-error";

/** Re-import the run's folder past beets' import history (`incremental: false`
 * is beets' `-I`). Moves into the new job the way the entry screen does — the
 * URL's `?job=` is the only run pointer — so this panel is replaced by the live
 * feed. A failure keeps the panel and says why; without that the button would
 * dead-end on the one error it is most likely to hit (the import slot).
 *
 * The sentence comes from {@link startErrorSentence}, so a 409 names the reason
 * the server gave — three different ones share that status, and naming a running
 * import for a backfill left the user waiting for something that was not
 * happening. No "use Resume above" in the generic half either: this panel has no
 * resume banner, and a sentence must name a control the screen has.
 *
 * `known` is only the label's number. The predicate that offers the button at
 * all is {@link importAgainPath}. */
function ImportAgainButton({
  path,
  known,
}: Readonly<{ path: string; known: number }>) {
  const [, setSearchParams] = useSearchParams();
  const start = useStartImport();
  const failure = startErrorSentence(
    start.error,
    start.isError,
    "Couldn’t start. Try again.",
  );
  // The count is on the line above, so the label only has to agree with it in
  // number.
  const label = known === 1 ? "Import it again" : "Import them again";
  return (
    <div className="flex flex-col items-center gap-2">
      {failure !== null && (
        // `w-full` as well as `break-words`: the column is `items-center`, so
        // this <p> is sized fit-content and its min-content width would become
        // the page's — a definite width is what a cross-axis item needs
        // (SettingsTrashPage.tsx has the 320px numbers).
        <p
          id={IMPORT_AGAIN_ERROR_ID}
          className="text-destructive w-full text-sm break-words"
          role="alert"
        >
          {failure}
        </p>
      )}
      <Button
        type="button"
        variant="outline"
        size="sm"
        // `aria-disabled`, never `disabled` — the Pagination rule the Pause
        // button above records: this button holds focus when it is clicked, and
        // disabling it on that commit strands keyboard focus on <body>. It is
        // the failure path that needs it, and that path's copy says "try again".
        // The click is swallowed instead.
        aria-disabled={start.isPending}
        className="aria-disabled:opacity-50"
        // The alert is the button's description while it is up, so a keyboard
        // user who comes back to the button hears why the last press failed.
        aria-describedby={failure !== null ? IMPORT_AGAIN_ERROR_ID : undefined}
        onClick={() => {
          if (start.isPending) return;
          start.mutate(
            {
              path,
              // `incremental: false` is beets' own `-I`. The other three fields
              // are the manual default this run already used; the generated
              // ImportOptions marks them required.
              options: {
                operation: "default",
                unattended: false,
                sweep: false,
                incremental: false,
              },
            },
            { onSuccess: (data) => setSearchParams({ job: data.job_id }) },
          );
        }}
      >
        {start.isPending ? (
          <>
            <Spinner className="animate-spin" aria-hidden="true" />
            Starting&hellip;
          </>
        ) : (
          label
        )}
      </Button>
    </div>
  );
}

/** The finished panel's title. A success check over "0 albums imported · 0
 * skipped" reads as a silent failure, so a run whose whole story is the history
 * skips says that instead — and only such a run.
 *
 * Shares {@link onlySkippedKnown} with the button below, so the title cannot
 * claim nothing happened over a feed that lists an album. An all-known
 * multi-folder start or inbox run still reads this way with nothing to press:
 * those are the button's own terms, not the outcome's. */
function doneTitle(state: ImportJobState): string {
  // `aborted`, not `stopped` — the flag that says the stop actually ended the
  // run early (backend `registry.py`, from the worker's own abort event). A
  // stop accepted while the last album was being placed sets `stopped` and
  // never reaches an abort point: the run finished, so this titles it as one.
  //
  // It wins over both of the arms below: the person pressed a button and is
  // owed the acknowledgement, and a run cut short early enough to have nothing
  // but history skips behind it would otherwise be titled "Nothing new to
  // import" — true of the counters, false about the run.
  if (state.aborted) return "Import stopped";
  return onlySkippedKnown(state) ? "Nothing new to import" : "Import finished";
}

/** A finished bank apply that left something to decide again, or still holds a
 * set-aside row — the one origin whose feed is read-only, so nothing on this
 * page can clear either.
 *
 * The decision lives on the Review page's bank row, which carries the runner's
 * re-decide guidance. Without this the panel's only link was the shell's "Start
 * over" → `/import`, a folder import, while a row said "Decide again."; the
 * file's own rule is that a sentence must name a control the screen has.
 *
 * Same button and words as {@link SweepDoneCta}'s banked arm — the other place
 * a finished run hands the user to `/review`. A bank apply that landed
 * everything gains nothing. */
function bankApplyNeedsReview(state: ImportJobState): boolean {
  return (
    state.origin === "bank_apply" &&
    state.progress.not_landed + state.set_aside > 0
  );
}

/** A run cut short whose remedy this page can name: the manual one.
 *
 * Two terms, and each drops a different false promise.
 *
 * `aborted` rather than `stopped`: "The rest stayed in the folder" needs there
 * to BE a rest. A stop accepted after the last abort point leaves nothing
 * behind, and pointing that user back at an empty folder is the bug this
 * predicate was re-keyed to fix.
 *
 * And the origin, because the sentence is only true where the folder IS the run
 * and nothing picks up behind it — the same origin the control is offered for
 * ({@link offersStop}). The API accepts a stop on any origin, so an aborted
 * inbox or bank-apply job can reach this panel without the button ever having
 * been shown; it keeps the title and the read-only feed, which are true of any
 * abort, and not the remedy, which is not. */
function abortedFromThisPage(state: ImportJobState): boolean {
  return state.aborted && offersStop(state.origin);
}

/** The stop that changed nothing: accepted, but the run reached its own end
 * before the worker could act on it, so every album it had went in.
 *
 * The panel is the finished one — title, glyph, counts, no remedy — because
 * that is what the run did. This adds the one thing the finished panel cannot
 * say: that the press was seen. Without it the only answer the presser gets is
 * a panel identical to one they never touched, which reads as the button having
 * been ignored.
 *
 * Origin-gated for the same reason the remedy is: a stop posted through the API
 * on an inbox or bank-apply run had no presser on this page to answer. */
function stopCutNothingShort(state: ImportJobState): boolean {
  return state.stopped && !state.aborted && offersStop(state.origin);
}

/** done: a legible outcome — imported/skipped counts (counting auto-applied
 * albums) + the feed list, whose applied rows now link straight to their
 * library pages (replaces the old blanket "View in library", spec §1). */
function JobDone({ state, jobId }: Readonly<{ state: ImportJobState; jobId: string }>) {
  const againPath = importAgainPath(state);
  // Three arms, at most one button. The first two are mutually exclusive by
  // origin — `bankApplyNeedsReview` is `bank_apply` only, `abortedFromThisPage`
  // is `manual` only — so neither can hide the other.
  //
  // The abort outranks `againPath`, the same order {@link doneTitle} takes and
  // for the same reason: a run cut short has not established that there is
  // nothing new, it just did not get that far. Both read the counters, and a run
  // cut short before it reached an unknown album has only history skips behind
  // it — which offered "Import them again" (beets' `-I`) under a sentence saying
  // to add the folder again, i.e. re-importing the albums already in the library
  // the sentence was telling the user to leave alone.
  let doneAction: React.ReactNode | undefined;
  if (bankApplyNeedsReview(state)) {
    doneAction = (
      // Outline once the run was cut short, for the reason the arm below gives:
      // a solid CTA under "Import stopped" reads as a success panel. On
      // `aborted`, so an apply whose stop arrived too late keeps the solid CTA
      // its panel earned. The link itself stays either way — the banked rows are
      // owed to Review whichever way the apply ended.
      <Button variant={state.aborted ? "outline" : undefined} size="sm" asChild>
        <Link to="/review">Review banked albums</Link>
      </Button>
    );
  } else if (abortedFromThisPage(state)) {
    // The sentence below names this control. Outline, like the failed panel's:
    // a solid CTA under a run cut short would read as a success panel.
    doneAction = (
      <Button variant="outline" size="sm" asChild>
        <Link to="/import">Add from folder</Link>
      </Button>
    );
  } else if (againPath !== null) {
    doneAction = (
      <ImportAgainButton path={againPath} known={state.progress.already_known} />
    );
  }
  return (
    <div className="flex flex-col gap-4">
      <EmptyState
        bordered
        // A run the stop actually ended is not a completion, so it does not
        // wear the success check — the same reading {@link SweepRun} gives a
        // paused sweep, with the app's Stop concept naming what happened. A run
        // that finished before its stop landed keeps the check: it finished.
        icon={state.aborted ? Stop : Success}
        title={doneTitle(state)}
        body={
          <>
            {/* The finished summary carries the run's duration too (the owner's
                ruling): the number counts the whole run, so it must not vanish
                at the finish line. */}
            {countsLine(state.progress)}
            <ElapsedSegment seconds={state.elapsed_seconds} />
            {abortedFromThisPage(state) && (
              // What an abort leaves behind, and the way past it. The counts
              // above cannot say either: an album the run never reached has no
              // feed row and is in no counter. Manual only — see the predicate.
              <span className="mt-1 block">
                The rest stayed in the folder. Add it again to continue.
              </span>
            )}
            {stopCutNothingShort(state) && (
              // The press, answered — see the predicate. No remedy clause
              // because there is nothing to remedy, and no claim about what
              // landed: the counts line above already says that.
              <span className="mt-1 block">Nothing was left to stop.</span>
            )}
          </>
        }
        action={doneAction}
      />
      {state.albums.length > 0 && (
        <FeedList
          albums={state.albums}
          jobId={jobId}
          // Read-only after an abort, for the reason {@link JobFailed} gives
          // its own feed: the worker left mid-run, so a Review/Resolve button
          // would open a decision whose POST 404s. A row left `needs_review`
          // keeps its badge — it IS still undecided — it just stops offering an
          // action this page cannot carry out. An unattended run that finished
          // on its own is untouched: its set-aside row is still live work, and
          // a run whose stop arrived after the last album is one of those.
          readOnly={feedIsReadOnly(state.origin) || state.aborted}
        />
      )}
    </div>
  );
}

/** failed: the worker's error, and what the run still earned before it died.
 *
 * A crash mid-apply is exactly when albums land or fail to land, so the server
 * keeps reporting this job's counters, and a terminal job is where the last of
 * them are settled into `not_landed` — a sweep that banked 200 albums and then
 * died must not read as though nothing happened. Sweeps count on `state.sweep`
 * (their `albums` stays
 * empty by design); every other origin counts on `state.progress` and carries
 * the feed rows.
 *
 * `progress` does not partition the run. A set-aside row (`needs_review` /
 * `needs_dup_resolution`) is refused by the server's `_is_imported` AND its
 * `_is_skipped`, and never landed, so it falls out of all three counters — and
 * on a failed job its Review button is gone. `state.set_aside` is the count, and
 * it gets its own sentence: a failed run must not silently drop a category of
 * album it is still holding. A row carrying a `note` is the one exception, and
 * the server owns it — see {@link onlySkippedKnown}.
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
          // panel. A non-sweep failure has no feed to send anyone to, so /import
          // is the way on, under the one label the page uses for it: the nav
          // item's and the h1's own words, which the stopped and not-found
          // panels also carry.
          sweep !== null ? (
            <SweepDoneCta sweep={sweep} variant="outline" />
          ) : (
            <Button variant="outline" size="sm" asChild>
              <Link to="/import">Add from folder</Link>
            </Button>
          )
        }
      />
      {sweep !== null && <SweepTiles sweep={sweep} />}
      {/* Read-only, whatever the origin. The rows are the record of what landed
          and what didn't, but the worker is gone: a Review/Resolve button here
          would open a decision whose POST no worker will consume
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
      // The closing clause names the button under it, so it takes the button's
      // words — one label for /import across this page's three terminal panels.
      body="It may have finished in another session, or the server restarted. Add the folder again to continue."
      action={
        <Button variant="outline" size="sm" asChild>
          <Link to="/import">Add from folder</Link>
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
