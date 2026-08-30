import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { createMemoryRouter, RouterProvider, useLocation } from "react-router";
import { beforeEach, describe, expect, test } from "vitest";

import { markAuthenticated } from "@/api/authStore";
import { SignOutButton } from "@/components/shell/SignOutButton";
import { RequireAuth } from "@/components/system/RequireAuth";
import { server } from "@/test/msw-server";

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
  return render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  markAuthenticated();
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

  test("a failed sign-out leaves the user where they are", async () => {
    // The cookie is still live, so pretending otherwise would strand them on a
    // sign-in page while the session they still hold keeps working.
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
  });
});
