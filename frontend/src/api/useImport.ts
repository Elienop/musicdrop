import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { client } from "@/api/client";
import { detailMessage, unwrap } from "@/api/lib";
import type { components } from "@/api/schema";

/** Job state + live feed as returned by `GET /api/import/{job_id}` (generated). */
export type ImportJobState = components["schemas"]["ImportJobState"];
/** One row in the live feed (generated contract). */
export type ImportAlbumSummary = components["schemas"]["ImportAlbumSummary"];
/** Coarse lifecycle phase (generated contract). */
export type ImportPhase = components["schemas"]["ImportPhase"];
/** Per-album feed status (generated contract). */
export type ImportAlbumStatus = components["schemas"]["ImportAlbumStatus"];
/** Match recommendation tier (generated; mirrors beets' Recommendation enum). */
export type Recommendation = components["schemas"]["Recommendation"];
/** Coarse progress counters from `GET /api/import/{job}` (generated contract). */
export type ImportProgress = components["schemas"]["ImportProgress"];
/** Body of `POST /api/import` (generated contract). */
export type StartImportRequest = components["schemas"]["StartImportRequest"];
/** Response of a successful `POST /api/import` (generated contract). */
export type StartImportResponse = components["schemas"]["StartImportResponse"];
/** The full per-album review payload (generated; chunk 4 renders it). */
export type Candidate = components["schemas"]["Candidate"];
/** A user's decision for one parked album (generated contract). */
export type ImportChoice = components["schemas"]["ImportChoice"];
/** Re-lookup parameters carried by a search choice (generated contract). */
export type ImportSearch = components["schemas"]["ImportSearch"];
/** A parked import album that duplicates one already in the library (generated). */
export type DuplicatePrompt = components["schemas"]["DuplicatePrompt"];
/** The user's resolution for a parked duplicate (generated contract). */
export type DuplicateDecision = components["schemas"]["DuplicateDecision"];
/** beets' four duplicate actions (generated; mirrors the backend enum). */
export type DuplicateAction = components["schemas"]["DuplicateAction"];
/** The per-track library-vs-import comparison shown on a duplicate prompt. */
export type MergePreview = components["schemas"]["MergePreview"];
/** One release position compared across the library copy and the import. */
export type DuplicateTrackRow = components["schemas"]["DuplicateTrackRow"];
/** Live sweep counters on a sweep-origin job/probe (generated contract). */
export type SweepStatus = components["schemas"]["SweepStatus"];
/** The last finished sweep's recap on the active probe (generated contract). */
export type FinishedSweep = components["schemas"]["FinishedSweep"];

/** Humanized labels for beets' recommendation levels (shared by the feed +
 * the review screen). Keeps the raw enum ("strong"/"none") out of the UI. */
export const RECOMMENDATION_LABEL: Record<Recommendation, string> = {
  none: "No match",
  low: "Low match",
  medium: "Medium match",
  strong: "Strong match",
};

/** URL of the current files' embedded cover for a parked album (served by the
 * backend; the browser falls back to a placeholder on 404). */
export function importCoverUrl(jobId: string, index: number): string {
  return `/api/import/${jobId}/albums/${index}/cover`;
}

/** Thrown when `POST /api/import` answers 409. Lets the entry screen surface a
 * message with a link to the running import, instead of the generic failure.
 *
 * Carries the server's own sentence, because the status has three reasons
 * (backend/app/api/import_.py `ensure_import_can_start` + the registry's
 * single-slot refusal): an import already running, a held beets swap lock, or a
 * running backfill / disk sync. The fallback is the sentence true for all
 * three. */
export class ImportConflictError extends Error {
  constructor(detail?: string | null) {
    super(detail ?? "The library is busy. Try again shortly.");
    this.name = "ImportConflictError";
  }
}

/** Thrown when `POST /api/import` answers 503 WITH a reason: the attached
 * library sits under a layout Apply refused, so no import can start. Carries
 * the refusal's own sentence — a "try again" is false for it, since nothing
 * changes until the layout does.
 *
 * `detail` is REQUIRED, so this class can only speak for a 503 our route sent:
 * the route's own always carries a sentence (backend/app/api/import_.py
 * `detail=str(exc)`), so a bodyless one came from a proxy answering for a
 * restarting container, where "try again" IS the right advice. That one falls
 * through to the caller's generic sentence, the same as a 502. */
