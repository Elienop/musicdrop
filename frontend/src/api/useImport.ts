import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { client } from "@/api/client";
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
/** Body of `POST /api/import` (generated contract). */
export type StartImportRequest = components["schemas"]["StartImportRequest"];
/** Response of a successful `POST /api/import` (generated contract). */
export type StartImportResponse = components["schemas"]["StartImportResponse"];
/** The full per-album review payload (generated; chunk 4 renders it). */
export type Candidate = components["schemas"]["Candidate"];
/** A user's decision for one parked album (generated contract). */
export type ImportChoice = components["schemas"]["ImportChoice"];

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

async function startImport(
  body: StartImportRequest,
): Promise<StartImportResponse> {
  const { data, error, response } = await client.POST("/api/import", { body });
  if (response.status === 409) {
    throw new ImportConflictError();
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

/** Whether `phase` is terminal (the import finished or failed).
 * Consumed by the import page in chunk 4/5 (drives the "done/failed" UI and
 * stops polling) — not dead code. */
export function isTerminalPhase(phase: ImportPhase): boolean {
  return phase === "done" || phase === "failed";
}

async function fetchJob(jobId: string): Promise<ImportJobState> {
  const { data, error } = await client.GET("/api/import/{job_id}", {
    params: { path: { job_id: jobId } },
  });
  // An unknown/expired job id (backend 404) intentionally collapses into this
  // generic error for the chunk-3 shell; chunk 4 may add a dedicated not-found
  // branch if the page needs to distinguish it.
  if (error || !data) {
    throw new Error("Failed to load import job");
  }
  return data;
}

/**
 * Poll an import job's state (`GET /api/import/{job_id}`). Disabled until a
 * `jobId` exists (no request, no error). `refetchInterval` is a function so the
 * loop runs only while the phase is active (scanning/reviewing/applying) and
 * returns `false` once terminal (done/failed) — TanStack v5 stops polling on a
 * falsy interval. No auto-retry: a transient error surfaces in the page's error
 * state behind an explicit retry rather than a silent backoff.
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
      const phase = query.state.data?.phase;
      if (phase === undefined || ACTIVE_PHASES.has(phase)) {
        return IMPORT_POLL_MS;
      }
      return false;
    },
  });
}

async function fetchCandidate(jobId: string, index: number): Promise<Candidate> {
  const { data, error } = await client.GET(
    "/api/import/{job_id}/albums/{index}",
    { params: { path: { job_id: jobId, index } } },
  );
  if (error || !data) {
    throw new Error("Failed to load candidate");
  }
  return data;
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
) {
  return useQuery({
    queryKey: ["import", "candidate", jobId, index],
    queryFn: () => fetchCandidate(jobId, index),
    enabled,
    retry: false,
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
  if (error) {
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
    onSettled: () => {
      void queryClient.invalidateQueries({
        queryKey: ["import", "job", jobId],
      });
    },
  });
}
