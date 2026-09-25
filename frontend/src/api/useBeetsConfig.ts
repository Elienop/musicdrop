import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { client } from "@/api/client";
import { unwrap } from "@/api/lib";
import type { components } from "@/api/schema";

/** Key of `GET /api/config/import-operation`, which `useApplyConfig` and the
 * Keep downloads switch invalidate: both reload beets. Here, not in
 * `useImportOperation.ts`, which imports this file. */
export const IMPORT_OPERATION_KEY = ["config", "import-operation"] as const;

/** Effective beets config + file freshness for the `/settings` view (generated). */
export type BeetsConfigSnapshot = components["schemas"]["BeetsConfigSnapshot"];
/** Body of `POST /api/config/save` (generated contract). */
export type SaveRequest = components["schemas"]["SaveRequest"];
/** Body of `POST /api/config/validate` (generated contract). */
export type ValidateRequest = components["schemas"]["ValidateRequest"];
/** One row of the lint output (generated; the editor's gutter consumes it). */
export type ValidationErrorItem = components["schemas"]["ValidationErrorItem"];
/** Both channels of `POST /api/config/validate` (generated contract). */
export type ValidateResponse = components["schemas"]["ValidateResponse"];
/**
 * One row of the ADVISORY channel (generated): a VALID setting that
 * MusicDrop-driven imports force or discard. `key` is the dotted config path
 * ("import.autotag"); `message` is the backend's copy, rendered verbatim.
 */
export type ConfigAdvisory = components["schemas"]["ConfigAdvisory"];

/** A structured error for the editor's mutation failures.
 *
 * Both `useSaveConfig` and `useApplyConfig` need to branch on HTTP status
 * (422 vs 409 vs 500) and surface the raw body for the conflict modal — a
 * stringified `.message` would force the caller to regex-match the detail.
 * We keep `name = "ConfigOpError"` so a `instanceof Error` check still holds
 * downstream while the SettingsPage can narrow on `.status` cleanly.
 */
export interface ConfigOpError extends Error {
  status: number;
  body: unknown;
}

export function configOpError(
  message: string,
  status: number,
  body: unknown,
): ConfigOpError {
  return Object.assign(new Error(message), {
    status,
    body,
    name: "ConfigOpError",
  });
}

/**
 * The recovery hint an Apply 422 or 500 carries, nested as
 * `{detail: {message, recovery}}` (config_editor.apply). Null for any other
 * shape or a blank string, so the caller falls back to its own sentence.
 */
export function applyRecoveryHint(
  err: ConfigOpError | null | undefined,
): string | null {
  const detail = (err?.body as { detail?: unknown } | undefined)?.detail;
  if (detail && typeof detail === "object" && "recovery" in detail) {
    const recovery = (detail as { recovery?: unknown }).recovery;
    if (typeof recovery === "string" && recovery.trim()) return recovery;
  }
  return null;
}

/** The `type` of a Save 422 row about config.yaml on disk
 * (config_editor._ON_DISK_ERROR_TYPE). The schema types `type` as a string. */
const CONFIG_ON_DISK = "config_on_disk";

/**
 * The server's sentence from a `config_on_disk` row of a Save 422, from either
 * `POST /api/config/save` or `POST /api/config/naming/save`. Null for any other
 * body (validation rows, a 409, a 500), so the caller keeps its own sentence.
 */
export function configOnDiskMessage(body: unknown): string | null {
  const detail = (body as { detail?: unknown } | null | undefined)?.detail;
  if (!Array.isArray(detail)) return null;
  for (const row of detail as unknown[]) {
    if (row && typeof row === "object" && "type" in row && "msg" in row) {
      const { type, msg } = row as { type: unknown; msg: unknown };
      if (type === CONFIG_ON_DISK && typeof msg === "string" && msg.trim()) {
        return msg;
      }
    }
  }
  return null;
}

async function fetchConfig(): Promise<BeetsConfigSnapshot> {
  return unwrap(await client.GET("/api/config"), "Failed to load config");
}

/**
 * Fetch the effective beets config snapshot (`GET /api/config`). The page is
 * passive: read-only YAML + a mtime-derived restart hint. `staleTime: 30_000`
 * keeps the snapshot warm across short tab-switches without polling.
 */
export function useBeetsConfig() {
  return useQuery({
    queryKey: ["beets-config"],
    queryFn: fetchConfig,
    staleTime: 30_000,
  });
}