export class ImportUnavailableError extends Error {
  constructor(detail: string) {
    super(detail);
    this.name = "ImportUnavailableError";
  }
}

/** Raise {@link ImportUnavailableError} for a 503 that carries the server's own
 * sentence, and do nothing otherwise — so a caller keeps `unwrap`'s generic
 * message for every other outcome. The two inbox routes refuse with the same
 * sentences `POST /api/import` does ("Is the music share mounted?"), and "try
 * again in a moment" is false advice for those: nothing changes until the share
 * comes back. The bodyless-503 rule is the class's, above. */
export function throwIfUnavailable(result: {
  error?: unknown;
  response: Response;
}): void {
  if (result.response.status !== 503) return;
  const reason = detailMessage(result.error);
  if (reason !== null) {
    throw new ImportUnavailableError(reason);
  }
}

/** Thrown when a start is rejected with a 422 (e.g. the in-library guard
 * refusing copy-mode). Carries the backend's reason. The detail body is read
 * through detailMessage: our guards send `{detail: string}` while the OpenAPI
 * schema declares the array shape — both must surface (carry-forward). */
export class ImportStartRejectedError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ImportStartRejectedError";
  }
}

/** The one sentence a failed start can take, for every surface that starts an
 * import. A refusal (409 / 422 / 503) carries the server's own reason; anything
 * else takes the caller's `generic` sentence, which is the only part that
 * differs between call sites. Returns null while there is no error.
 *
 * One helper, not one per page: the finished panel and the Bank's stale row each
 * held a copy of these branches. The entry screen keeps its own conflict copy —
 * it has the Resume banner and the active-import probe, so it can name the
 * control. */
export function startErrorSentence(
  error: unknown,
  isError: boolean,
  generic: string,
): string | null {
  if (
    error instanceof ImportConflictError ||
    error instanceof ImportStartRejectedError ||
    error instanceof ImportUnavailableError
  ) {
    return endStopped(error.message);
  }
  return isError ? generic : null;
}

/** A carried server sentence with a full stop, when it ends in no terminal
 * punctuation of its own.
 *
 * The two conventions differ: the literal `detail=` strings under
 * `backend/app/api/` were counted at 47 without terminal punctuation to 5 with,
 * while every client sentence in this app has one. Rather than churn the 47, the
 * join is normalised here, where all three refusals pass through. */
function endStopped(sentence: string): string {
  return /[.!?…]$/u.test(sentence) ? sentence : `${sentence}.`;
}

/** Thrown when the polled job id is unknown or expired (backend 404). Lets the
 * run page show a dedicated "no longer available" notice and STOP polling,
 * rather than hammering the 404 every second. */
export class ImportJobNotFoundError extends Error {
  constructor() {
    super("Import job not found");
    this.name = "ImportJobNotFoundError";
  }
}

/** Thrown when a parked album's candidate is no longer available (backend 404 —
 * already decided, or the worker advanced). Distinct from a transient transport
 * failure so the review screen can show the calm "already decided" notice for a
 * true 404 while STILL surfacing a retryable error for a 5xx/network blip (which
 * `retry: false` would otherwise mislabel as "gone"), and stop any live poll. */
export class CandidateNotFoundError extends Error {
  constructor() {
    super("Import candidate not found");
    this.name = "CandidateNotFoundError";
  }
}

async function startImport(
  body: StartImportRequest,
): Promise<StartImportResponse> {
  const { data, error, response } = await client.POST("/api/import", { body });
  // Each refusal carries the server's own sentence (the route declares an
  // ErrorDetail body for all three); for 409 and 422 a bodyless answer falls
  // back to a class sentence, which is why detailMessage's null passes through.
  if (response.status === 409) {
    throw new ImportConflictError(detailMessage(error));
  }
  if (response.status === 422) {
    throw new ImportStartRejectedError(
      detailMessage(error) ?? "The import was rejected. Check the path and options.",
    );
  }
  // 503 is the exception: only throw the carrying class when there is a
  // sentence to carry. A bodyless/HTML 503 is a proxy's, not ours, and falls to
  // the generic failure below — see {@link ImportUnavailableError}.
  if (response.status === 503) {
    const reason = detailMessage(error);
    if (reason !== null) {
      throw new ImportUnavailableError(reason);
    }
  }
  if (error || !data) {
    throw new Error("Failed to start import");
  }
  return data;
}

/** Start an import (`POST /api/import`). A 409 (already running) surfaces as
 * {@link ImportConflictError} so the entry screen can branch. The caller routes
 * to `/import?job=<id>` on success. */
