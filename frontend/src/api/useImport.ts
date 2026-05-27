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
