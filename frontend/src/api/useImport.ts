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

/** Thrown when a start is rejected because an import is already running (409).
 * Lets the entry screen surface a "an import is already running" message with a
 * link to it, instead of the generic failure. */
export class ImportConflictError extends Error {
  constructor() {
    super("An import is already running");
    this.name = "ImportConflictError";
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

/** Thrown when the polled job id is unknown or expired (backend 404). Lets the
 * run page show a dedicated "no longer available" notice and STOP polling,
 * rather than hammering the 404 every second. */
export class ImportJobNotFoundError extends Error {
  constructor() {
    super("Import job not found");
    this.name = "ImportJobNotFoundError";
  }
}

async function startImport(
  body: StartImportRequest,
): Promise<StartImportResponse> {
  const { data, error, response } = await client.POST("/api/import", { body });
  if (response.status === 409) {
    throw new ImportConflictError();
  }
  if (response.status === 422) {
    throw new ImportStartRejectedError(
      detailMessage(error) ?? "The import was rejected. Check the path and options.",
    );
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
 * loop runs only while the phase is active (scanning/reviewing/applying) and
 * returns `false` once terminal (done/failed) or when the job is not found
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
      const phase = query.state.data?.phase;
      if (phase === undefined || ACTIVE_PHASES.has(phase)) {
        return IMPORT_POLL_MS;
      }
      return false;
    },
  });
}

async function fetchCandidate(jobId: string, index: number): Promise<Candidate> {
  return unwrap(
    await client.GET("/api/import/{job_id}/albums/{index}", {
      params: { path: { job_id: jobId, index } },
    }),
    "Failed to load candidate",
  );
}

/**
 * Fetch the full Candidate for one parked album
 * (`GET /api/import/{job}/albums/{index}`). `enabled` gates it so it only fires
 * when the review screen opens (it 404s once the album is no longer parked).
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
    refetchInterval,
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
