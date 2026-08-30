import { useSyncExternalStore } from "react";

/**
 * The one fact about the session the app learns WITHOUT asking: the server
 * refused a gated request, so this browser no longer holds a usable cookie.
 *
 * It lives outside React (and outside TanStack) because the transport layer
 * discovers it — `api/client.ts`'s middleware and `api/lib.ts`'s `apiFetch`,
 * both of which run in plain module scope where there is no hook to call and
 * no `navigate` to reach. jsdom also throws on `window.location.assign`, so a
 * module-scope redirect could not be tested. A subscribable store instead
 * lets the router-level guard observe the flip and navigate normally — the
 * same idiom as `api/assetVersion.ts`.
 *
 * Deliberately ONE boolean rather than a three-state mirror of
 * `GET /api/auth/status`: the status endpoint already answers "are we signed
 * in", and a second copy of that answer can only drift from it. This store
 * carries the half the endpoint cannot — that the cached answer has since
 * gone stale. `useAuthGateState` (api/auth.ts) composes the two into the
 * "unknown" / "authenticated" / "unauthenticated" state the guard renders.
 */

/** Thrown for any 401 outside the `/api/auth/` family, so a caller (and the
 * query client's retry predicate) can tell "your session is gone" apart from
 * an ordinary request failure. */
export class UnauthenticatedError extends Error {
  constructor(message = "Your session has expired. Sign in again.") {
    super(message);
    this.name = "UnauthenticatedError";
  }
}

let signedOut = false;
const listeners = new Set<() => void>();

function notify(): void {
  for (const l of listeners) l();
}

/** Record that the server refused us. Idempotent BY DESIGN: one page mounts
 * half a dozen queries that all 401 together, and each extra notification
 * would re-render every subscriber for a transition that already happened. */
export function markUnauthenticated(): void {
  if (signedOut) return;
  signedOut = true;
  notify();
}

/** Clear the refusal — a successful sign-in is the only thing that knows the
 * cookie is good again. Idempotent for the same reason. */
export function markAuthenticated(): void {
  if (!signedOut) return;
  signedOut = false;
  notify();
}

/**
 * Subscribe to transitions. Exported because it is the ONLY witness to the
 * idempotency above: `useSignedOut` goes through `useSyncExternalStore`, which
 * compares snapshots with `Object.is` and silently swallows a redundant
 * notification — so a store that fired once per refusal instead of once per
 * transition would render identically, and a test written through the hook
 * would pass either way.
 */
export function subscribeAuthStore(onChange: () => void): () => void {
  listeners.add(onChange);
  return () => listeners.delete(onChange);
}

function read(): boolean {
  return signedOut;
}

/** True once a gated request has been refused, until the next sign-in. */
export function useSignedOut(): boolean {
  return useSyncExternalStore(subscribeAuthStore, read, read);
}
