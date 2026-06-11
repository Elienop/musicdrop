import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { QueryClient } from "@tanstack/react-query";

import { client } from "@/api/client";
import type { components } from "@/api/schema";

/** Whole-library duplicate-album report (generated contract). */
export type DuplicatesReport = components["schemas"]["DuplicatesReport"];
/** One detected duplicate group (generated). */
export type DuplicateGroup = components["schemas"]["DuplicateGroup"];
/** One album copy in a group (generated). */
export type DuplicateAlbum = components["schemas"]["DuplicateAlbum"];
/** strict | fuzzy match mode (generated). */
export type DuplicateMode = components["schemas"]["DuplicateMode"];
/** Body of POST /api/duplicates/resolve (generated). */
export type ResolveRequest = components["schemas"]["ResolveRequest"];
/** Result of a resolve (generated). */
export type ResolveResult = components["schemas"]["ResolveResult"];
/** Body of POST /api/duplicates/resolve-all (generated). */
export type ResolveAllRequest = components["schemas"]["ResolveAllRequest"];
/** Result of a batch resolve (generated). */
export type ResolveAllResult = components["schemas"]["ResolveAllResult"];

/** Structured error so the page can branch on HTTP status (409 import/stale,
 * 404 gone, 500). `name = "DuplicatesOpError"` keeps `instanceof Error` true. */
export interface DuplicatesOpError extends Error {
  status: number;
  body: unknown;
}

function duplicatesOpError(message: string, status: number, body: unknown): DuplicatesOpError {
  return Object.assign(new Error(message), { status, body, name: "DuplicatesOpError" });
}

/** A resolve trashes loser albums out of the library, so beyond the
 * ["duplicates", *] reports every cached library surface — album details,
 * grids, roster, browse, search, stats — would keep serving the removed
 * albums for the 30s staleTime window. Refresh them all. */
function invalidateAfterResolve(queryClient: QueryClient): void {
  void queryClient.invalidateQueries({ queryKey: ["duplicates"] });
  void queryClient.invalidateQueries({ queryKey: ["album"] });
  void queryClient.invalidateQueries({ queryKey: ["albums"] });
  void queryClient.invalidateQueries({ queryKey: ["artists"] });
  void queryClient.invalidateQueries({ queryKey: ["browse"] });
  void queryClient.invalidateQueries({ queryKey: ["search"] });
  void queryClient.invalidateQueries({ queryKey: ["stats"] });
}

async function fetchDuplicates(mode: DuplicateMode): Promise<DuplicatesReport> {
  const { data, error, response } = await client.GET("/api/duplicates", {
    params: { query: { mode } },
  });
  // Guard on !response.ok: a bodyless 5xx leaves openapi-fetch's `error` undefined.
  if (error || !response.ok || !data) {
    throw new Error("Failed to load duplicates");
  }
  return data;
}

/** Fetch the duplicate-album report for a mode. Keyed by mode so strict/fuzzy
 * cache independently; `staleTime` keeps it warm across short tab-switches. */
export function useDuplicates(mode: DuplicateMode) {
  return useQuery({
    queryKey: ["duplicates", mode],
    queryFn: () => fetchDuplicates(mode),
    staleTime: 30_000,
  });
}

/** Resolve a group (move losers to Trash). Invalidates ALL ["duplicates", *]
 * reports so the resolved group disappears on the next render, plus the
 * library surfaces that cached the trashed albums. Throws a
 * {@link DuplicatesOpError} on non-2xx so the page can branch on `.status`. */
export function useResolveDuplicate() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (req: ResolveRequest): Promise<ResolveResult> => {
      const { data, error, response } = await client.POST("/api/duplicates/resolve", {
        body: req,
      });
      if (!response.ok || !data) {
        throw duplicatesOpError("Resolve failed", response.status, error);
      }
      return data;
    },
    onSuccess: () => {
      invalidateAfterResolve(queryClient);
    },
  });
}

/** Resolve EVERY marked group in one request (move losers to Trash). Invalidates
 * all ["duplicates", *] reports on success so resolved groups disappear, plus
 * the library surfaces that cached the trashed albums. Throws a
 * {@link DuplicatesOpError} on non-2xx so the page can branch on `.status`. */
export function useResolveAllDuplicates() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (req: ResolveAllRequest): Promise<ResolveAllResult> => {
      const { data, error, response } = await client.POST("/api/duplicates/resolve-all", {
        body: req,
      });
      if (!response.ok || !data) {
        throw duplicatesOpError("Resolve all failed", response.status, error);
      }
      return data;
    },
    onSuccess: () => {
      invalidateAfterResolve(queryClient);
    },
  });
}
