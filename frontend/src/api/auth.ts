import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";

import { bumpAssetVersion } from "@/api/assetVersion";
import {
  markAuthenticated,
  markUnauthenticated,
  UnauthenticatedError,
  useSignedOut,
} from "@/api/authStore";
import { client } from "@/api/client";
import { detailMessage, unwrap } from "@/api/lib";
import type { components } from "@/api/schema";
import { invalidateLibraryContent } from "@/api/useEventStream";

export type AuthStatus = components["schemas"]["AuthStatus"];

/** Where the server's single password comes from, as the contract spells it.
 * Derived from the generated model rather than re-typed, so a fourth value
 * added server-side is a type error here instead of a silent `default:` arm. */
export type PasswordSource = AuthStatus["password_source"];

/** `GET /api/auth/status` is exempt from the session gate, so this query
 * answers for a signed-OUT browser too — which is what makes it usable both
 * as the shell's admission check and as the login page's "can anyone sign in
 * here at all" probe (`password_set`). */
export const AUTH_STATUS_KEY = ["auth", "status"] as const;

async function fetchAuthStatus(): Promise<AuthStatus> {
  return unwrap(
    await client.GET("/api/auth/status"),
    "Couldn’t check whether you’re signed in.",
  );
}

export function useAuthStatus() {
  return useQuery({ queryKey: AUTH_STATUS_KEY, queryFn: fetchAuthStatus });
}

/**
 * What BOTH session mutations say when the request never got an answer.
 *
 * A transport failure makes `client.POST` REJECT rather than resolve, and the
 * browser's own words for that — "Failed to fetch" (Chromium) / "NetworkError
 * when attempting to fetch resource" (Firefox) — are jargon wherever they
 * surface: in the sign-in page's alert, and in the sign-out toast. One constant
 * rather than two literals because the two hooks were fixed a slice apart and
 * only one of them got the sentence; sharing it is what stops the next edit
 * moving one and not the other.
 *
 * Deliberately about REACHABILITY rather than about signing in or out: it is
 * the same fact in both places, and the operator's next move ("is MusicDrop
 * running?") is the same too.
 */
export const SERVER_UNREACHABLE_MESSAGE =
  "Can’t reach the server. Check that MusicDrop is running, then try again.";

export type AuthGateState = "unknown" | "authenticated" | "unauthenticated";

/**
 * The admission answer, composed from the two halves that each know part of
 * it: the status endpoint (was this browser signed in when we last asked) and
 * the transport store (has the server refused us since). Neither alone is
 * enough — the query's answer goes stale the moment a cookie expires, and the
 * store only ever learns about refusals.
 */
export function useAuthGateState(): AuthGateState {
  const signedOut = useSignedOut();
  const { data, isPending, isError } = useAuthStatus();

  if (signedOut) return "unauthenticated";
  if (isPending) return "unknown";
  // A FAILED probe means the backend is unreachable, not that the session is
  // gone: this endpoint is exempt from the gate and answers 200 for a
  // cookie-less caller. Bouncing to /login on an outage would swap the shell's
  // honest "Offline" indicator for a sign-in form that cannot work either, so
  // "can't tell" renders the shell and lets a real 401 from any gated request
  // flip the store instead.
  if (isError) return "authenticated";
  return data?.authenticated ? "authenticated" : "unauthenticated";
}

/**
 * Empty the query cache when this browser stops being signed in — on the
 * TRANSITION, so both ways of getting there are covered by one line.
 *
 * `["beets-config"]` holds the raw config.yaml with its secrets UNMASKED, and
 * TanStack keeps every cached answer readable for `gcTime` (five minutes by
 * default) after its last observer unmounts. This used to live in
 * `useLogout.onSuccess`, which is the BUTTON rather than the transition: an
 * EXPIRY (a gated poll's 401 → `markUnauthenticated()` from the client
 * middleware) bounced to /login with the whole cache still sitting in the JS
 * heap, because nobody had clicked anything. Measured before the move:
 * `CONFIG STILL IN CACHE AFTER EXPIRY BOUNCE: {"raw":"slskd:\n  api_key:
 * SUPERSECRET\n"}`.
 *
 * `clear()` and not `invalidateQueries()` — invalidation only marks data
 * stale, it leaves the bytes in place.
 *
 * Keyed on the STORE, not on `useAuthGateState()`: the store flips only on a
 * real refusal, whereas the gate state also reads "unauthenticated" for a cold
 * signed-out visit — whose cache is empty anyway, and whose status query
 * `clear()` would delete out from under the observer that is still asking, for
 * a re-render loop and no benefit.
 */
export function useClearCacheOnSignOut(): void {
  const queryClient = useQueryClient();
  const signedOut = useSignedOut();
  useEffect(() => {
    if (!signedOut) return;
    queryClient.clear();
  }, [signedOut, queryClient]);
}

