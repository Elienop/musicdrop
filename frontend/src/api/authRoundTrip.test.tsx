import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { createMemoryRouter, RouterProvider, useLocation } from "react-router";
import { beforeEach, describe, expect, test, vi } from "vitest";

import { markAuthenticated, markUnauthenticated } from "@/api/authStore";
import { SignOutButton } from "@/components/shell/SignOutButton";
import { RequireAuth } from "@/components/system/RequireAuth";
import { LoginPage } from "@/pages/LoginPage";
import { server } from "@/test/msw-server";

// No AppToaster here, and sonner is where mutation failures go — captured
// rather than rendered, as SignOutButton's own tests do. Asserted on: a
// journey that ends back inside the shell must not have reported a failure on
// the way.
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));
import { toast } from "sonner";

const STATUS_URL = `${window.location.origin}/api/auth/status`;
const LOGIN_URL = `${window.location.origin}/api/auth/login`;
const LOGOUT_URL = `${window.location.origin}/api/auth/logout`;

/**
 * The whole SUITE tested transitions INTO signed-out and none back out, which
 * is how two lines came to survive it: deleting `markAuthenticated()` from
 * `useLogin.onSuccess` (api/auth.ts) made sign-out a one-way door, and
 * deleting the `!signedOut &&` conjunct from LoginPage's redirect turned an
 * expiry into an infinite redirect loop. Both are about the RETURN journey, so
 * both need a test that travels it — the guard, the sign-out control and the
 * sign-in form in one router, with a server that changes its mind.
 */

/** A server with a session it actually keeps: `/api/auth/status` reports the
 * flag, `login` sets it, `logout` clears it. The two production lines under
 * test are only visible to a test whose backend answers CONSISTENTLY across a
 * whole journey — a fixed handler per phase would hide them. */
function sessionServer(startSignedIn: boolean) {
  const session = { signedIn: startSignedIn };
  server.use(
    http.get(STATUS_URL, () =>
      HttpResponse.json({
        authenticated: session.signedIn,
        password_set: true,
      }),
    ),
    http.post(LOGIN_URL, () => {
      session.signedIn = true;
      return HttpResponse.json({ authenticated: true, password_set: true });
    }),
    http.post(LOGOUT_URL, () => {
      session.signedIn = false;
      return new HttpResponse(null, { status: 204 });
    }),
  );
  return session;
}

function LocationProbe() {
  const loc = useLocation();
  return <div data-testid="location">{loc.pathname}</div>;
}

/** The real three pieces in one router: the guard admitting (or bouncing) the
 * shell, the sign-out control inside it, and the actual LoginPage — not a
 * stand-in, because the redirect being tested is the LoginPage's own. */
function renderApp(initialEntry = "/browse") {
  const router = createMemoryRouter(
    [
      {
        path: "/login",
        element: (
          <>
            <LoginPage />
            <LocationProbe />
          </>
        ),
      },
      {
        path: "*",
        element: (
          <>
            <RequireAuth>
              <div>shell contents</div>
              <SignOutButton />
            </RequireAuth>
            <LocationProbe />
          </>
        ),
      },
    ],
    { initialEntries: [initialEntry] },
  );
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  // Every pathname the router actually settles on, in order. A redirect LOOP
  // is not an absence to assert — it is this list growing without end — so the
  // "no loop" half of the second test is pinned by reading it rather than by
  // hoping React throws.
  const visited: string[] = [];
  const record = (pathname: string) => {
    if (visited.at(-1) !== pathname) visited.push(pathname);
  };
  record(router.state.location.pathname);
  router.subscribe((state) => {
    record(state.location.pathname);
  });
  render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return { queryClient, visited };
}

beforeEach(() => {
  markAuthenticated();
  vi.mocked(toast.error).mockClear();
});

