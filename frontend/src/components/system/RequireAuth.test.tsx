import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { createMemoryRouter, RouterProvider, useLocation } from "react-router";
import { beforeEach, describe, expect, test } from "vitest";

import { markAuthenticated, markUnauthenticated } from "@/api/authStore";
import { RequireAuth } from "@/components/system/RequireAuth";
import { server } from "@/test/msw-server";

const STATUS_URL = `${window.location.origin}/api/auth/status`;

/** Reports the location AND the state RequireAuth stashed on it. */
function LoginStandIn() {
  const loc = useLocation();
  const from = (loc.state as { from?: { pathname?: string } } | null)?.from;
  return (
    <div>
      <div data-testid="location">{loc.pathname}</div>
      <div data-testid="intended">{from?.pathname ?? "none"}</div>
    </div>
  );
}

function renderGuard(initialEntry = "/artists") {
  const router = createMemoryRouter(
    [
      { path: "/login", element: <LoginStandIn /> },
      {
        path: "*",
        element: (
          <RequireAuth>
            <div>shell contents</div>
          </RequireAuth>
        ),
      },
    ],
    { initialEntries: [initialEntry] },
  );
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  markAuthenticated();
});

describe("RequireAuth", () => {
  test("admits a signed-in browser to the shell", async () => {
    server.use(
      http.get(STATUS_URL, () =>
        HttpResponse.json({ authenticated: true, password_set: true }),
      ),
    );
    renderGuard();

    expect(await screen.findByText("shell contents")).toBeInTheDocument();
  });

  test("sends a signed-out browser to /login, remembering the page asked for", async () => {
    server.use(
      http.get(STATUS_URL, () =>
        HttpResponse.json({ authenticated: false, password_set: true }),
      ),
    );
    renderGuard("/artists");

    await waitFor(() =>
      expect(screen.getByTestId("location")).toHaveTextContent("/login"),
    );
    // Without this, signing in would dump everyone on the Overview whatever
    // they had bookmarked.
    expect(screen.getByTestId("intended")).toHaveTextContent("/artists");
    expect(screen.queryByText("shell contents")).not.toBeInTheDocument();
  });

  test("renders neither shell nor redirect while the probe is in flight", async () => {
    server.use(
      http.get(STATUS_URL, () =>
        HttpResponse.json({ authenticated: true, password_set: true }),
      ),
    );
    renderGuard();

    // A flash of dead chrome, or of a sign-in form about to be replaced, is
    // worse than a moment of nothing.
    expect(screen.queryByText("shell contents")).not.toBeInTheDocument();
    expect(screen.queryByTestId("location")).not.toBeInTheDocument();
    expect(screen.getByRole("status")).toBeInTheDocument();

    expect(await screen.findByText("shell contents")).toBeInTheDocument();
  });

  test("a mid-session refusal moves an already-rendered shell to /login", async () => {
    // The expired-cookie path: the status probe answered "yes" minutes ago and
    // its answer is still cached, so only the transport store knows better.
    server.use(
      http.get(STATUS_URL, () =>
        HttpResponse.json({ authenticated: true, password_set: true }),
      ),
    );
    renderGuard("/browse");
    expect(await screen.findByText("shell contents")).toBeInTheDocument();

    act(() => {
      markUnauthenticated();
    });

    await waitFor(() =>
      expect(screen.getByTestId("location")).toHaveTextContent("/login"),
    );
    expect(screen.getByTestId("intended")).toHaveTextContent("/browse");
  });

  test("an unreachable backend keeps the shell rather than a form that can't help", async () => {
    // /api/auth/status is EXEMPT from the gate and answers 200 for a
    // cookie-less caller, so a failure here means the backend is down — not
    // that the session ended. The shell's own health row says "Offline"; a
    // sign-in page would just be a second thing that doesn't work.
    server.use(
      http.get(STATUS_URL, () => new HttpResponse(null, { status: 503 })),
    );
    renderGuard();

    expect(await screen.findByText("shell contents")).toBeInTheDocument();
    expect(screen.queryByTestId("location")).not.toBeInTheDocument();
  });
});