/**
 * Exchange the password for a session cookie.
 *
 * The rejection cases are all authored server-side and meant to be shown
 * verbatim — wrong password, no password configured, an unreadable hash, a
 * sign-in already in flight (429), a missing signing secret (503) — so this
 * surfaces `detail` rather than inventing per-status copy that would drift
 * from the backend's.
 *
 * The success side lives here, not at the call site: TanStack runs a
 * hook-level `onSuccess` unconditionally, while a per-call one is skipped once
 * the observer unmounts. Navigation is the page's job; being signed in is
 * this hook's.
 */
export function useLogin() {
  const queryClient = useQueryClient();
  return useMutation<AuthStatus, Error, string>({
    // The password rides in `variables`, which TanStack keeps readable for
    // `gcTime` after the form unmounts — five minutes of a plaintext
    // credential sitting in memory for no one's benefit. Zero here rather
    // than a `reset()` at the call site: it covers the REJECTED attempt too
    // (a wrong password is still a password someone typed), and it cannot be
    // forgotten by a future caller. Safe while mounted — TanStack's
    // `optionalRemove` only collects a mutation with no observers left.
    gcTime: 0,
    mutationFn: async (password) => {
      let result;
      try {
        result = await client.POST("/api/auth/login", { body: { password } });
      } catch {
        // A rejection here means the request never got an ANSWER — the server
        // is down, the proxy dropped it, DNS failed. The browser's own words
        // for that are jargon on the one screen whose whole job is explaining
        // why signing in isn't working, so this says the same thing sign-out
        // says (see SERVER_UNREACHABLE_MESSAGE). (The gate's 401 cannot reach
        // here: /api/auth/login is exempt from the client middleware.)
        throw new Error(SERVER_UNREACHABLE_MESSAGE);
      }
      const { data, error } = result;
      if (error || !data) {
        throw new Error(detailMessage(error) ?? "Couldn’t sign in. Try again.");
      }
      return data;
    },
    onSuccess: (status) => {
      sessionEstablished(queryClient, status);
    },
  });
}

/**
 * What both cookie-minting routes do to this browser once the server has said
 * yes — sign-in and first-run setup alike, because `POST /api/auth/setup`
 * returns the same `AuthStatus` behind the same `Set-Cookie` block as login
 * (`backend/app/api/auth.py`), so the app is in exactly the state a sign-in
 * leaves it in.
 *
 * One function rather than two identical `onSuccess` bodies: the cache-seeding
 * half and the asset bump were written for login and are just as load-bearing
 * on setup, and a copy would be free to lose one of them.
 */
function sessionEstablished(
  queryClient: ReturnType<typeof useQueryClient>,
  status: AuthStatus,
): void {
  // The response IS an AuthStatus, so the guard's query starts warm instead of
  // round-tripping again on the way into the shell.
  queryClient.setQueryData(AUTH_STATUS_KEY, status);
  markAuthenticated();
  // Whatever this browser cached belongs to a session that has been away;
  // invalidating gives the shell fresh data, and the asset bump remounts every
  // <img> whose bytes may have changed meanwhile. It also covers the gap a
  // FRESH EventSource cannot: its catch-up only runs on a RE-connect
  // (useEventStream's `connected` flag starts false), so a stream opened after
  // sign-in replays nothing.
  invalidateLibraryContent(queryClient);
  bumpAssetVersion();
}

/**
 * A refusal from one of the two password-WRITING routes, carrying the status
 * the server answered with.
 *
 * The status is on the error because the two forms branch on it and the
 * sentence alone cannot be branched on: the setup form treats a 409 as "someone
 * configured a password while this page was open" and re-checks rather than
 * showing text, and the change form paints a 409 (the env override is active)
 * as a notice while a 403 (wrong current password) is an inline field error
 * that keeps the form. Matching on the server's prose instead would break the
 * moment the prose is reworded.
 *
 * `status` is null for the one failure the server did not author: a request
 * that never got an answer at all.
 */
export class PasswordRequestError extends Error {
  readonly status: number | null;

  constructor(message: string, status: number | null) {
    super(message);
    this.name = "PasswordRequestError";
    this.status = status;
  }
}

/** The result shape both password routes return — `AuthStatus` on success, an
 * `ErrorDetail`-ish body otherwise. Widened from openapi-fetch's union so ONE
 * helper can run both calls. */
interface AuthStatusResult {
  data?: AuthStatus;
  error?: unknown;
  response: Response;
}