export function useStartImport() {
  return useMutation({
    mutationFn: startImport,
  });
}

/** Poll cadence (ms) while an import is active. Brisk enough that auto-applied
 * albums stream into the feed promptly; the loop stops at a terminal phase. */
const IMPORT_POLL_MS = 1000;

/** Poll cadence (ms) while the worker is blocked on a person
 * (`awaiting_decision`). beets runs under `config["threaded"] = False`
 * (backend/app/beets/import_session.py), so its pipeline is serial and the one
 * worker thread sits in `reply.get()` — nothing but the server's elapsed clock
 * can move until a decision is pushed. Both decision mutations invalidate this
 * query, so a decision never waits for this interval. */
const IMPORT_PARKED_POLL_MS = 10_000;

/** Phases where the worker is still running — the feed is live and should poll.
 * `applying` is included defensively: beets exposes no signal to set it, so it
 * may never be observed, but if it is it's a transient working state, not
 * terminal. */
const ACTIVE_PHASES: ReadonlySet<ImportPhase> = new Set([
  "scanning",
  "reviewing",
  "applying",
]);

/** Whether `phase` is terminal (the import finished or failed). Used by the
 * import run page to flag terminal states. */
export function isTerminalPhase(phase: ImportPhase): boolean {
  return phase === "done" || phase === "failed";
}

/** Whether beets is working right now: the run is active AND nobody is being
 * waited on. THE one predicate behind both the run page's spinner and this
 * hook's poll cadence — they must never disagree about who is working.
 *
 * Neither half is inferable from the feed. `phase` latches to "reviewing" at
 * the first parked album and never returns to "scanning", so it still reads
 * "reviewing" while the worker scans the rest of the folder. And a set-aside
 * row status means two different things: an UNATTENDED duplicate emits
 * `needs_dup_resolution` and skips on without parking, and a `search`
 * re-lookup deliberately keeps its row `needs_review` while beets works — both
 * used to read as "blocked", which dropped the spinner and backed the poll off
 * for a decision nobody would ever be asked for. `awaiting_decision` is the
 * server's own answer: the bridge reports whether a park is still unanswered
 * (`ImportBridge.has_unanswered_park`, backend/app/beets/import_session.py). */
export function isWorking(state: ImportJobState): boolean {
  return ACTIVE_PHASES.has(state.phase) && !state.awaiting_decision;
}

async function fetchJob(jobId: string): Promise<ImportJobState> {
  const { data, error, response } = await client.GET("/api/import/{job_id}", {
    params: { path: { job_id: jobId } },
  });
  // A 404 means the job is unknown/expired — surface it distinctly so the page
  // can show a not-found notice and the poll can stop.
  if (response.status === 404) {
    throw new ImportJobNotFoundError();
  }
  if (error || !data) {
    throw new Error("Failed to load import job");
  }
  return data;
}

/**
 * Poll an import job's state (`GET /api/import/{job_id}`). Disabled until a
 * `jobId` exists (no request, no error). `refetchInterval` is a function so the
 * loop runs only while the phase is active (scanning/reviewing/applying), backs
 * off to {@link IMPORT_PARKED_POLL_MS} while the worker is blocked on a person
 * ({@link isWorking}), and returns `false` once terminal (done/failed)
 * or when the job is not found
 * (404 -> `ImportJobNotFoundError`). `retry: false` disables React Query's
 * per-request retry; a transient error still keeps the poll loop (it may
 * self-heal) behind the page's manual retry, whereas a 404 stops it for good.
 */