describe("the session's return journey", () => {
  test("sign out, then sign back in, and the shell is reachable again", async () => {
    // KILLS: `markAuthenticated()` in useLogin.onSuccess (api/auth.ts).
    // Without it the store's `signedOut` stays true forever, so
    // useAuthGateState answers "unauthenticated" however good the new cookie
    // is — a successful sign-in bounces straight back to /login and sign out
    // is a one-way door for the life of the tab.
    sessionServer(true);
    renderApp("/browse");

    expect(await screen.findByText("shell contents")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Sign out" }));

    // Out: the store flip carries the guard to the sign-in form.
    expect(await screen.findByLabelText("Password")).toBeInTheDocument();
    expect(screen.getByTestId("location")).toHaveTextContent("/login");

    await userEvent.type(screen.getByLabelText("Password"), "hunter2");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));

    // And back in — to the page they were on, not to /login again.
    expect(await screen.findByText("shell contents")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByTestId("location")).toHaveTextContent("/browse"),
    );
    expect(screen.queryByLabelText("Password")).not.toBeInTheDocument();
    expect(toast.error).not.toHaveBeenCalled();
  });

  test("a session that ends mid-visit lands on a usable form, not a redirect loop", async () => {
    // KILLS: the `!signedOut &&` conjunct in LoginPage (pages/LoginPage.tsx).
    //
    // The state this models is one the code explicitly ACCEPTS rather than
    // prevents (see the long note in api/authStore.ts): the transport store
    // and `GET /api/auth/status` are two witnesses to the same question, and
    // they can disagree — the store learns about a refusal the instant it
    // happens, while the exempt status endpoint answers for whatever cookie
    // the browser holds. Here the store has been flipped by a gated request's
    // 401 while the endpoint still says yes.
    //
    // Without the conjunct, LoginPage reads only the endpoint, redirects to
    // the remembered page, RequireAuth reads the store and bounces straight
    // back, and the two navigate at each other for as long as anyone watches.
    // (Whether that surfaces as React's "Maximum update depth exceeded"
    // depends on how fast the round trip is: measured here, each bounce waits
    // on the status query, so it is an endless slow ping-pong rather than a
    // synchronous overflow. Same fault, quieter symptom — which is why this
    // test reads the router's own trail instead of waiting for a throw.)
    // The store is the fresher witness, so it wins.
    sessionServer(true);
    const { visited } = renderApp("/browse");

    expect(await screen.findByText("shell contents")).toBeInTheDocument();

    // What the client middleware does on a gated 401, from the outside.
    act(() => {
      markUnauthenticated();
    });

    expect(await screen.findByLabelText("Password")).toBeInTheDocument();
    expect(screen.getByTestId("location")).toHaveTextContent("/login");
    // `getByRole`, not `getByText("Sign in")`: the card's own <h2> carries the
    // same words, so the plain text query matches two nodes and throws.
    expect(screen.getByRole("button", { name: "Sign in" })).toBeEnabled();

    // Give the status query every chance to re-answer "authenticated: true"
    // and start something. A settled page is unchanged by the wait; a looping
    // one adds two entries to the trail per round trip.
    await new Promise((resolve) => setTimeout(resolve, 300));

    expect(screen.getByLabelText("Password")).toBeInTheDocument();
    expect(screen.queryByText("shell contents")).not.toBeInTheDocument();
    // Arrived once and STAYED: the whole journey is /browse then /login, with
    // nothing after it.
    expect(visited).toEqual(["/browse", "/login"]);
  });

  test("an expiry empties the cache too, not just a Sign out click", async () => {
    // LOW 5: `queryClient.clear()` used to hang off `useLogout.onSuccess` —
    // the BUTTON — so an expiry bounced to /login with `["beets-config"]` (the
    // raw config.yaml, secrets UNMASKED) still readable in the JS heap for the
    // default five-minute gcTime. Nobody clicked anything, so nothing cleared.
    sessionServer(true);
    const { queryClient } = renderApp("/browse");
    expect(await screen.findByText("shell contents")).toBeInTheDocument();
    queryClient.setQueryData(["beets-config"], {
      raw: "slskd:\n  api_key: SUPERSECRET\n",
    });

    act(() => {
      markUnauthenticated();
    });

    await waitFor(() =>
      expect(queryClient.getQueryData(["beets-config"])).toBeUndefined(),
    );
  });
});