/**
 * Run a password write and normalise its three failure shapes into one
 * `PasswordRequestError`.
 *
 * `send` is a thunk rather than a path + body, because the two routes take
 * different bodies and openapi-fetch types each call site precisely — passing
 * the already-typed call in keeps that check at the call site instead of
 * loosening it here.
 *
 * An `UnauthenticatedError` is re-thrown untouched: `POST /api/auth/password`
 * is GATED, so a 401 from it really is "your session ended", and the client
 * middleware has already flipped the store to bounce this browser to /login.
 * Swallowing it into a form error would leave the user typing into a page the
 * app is navigating away from.
 */
async function writePassword(
  send: () => Promise<AuthStatusResult>,
  fallback: string,
): Promise<AuthStatus> {
  let result: AuthStatusResult;
  try {
    result = await send();
  } catch (cause) {
    if (cause instanceof UnauthenticatedError) {
      throw cause;
    }
    // No answer at all — the server is down, the proxy dropped it, DNS failed.
    // Same sentence as sign-in and sign-out use for the same fact.
    throw new PasswordRequestError(SERVER_UNREACHABLE_MESSAGE, null);
  }
  const { data, error, response } = result;
  if (error || !data) {
    throw new PasswordRequestError(
      detailMessage(error) ?? fallback,
      response.status,
    );
  }
  return data;
}

/**
 * Set the FIRST password on a server that has none, and take the cookie it
 * mints.
 *
 * Available only while `password_source` is `"none"`; the route answers 409 the
 * moment any source exists, which is how a second browser that had this form
 * open learns it lost the race (see the setup form in pages/LoginPage.tsx).
 */
export function useSetupPassword() {
  const queryClient = useQueryClient();
  return useMutation<AuthStatus, PasswordRequestError, string>({
    // Same reasoning as useLogin: the password rides in `variables`, which
    // TanStack would otherwise keep readable for `gcTime` after the form
    // unmounts.
    gcTime: 0,
    mutationFn: (password) =>
      writePassword(
        () => client.POST("/api/auth/setup", { body: { password } }),
        "Couldn’t set the password. Try again.",
      ),
    onSuccess: (status) => {
      sessionEstablished(queryClient, status);
    },
  });
}

/**
 * Replace the stored password, and take the re-minted cookie for THIS browser.
 *
 * Every other live session is signed out by this — the session signing key is
 * derived from the password hash — and the panel says so, because it is the
 * only revocation this stateless session design has.
 */
export function useChangePassword() {
  const queryClient = useQueryClient();
  return useMutation<
    AuthStatus,
    PasswordRequestError,
    { currentPassword: string; newPassword: string }
  >({
    gcTime: 0,
    mutationFn: ({ currentPassword, newPassword }) =>
      writePassword(
        () =>
          client.POST("/api/auth/password", {
            body: {
              current_password: currentPassword,
              new_password: newPassword,
            },
          }),
        "Couldn’t change the password. Try again.",
      ),
    onSuccess: (status) => {
      // Only the status cache, and deliberately NOT `sessionEstablished`: this
      // browser was already signed in, its cached library data is still its
      // own, and bumping the asset version would remount every <img> in the app
      // for a change that touched no bytes. What DID change is
      // `password_source`, which the panel itself renders.
      queryClient.setQueryData(AUTH_STATUS_KEY, status);
    },
  });
}

/** Expire the cookie in this browser. The server keeps no session list, so
 * this is the browser's half only (README §Authentication). */
export function useLogout() {
  return useMutation<void, Error, void>({
    mutationFn: async () => {
      let response: Response;
      try {
        ({ response } = await client.POST("/api/auth/logout"));
      } catch (error) {
        // A 401 is the ASKED-FOR outcome, not a failure. /api/auth/logout is
        // itself gated, so an expired cookie — or an operator rotating the
        // password hash — refuses the very request that would clear it, and
        // the session it was going to clear is already gone. Reporting an
        // error here deadlocked the one control that gets the user out: every
        // retry reproduced the same 401, forever.
        if (error instanceof UnauthenticatedError) {
          return;
        }
        // Everything else that REJECTS is a request with no answer, and its
        // message is the browser's raw "Failed to fetch" — which this hook
        // used to rethrow verbatim into `SignOutButton`'s toast. Same fact and
        // same sentence as the sign-in form's (see useLogin above); the
        // ANSWERED failure below keeps its own wording, because a server that
        // replied 500 is emphatically reachable.
        throw new Error(SERVER_UNREACHABLE_MESSAGE);
      }
      if (!response.ok) {
        throw new Error("Couldn’t sign out. Try again.");
      }
    },
    onSuccess: () => {
      // The cached AuthStatus still says `authenticated: true`; the store is
      // what makes the gate (and the login page's already-signed-in redirect)
      // disagree with it, exactly as it does for an expired cookie. Emptying
      // the cache is NOT done here: it hangs off this same flip, one level up,
      // so an expiry gets it too (see useClearCacheOnSignOut).
      markUnauthenticated();
    },
  });
}
