import { QueryClient } from "@tanstack/react-query";

import { UnauthenticatedError } from "@/api/authStore";

/**
 * The app's real QueryClient.
 *
 * Its own module so the retry rule below is reachable from a test: main.tsx
 * mounts a root as a side effect of being imported, and `test/render.tsx`
 * builds a `retry: false` client of its own — between them, nothing exercised
 * the production behaviour.
 *
 * Retries are capped so an outage surfaces the error state promptly instead of
 * hanging through TanStack's long default backoff, and a short staleTime
 * avoids refetching on every focus/mount for read-heavy library views.
 *
 * `failureCount < 1` is the predicate form of the plain `retry: 1` it grew
 * out of — TanStack evaluates `failureCount < retry` with the count still at 0
 * on the first failure, so the two agree exactly. The 401 is the only
 * departure: a dead session is not bad luck, and a second attempt spends
 * another round trip before the guard bounces to /login regardless.
 */
export function createAppQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: (failureCount, error) =>
          !(error instanceof UnauthenticatedError) && failureCount < 1,
        staleTime: 30_000,
      },
    },
  });
}