export function useImportJob(jobId: string | undefined) {
  return useQuery({
    queryKey: ["import", "job", jobId],
    queryFn: () => {
      // `enabled` already guarantees a jobId; narrow it (no cast) so the
      // type is proven rather than asserted.
      if (!jobId) throw new Error("no job id");
      return fetchJob(jobId);
    },
    enabled: Boolean(jobId),
    retry: false,
    refetchInterval: (query) => {
      // A not-found job is gone for good — stop polling. Other transient errors
      // keep the loop (they may recover); the page shows a retry meanwhile.
      if (query.state.error instanceof ImportJobNotFoundError) {
        return false;
      }
      const state = query.state.data;
      if (state === undefined) {
        return IMPORT_POLL_MS;
      }
      // Terminal FIRST. Order is load-bearing: a run that dies while an album
      // is parked would otherwise poll at 10s forever, since the backoff branch
      // below returns an interval rather than falling through.
      if (!ACTIVE_PHASES.has(state.phase)) {
        return false;
      }
      // Back off only while the worker is genuinely blocked on a person: it
      // can then re-read nothing but the elapsed clock, and a 1s poll cost 60
      // requests a minute for as long as the operator took (the measured
      // defect). While beets is working the feed is live — stay fast.
      //
      // An EMPTY feed is exempt. A park buffered before its row exists leaves
      // the state blocked with nothing on screen to act on, and the outcome
      // that creates the row is already queued — so exactly one poll stands
      // between the user and the decision panel, and the backoff makes it 10s.
      // This is the unattended inbox path.
      return isWorking(state) || state.albums.length === 0
        ? IMPORT_POLL_MS
        : IMPORT_PARKED_POLL_MS;
    },
  });
}

async function fetchCandidate(jobId: string, index: number): Promise<Candidate> {
  const result = await client.GET("/api/import/{job_id}/albums/{index}", {
    params: { path: { job_id: jobId, index } },
  });
  // A 404 means the album is no longer parked (already decided / the worker
  // advanced) — surface it distinctly (like fetchJob's 404), NOT through the
  // generic unwrap. That lets the page tell a genuine "gone" apart from a
  // transient failure, and stop any live poll.
  if (result.response.status === 404) {
    throw new CandidateNotFoundError();
  }
  return unwrap(result, "Failed to load candidate");
}

/**
 * Fetch the full Candidate for one parked album
 * (`GET /api/import/{job}/albums/{index}`). `enabled` gates it so it only fires
 * when the review screen opens (it 404s once the album is no longer parked).
 * `refetchInterval` is wrapped so a 404 (`CandidateNotFoundError`) stops the
 * poll for good — a caller can hold it live (while a search re-parks the album)
 * without hammering a 404 once the album is gone. `retry: false` means one
 * transient error surfaces immediately; the page shows a retryable error there.
 */
export function useImportCandidate(
  jobId: string,
  index: number,
  enabled: boolean,
  refetchInterval: number | false = false,
) {
  return useQuery({
    queryKey: ["import", "candidate", jobId, index],
    queryFn: () => fetchCandidate(jobId, index),
    enabled,
    retry: false,
    refetchInterval: (query) => {
      // A not-found candidate is gone for good — stop polling. Other transient
      // errors keep the caller's cadence (they may recover).
      if (query.state.error instanceof CandidateNotFoundError) {
        return false;
      }
      return refetchInterval;
    },
  });
}

/** `GET /api/import/{job}/albums/{index}/duplicates` response (generated). */
export type DuplicatesCheckResponse = components["schemas"]["DuplicatesCheckResponse"];

/**
 * Up-front library-collision check for the selected candidate option. A
 * heads-up only — Apply still routes through the duplicate prompt.
 * `searchRevision` keys the cache so a landed release search re-checks
 * against the fresh options; 404 (no longer parked) just ends the query.
 */
export function useImportDuplicates(
  jobId: string,
  index: number,
  candidateIndex: number,
  searchRevision: number,
) {
  return useQuery({
    queryKey: ["import", "duplicates", jobId, index, candidateIndex, searchRevision],
    retry: false,
    queryFn: async () =>
      unwrap(
        await client.GET("/api/import/{job_id}/albums/{index}/duplicates", {
          params: {
            path: { job_id: jobId, index },
            query: { candidate_index: candidateIndex },
          },
        }),
        "Failed to check for duplicates",
      ),
  });
}

/** Arguments to a choice submission: which album, and the decision. */
export interface SubmitChoiceArgs {
  index: number;
  choice: ImportChoice;
}

async function submitChoice(
  jobId: string,
  { index, choice }: SubmitChoiceArgs,
): Promise<void> {
  const { error, response } = await client.POST(
    "/api/import/{job_id}/albums/{index}/choice",
    { params: { path: { job_id: jobId, index } }, body: choice },
  );
  // 404 (slot already advanced) / 409 (a choice already landed) are not hard
  // failures: they mean 'no longer awaiting this album'. Swallow them — the
  // caller refetches the job to resync. A real transport error still throws.
  if (response.status === 404 || response.status === 409) {
    return;
  }
  // Any other non-2xx is a hard failure. Guard on `!response.ok`, not just
  // `error`: a bodyless 5xx (e.g. a gateway 502) leaves openapi-fetch's `error`
  // undefined, and on this no-undo action we must surface it, never resolve as
  // if the choice landed.
  if (error || !response.ok) {
    throw new Error("Failed to submit choice");
  }
}

