import createClient from "openapi-fetch";

import { markUnauthenticated, UnauthenticatedError } from "@/api/authStore";
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

/** Endpoints that answer 401 as a NORMAL result rather than as "your session
 * is gone": sign-in rejecting a wrong password, and its siblings. Bouncing on
 * those would throw the user off the very page they are trying to sign in on. */
const AUTH_PATH_PREFIX = "/api/auth/";

/**
 * One place where the session gate's 401 becomes an app-level fact.
 *
 * Every gated route can answer 401 (an expired or missing cookie), and no call
 * site can act on it usefully: `lib.ts`'s `unwrap` collapses the status code
 * into a generic Error, and the ~40 hooks that check `{ error, response }`
 * themselves would each paint their own misleading "Couldn't load …" while the
 * app is already bouncing to /login. Handling it here covers all of them with
 * one edit, and openapi-fetch propagates a throw from `onResponse` straight out
 * of `client.GET/POST/…` (its middleware loop is not wrapped in a try) — the
 * same rejection shape a transport failure already produces, so callers need no
 * change.
 */
client.use({
  onResponse({ response, schemaPath }) {
    if (response.status !== 401 || schemaPath.startsWith(AUTH_PATH_PREFIX)) {
      return;
    }
    markUnauthenticated();
    throw new UnauthenticatedError();
  },
});
