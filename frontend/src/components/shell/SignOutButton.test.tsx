import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { createMemoryRouter, RouterProvider, useLocation } from "react-router";
import { beforeEach, describe, expect, test, vi } from "vitest";

import { markAuthenticated } from "@/api/authStore";
import { SignOutButton } from "@/components/shell/SignOutButton";
import { RequireAuth } from "@/components/system/RequireAuth";
import { server } from "@/test/msw-server";

// The shell's AppToaster is not mounted here, and sonner is the app's
// mutation-failure channel — so the toast is captured rather than rendered
// (the activityToasts / ReviewPage dialect).
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));
import { toast } from "sonner";

const STATUS_URL = `${window.location.origin}/api/auth/status`;
const LOGOUT_URL = `${window.location.origin}/api/auth/logout`;

function LocationProbe() {
  const loc = useLocation();
  return <div data-testid="location">{loc.pathname}</div>;
}

/** The button behind the real guard, because signing out has no navigation of
 * its own: clearing the cookie flips the store and RequireAuth carries it to
 * /login, exactly as a mid-session 401 does. Rendering it bare would test the
 * request and nothing else. */
function renderInGuardedShell() {
  const router = createMemoryRouter(
    [
      { path: "/login", element: <LocationProbe /> },
      {
        path: "*",
        element: (
          <RequireAuth>
            <SignOutButton />
          </RequireAuth>
        ),
      },
    ],
    { initialEntries: ["/browse"] },
  );
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const view = render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return { view, queryClient };
}

beforeEach(() => {
  markAuthenticated();
  vi.mocked(toast.error).mockClear();
  server.use(
    http.get(STATUS_URL, () =>
      HttpResponse.json({ authenticated: true, password_set: true }),
    ),
  );
});

describe("SignOutButton", () => {
  test("is quiet icon chrome with a name, not a labelled push button", async () => {
    renderInGuardedShell();

    const button = await screen.findByRole("button", { name: "Sign out" });
    // Icon-only, in ActivityButton's dialect — the accessible name is the
    // whole label, so it must not be left to the glyph.
    expect(button).toHaveAttribute("data-variant", "ghost");
    expect(button).toHaveTextContent("");
  });

  test("clearing the cookie carries the user out to /login", async () => {
    let posted = 0;
    server.use(
      http.post(LOGOUT_URL, () => {
        posted += 1;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    renderInGuardedShell();

    await userEvent.click(
      await screen.findByRole("button", { name: "Sign out" }),
    );

    await waitFor(() =>
      expect(screen.getByTestId("location")).toHaveTextContent("/login"),
    );
    expect(posted).toBe(1);
  });

  test("a failed sign-out leaves the user where they are, and SAYS so", async () => {
    // The cookie is still live, so pretending otherwise would strand them on a
    // sign-in page while the session they still hold keeps working. But
    // staying put is only half the behaviour: without the toast this is a
    // click that produces no navigation, no error and no acknowledgement —
    // the hook's authored message reaching nobody.
    server.use(
      http.post(LOGOUT_URL, () => new HttpResponse(null, { status: 500 })),
    );
    renderInGuardedShell();

    await userEvent.click(
      await screen.findByRole("button", { name: "Sign out" }),
    );

    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Sign out" }),
      ).toBeEnabled(),
    );
    expect(screen.queryByTestId("location")).not.toBeInTheDocument();
    expect(toast.error).toHaveBeenCalledWith("Couldn’t sign out. Try again.");
  });

  test("an unreachable server is explained, not reported in browser jargon", async () => {
    // `useLogout` caught only UnauthenticatedError and rethrew everything else
    // AS IT WAS, so a dead server arrived at the toast as the browser's own
    // words — literally "Failed to fetch" in Chromium, "NetworkError when
    // attempting to fetch resource" in Firefox. `useLogin` was fixed for
    // exactly this a slice earlier and this half was missed, which is why the
    // sentence is now one shared constant rather than two literals.
    server.use(http.post(LOGOUT_URL, () => HttpResponse.error()));
    renderInGuardedShell();

    await userEvent.click(
      await screen.findByRole("button", { name: "Sign out" }),
    );

    await waitFor(() => expect(toast.error).toHaveBeenCalled());
    // The literal, not the imported constant: an oracle that reads the same
    // constant the code does would pass whatever that constant said.
    expect(toast.error).toHaveBeenCalledWith(
      "Can’t reach the server. Check that MusicDrop is running, then try again.",
    );
    expect(vi.mocked(toast.error).mock.calls[0][0]).not.toMatch(/fetch/i);
    // Still signed in, because nothing cleared the cookie.
    expect(screen.queryByTestId("location")).not.toBeInTheDocument();
  });

  test("a 401 on the logout route IS a sign-out, not a failure", async () => {
    // The deadlock this replaced: the cookie expired (or the operator rotated
    // the password hash), so the gated logout route refuses the one request
    // that would clear it. Reported as an error, the user was stuck inside the
    // shell with a dead session and every retry reproduced it forever.
    server.use(
      http.post(LOGOUT_URL, () =>
        HttpResponse.json({ detail: "authentication required" }, { status: 401 }),
      ),
    );
    const { queryClient } = renderInGuardedShell();
    queryClient.setQueryData(["beets-config"], {
      yaml_text: "plex:\n  token: super-secret\n",
    });

    await userEvent.click(
      await screen.findByRole("button", { name: "Sign out" }),
    );

    await waitFor(() =>
      expect(screen.getByTestId("location")).toHaveTextContent("/login"),
    );
    // Neither the bounce NOR the cache clear discriminates any more, and
    // saying so is the point: the client middleware flips the store on its way
    // past, and BOTH of those now hang off that flip (RequireAuth ->
    // useClearCacheOnSignOut), so they happen whether this hook calls the 401
    // a success or a failure. They are asserted as invariants, not as the
    // oracle.
    await waitFor(() =>
      expect(queryClient.getQueryData(["beets-config"])).toBeUndefined(),
    );
    // THIS is what discriminates: the error arm would report the refusal, and
    // that is the deadlock — a user told sign-out failed, on a session that is
    // already gone, whose every retry reproduces the same 401.
    expect(toast.error).not.toHaveBeenCalled();
  });

  test("signing out empties the cache, not just the session flag", async () => {
    // `/api/config` caches the raw config.yaml with its secrets UNMASKED under
    // ["beets-config"], and TanStack keeps a query readable for gcTime (5
    // minutes by default) after its last observer unmounts. Marking the
    // session gone while leaving the bytes in place makes sign-out a promise
    // the cache does not keep.
    server.use(
      http.post(LOGOUT_URL, () => new HttpResponse(null, { status: 204 })),
    );
    const { queryClient } = renderInGuardedShell();
    queryClient.setQueryData(["beets-config"], {
      yaml_text: "plex:\n  token: super-secret\n",
    });

    await userEvent.click(
      await screen.findByRole("button", { name: "Sign out" }),
    );

    await waitFor(() =>
      expect(screen.getByTestId("location")).toHaveTextContent("/login"),
    );
    expect(queryClient.getQueryData(["beets-config"])).toBeUndefined();
  });
});
