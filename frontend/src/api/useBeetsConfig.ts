import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { client } from "@/api/client";
import { unwrap } from "@/api/lib";
import type { components } from "@/api/schema";

/** Effective beets config + file freshness for the `/settings` view (generated). */
export type BeetsConfigSnapshot = components["schemas"]["BeetsConfigSnapshot"];
/** Body of `POST /api/config/save` (generated contract). */
export type SaveRequest = components["schemas"]["SaveRequest"];
/** Body of `POST /api/config/validate` (generated contract). */
export type ValidateRequest = components["schemas"]["ValidateRequest"];
/** One row of the lint output (generated; the editor's gutter consumes it). */
export type ValidationErrorItem = components["schemas"]["ValidationErrorItem"];

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

function configOpError(
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
  return useMutation({
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
 * before re-enabling. The 500 branch carries a recovery hint in `body` so the
 * SettingsPage can render it inline. Cache invalidation hits both the snapshot
 * (the post-reload `BeetsConfigSnapshot` has `apply_pending = false` and a new
 * mtime) AND the `["active-import"]` probe (an import may have started+ended
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
    },
  });
}

/**
 * Lint the working YAML (`POST /api/config/validate`).
 *
 * Returns the `errors` array directly (never the wrapping `ValidateResponse`)
 * because the only caller — CodeMirror's async `linter()` source — needs a
 * flat list keyed off `line`/`column` for the gutter. A clean validate is the
 * empty array, NOT a thrown error: the backend always 200s on a successful
 * parse, leaving lint failures as a data shape rather than an exception. Hard
 * transport errors (network down, 5xx) still throw so React Query reports them.
 */
export function useValidateConfig() {
  return useMutation({
    mutationFn: async (
      req: ValidateRequest,
    ): Promise<ValidationErrorItem[]> => {
      const { data, error, response } = await client.POST(
        "/api/config/validate",
        {
          body: req,
        },
      );
      if (!response.ok || !data) {
        throw configOpError("Validate failed", response.status, error);
      }
      return data.errors;
    },
  });
}
