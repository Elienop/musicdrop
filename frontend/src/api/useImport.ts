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
/** Body of `POST /api/import` (generated contract). */
export type StartImportRequest = components["schemas"]["StartImportRequest"];
/** Response of a successful `POST /api/import` (generated contract). */
export type StartImportResponse = components["schemas"]["StartImportResponse"];
/** The full per-album review payload (generated; chunk 4 renders it). */
export type Candidate = components["schemas"]["Candidate"];
/** A user's decision for one parked album (generated contract). */
export type ImportChoice = components["schemas"]["ImportChoice"];

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

/** Whether `phase` is terminal (the import finished or failed). */
export function isTerminalPhase(phase: ImportPhase): boolean {
  return phase === "done" || phase === "failed";
}

async function fetchJob(jobId: string): Promise<ImportJobState> {
  const { data, error } = await client.GET("/api/import/{job_id}", {
    params: { path: { job_id: jobId } },
  });
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
    queryFn: () => fetchJob(jobId as string),
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