/**
 * Persist the edited YAML (`POST /api/config/save`). On a non-2xx the hook
 * throws a {@link ConfigOpError} so the SettingsPage can branch on
 * `.status === 422` (schema/parse — render the gutter via the lint source)
 * vs `.status === 409` (CAS mismatch — render the conflict diff modal) without
 * a string-match on `.message`. On success the snapshot query is invalidated
 * so the editor reseeds against the freshly-written file (and `apply_pending`
 * flips true).
 */
export function useSaveConfig() {
  const queryClient = useQueryClient();
  // Type the error as ConfigOpError (what the mutationFn throws) so callers can
  // branch on `.status` — e.g. 409 (conflict) vs any other failure — without a
  // narrowing dance. Mirrors useApplyConfig.
  return useMutation<BeetsConfigSnapshot, ConfigOpError, SaveRequest>({
    mutationFn: async (req: SaveRequest): Promise<BeetsConfigSnapshot> => {
      const { data, error, response } = await client.POST("/api/config/save", {
        body: req,
      });
      // Guard on !response.ok rather than `error` alone — a bodyless 5xx leaves
      // openapi-fetch's `error` undefined yet the call must still surface a
      // failure (mirroring `fetchConfig`'s rule above).
      if (!response.ok || !data) {
        throw configOpError("Save failed", response.status, error);
      }
      return data;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["beets-config"] });
    },
  });
}

/**
 * Reload beets in-process (`POST /api/config/apply`). No body — the registry
 * gates this server-side on the import-active probe; if the user clicks Apply
 * during an import the backend returns 409 and the page rehydrates the gate
 * before re-enabling. The 422 and 500 branches carry a recovery hint in `body`
 * ({@link applyRecoveryHint}); both Apply surfaces render it inline. Cache
 * invalidation hits both the snapshot (the post-reload `BeetsConfigSnapshot`
 * has `apply_pending = false` and a new mtime) AND the `["active-import"]` probe (an import may have started+ended
 * during the rebuild — cheaper to refetch than to reason about the race).
 */
export function useApplyConfig() {
  const queryClient = useQueryClient();
  // Type the error as ConfigOpError (what the mutationFn throws) so callers can
  // branch on `.status` — e.g. a 409 when a library job is blocking Apply.
  return useMutation<BeetsConfigSnapshot, ConfigOpError>({
    mutationFn: async (): Promise<BeetsConfigSnapshot> => {
      const { data, error, response } = await client.POST("/api/config/apply");
      if (!response.ok || !data) {
        throw configOpError("Apply failed", response.status, error);
      }
      return data;
    },
    onSuccess: () => {
      // Snapshot first (the editor reseeds against `apply_pending = false`).
      void queryClient.invalidateQueries({ queryKey: ["beets-config"] });
      // Then the import-active probe. The probe gated this Apply (the click
      // would have 409'd otherwise) so the cached `false` *was* correct — but
      // a long rebuild could leave it stale. The key here must stay in
      // lockstep with `useActiveImport`'s `queryKey: ["active-import"]`.
      void queryClient.invalidateQueries({ queryKey: ["active-import"] });
      // What imports use is read at every load, so an Apply can change it.
      void queryClient.invalidateQueries({ queryKey: IMPORT_OPERATION_KEY });
    },
  });
}

/**
 * Lint the working YAML (`POST /api/config/validate`).
 *
 * Returns the WHOLE {@link ValidateResponse}, because the endpoint answers on
 * two independent channels and they have different destinations. `errors` is
 * what CodeMirror's async `linter()` source turns into gutter diagnostics;
 * `advisories` is what it must NOT — an advisory fires on a perfectly valid
 * setting that MusicDrop-driven imports force or discard, so painting one red
 * would make a correct config look broken. The page lifts advisories into
 * React state and renders them beside the editor instead.
 *
 * (This hook used to narrow to `data.errors` on the grounds that the linter
 * was the only caller. That is no longer true, and narrowing here would
 * discard the advisory channel before any caller could reach it.)
 *
 * A clean validate is two empty arrays, NOT a thrown error: the backend always
 * 200s on a successful parse, leaving lint failures as a data shape rather
 * than an exception. Hard transport errors (network down, 5xx) still throw so
 * React Query reports them.
 */
export function useValidateConfig() {
  return useMutation({
    mutationFn: async (req: ValidateRequest): Promise<ValidateResponse> => {
      const { data, error, response } = await client.POST(
        "/api/config/validate",
        {
          body: req,
        },
      );
      if (!response.ok || !data) {
        throw configOpError("Validate failed", response.status, error);
      }
      return data;
    },
  });
}
