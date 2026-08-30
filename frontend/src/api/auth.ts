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
      // The login response IS an AuthStatus, so the guard's query starts warm
      // instead of round-tripping again on the way into the shell.
      queryClient.setQueryData(AUTH_STATUS_KEY, status);
      markAuthenticated();
      // Whatever this browser cached belongs to a session that has been away;
      // invalidating gives the shell fresh data, and the asset bump remounts
      // every <img> whose bytes may have changed meanwhile. It also covers the
      // gap a FRESH EventSource cannot: its catch-up only runs on a RE-connect
      // (useEventStream's `connected` flag starts false), so a stream opened
      // after sign-in replays nothing.
      invalidateLibraryContent(queryClient);
      bumpAssetVersion();
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
