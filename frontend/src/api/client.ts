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

/**
 * Endpoints where a 401 is a NORMAL result rather than "your session is gone",
 * as an EXACT set — the server's exemption is exact too
 * (`backend/app/auth/gate.py::EXEMPT_PATHS`), and a prefix here would claim
 * more than the gate grants.
 *
 * `login` earns its place: its 401 IS the rejection the form exists to render,
 * and bouncing on it would throw the user off the very page they are signing in
 * on. `status` is the login page's own admission probe — it is gate-exempt and
 * answers 200 for a cookie-less caller, so a 401 there would be a contract
 * break rather than a session fact, and acting on one would have this page sign
 * its visitor out mid-probe.
 *
 * The gate's other two exempt paths are absent because listing them would be
 * decoration: `/api/health` (which the topbar does call) and
 * `/api/slskd/webhook` are exempt server-side, so neither can answer 401 at
 * all, and a member no test can distinguish from its absence makes this set
 * look better covered than it is — the same reasoning `gate.py` gives for
 * keeping `logout` out of `AUTH_ROUTE_PATHS`.
 *
 * `/api/auth/logout` is deliberately ABSENT: it is gated, so its 401 means the
 * session really is gone. Exempting it deadlocked sign-out — the middleware
 * returned without flipping the store, `useLogout` threw, and every retry
 * reproduced it (see `useLogout` in api/auth.ts for the other half of the fix).
 *
 * EXPORTED for one reason: this set and `backend/app/auth/gate.py::EXEMPT_PATHS`
 * are hand-maintained in two languages, and nothing failed if they diverged.
 * The tracked `frontend/openapi.json` already carries the gate's own answer —
 * `app/openapi_overlay.py` stamps `security: []` on exactly the operations
 * `path_requires_session` exempts — so `api/gateExemptions.test.ts` reads the
 * contract and refuses to let the two drift silently.
 */
export const GATE_EXEMPT_PATHS: ReadonlySet<string> = new Set([
  "/api/auth/login",
  "/api/auth/status",
]);

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
    if (response.status !== 401 || GATE_EXEMPT_PATHS.has(schemaPath)) {
      return;
    }
    markUnauthenticated();
    throw new UnauthenticatedError();
  },
});
