import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

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
        // for that are "Failed to fetch" / "NetworkError when attempting to
        // fetch resource", which is jargon on the one screen whose whole job
        // is explaining why signing in isn't working. (The gate's 401 cannot
        // reach here: /api/auth/login is exempt from the client middleware.)
        throw new Error(
          "Can’t reach the server. Check that MusicDrop is running, then try again.",
        );
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
  const queryClient = useQueryClient();
  return useMutation<void, Error, void>({
    mutationFn: async () => {
      try {
        const { response } = await client.POST("/api/auth/logout");
        if (!response.ok) {
          throw new Error("Couldn’t sign out. Try again.");
        }
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
        throw error;
      }
    },
    onSuccess: () => {
      // The cached AuthStatus still says `authenticated: true`; the store is
      // what makes the gate (and the login page's already-signed-in redirect)
      // disagree with it, exactly as it does for an expired cookie.
      markUnauthenticated();
      // Sign out is a promise about THIS browser, and the cache is where it
      // would otherwise be broken: `["beets-config"]` holds the raw
      // config.yaml with its secrets unmasked, and the default gcTime keeps
      // every cached answer readable for five minutes after the last observer
      // unmounts. `clear()` (not `invalidate`) because invalidation only marks
      // data stale — it leaves the bytes in place.
      queryClient.clear();
    },
  });
}