/**
 * Submit a decision for a parked album
 * (`POST /api/import/{job}/albums/{index}/choice`). On settle, invalidate the
 * job query so the feed reflects the new state (the worker advances to the next
 * album). A 404/409 is treated as 'already advanced' and resolves quietly.
 */
export function useSubmitChoice(jobId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (args: SubmitChoiceArgs) => submitChoice(jobId, args),
    onSettled: (_data, _err, args) => {
      void queryClient.invalidateQueries({
        queryKey: ["import", "job", jobId],
      });
      // A `search` choice makes the worker re-park this album in place — refetch
      // its candidate so the re-looked-up release renders once it lands.
      void queryClient.invalidateQueries({
        queryKey: ["import", "candidate", jobId, args.index],
      });
    },
  });
}

async function fetchDuplicatePrompt(
  jobId: string,
  index: number,
): Promise<DuplicatePrompt> {
  const { data, error } = await client.GET(
    "/api/import/{job_id}/albums/{index}/duplicate",
    { params: { path: { job_id: jobId, index } } },
  );
  if (error || !data) {
    throw new Error("Failed to load duplicate");
  }
  return data;
}

/**
 * Fetch the parked DuplicatePrompt for one album
 * (`GET /api/import/{job}/albums/{index}/duplicate`). `enabled` gates it so it
 * fires only when the duplicate panel opens (it 404s once resolved).
 */
export function useDuplicatePrompt(
  jobId: string,
  index: number,
  enabled: boolean,
) {
  return useQuery({
    queryKey: ["import", "duplicate", jobId, index],
    queryFn: () => fetchDuplicatePrompt(jobId, index),
    enabled,
    retry: false,
  });
}

/** Arguments to a duplicate-decision submission: which album, and the decision. */
export interface ResolveDuplicateArgs {
  index: number;
  decision: DuplicateDecision;
}

async function resolveDuplicate(
  jobId: string,
  { index, decision }: ResolveDuplicateArgs,
): Promise<void> {
  const { error, response } = await client.POST(
    "/api/import/{job_id}/albums/{index}/duplicate",
    { params: { path: { job_id: jobId, index } }, body: decision },
  );
  // 404 (already advanced) / 409 (a decision already landed) mean 'no longer
  // awaiting this album' — swallow them; the caller refetches the job to resync.
  if (response.status === 404 || response.status === 409) {
    return;
  }
  // Any other non-2xx is a hard failure. Guard on `!response.ok`, not just
  // `error`: a bodyless 5xx leaves openapi-fetch's `error` undefined, and on
  // this action we must surface it, never resolve as if the decision landed.
  if (error || !response.ok) {
    throw new Error("Failed to resolve duplicate");
  }
}

/**
 * Submit a duplicate decision
 * (`POST /api/import/{job}/albums/{index}/duplicate`). On settle, invalidate the
 * job query so the feed advances. 404/409 resolve quietly (already advanced).
 */
export function useResolveImportDuplicate(jobId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (args: ResolveDuplicateArgs) => resolveDuplicate(jobId, args),
    onSettled: () => {
      void queryClient.invalidateQueries({
        queryKey: ["import", "job", jobId],
      });
    },
  });
}

async function pauseImport(jobId: string): Promise<void> {
  const { error, response } = await client.POST("/api/import/{job_id}/pause", {
    params: { path: { job_id: jobId } },
  });
  // 404 (job gone) / 409 (not a sweep any more / already finished) both mean
  // "there is nothing left to pause" — the refetch below shows the real state.
  // A repeat pause on a still-active sweep is an idempotent 204 server-side.
  if (response.status === 404 || response.status === 409) {
    return;
  }
  if (error || !response.ok) {
    throw new Error("Failed to pause the sweep");
  }
}

/**
 * Ask the active sweep to stop at its next album boundary
 * (`POST /api/import/{job}/pause`). On settle, refresh the job state AND the
 * active probe so every sweep surface (run page, Review banner, activity row)
 * flips to "pausing"/done together.
 */
export function usePauseSweep(jobId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => pauseImport(jobId),
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ["import", "job", jobId] });
      void queryClient.invalidateQueries({ queryKey: ["active-import"] });
    },
  });
}
