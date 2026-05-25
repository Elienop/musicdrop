import createClient from "openapi-fetch";

import type { paths } from "@/api/schema";

/**
 * Typed API client.
 *
 * The backend mounts its routers under a `/api` prefix, so the generated
 * OpenAPI paths already start with `/api/...` (e.g. `/api/health`). The
 * `baseUrl` therefore stays empty — prefixing `/api` here would double up to
 * `/api/api/health`. Requests are same-origin: in dev, Vite proxies `/api` →
 * http://localhost:3030 (see vite.config.ts); in prod the SPA is served from
 * the same origin as the API.
 *
 * `baseUrl` is the page origin (so the paths' leading `/api` is not doubled).
 * We use the origin explicitly rather than `""` because Node's fetch (undici)
 * — used under jsdom in tests — refuses to parse origin-relative URLs;
 * resolving against `window.location.origin` keeps a single code path that
 * works in both the browser and the test runner.
 */
export const client = createClient<paths>({
  baseUrl: typeof window === "undefined" ? "" : window.location.origin,
  // Defer the global `fetch` lookup to call time. openapi-fetch otherwise
  // captures `globalThis.fetch` at client-creation (module load), which under
  // tests is the pre-MSW reference — so requests would escape interception.
  // Resolving per-call also future-proofs any runtime fetch swap.
  fetch: (...args) => globalThis.fetch(...args),
});
