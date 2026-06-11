import { render, screen, waitFor } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, expect, test, vi } from "vitest";

import { RouteErrorBoundary } from "@/components/system/RouteErrorBoundary";

function Boom(): never {
  throw new Error("kaboom from the page");
}

afterEach(() => vi.restoreAllMocks());

test("a crashing page renders the styled fallback with detail + escape routes", () => {
  // React logs the caught render error — silence it so the suite stays clean.
  vi.spyOn(console, "error").mockImplementation(() => {});

  const router = createMemoryRouter([
    {
      errorElement: <RouteErrorBoundary />,
      children: [{ path: "/", element: <Boom /> }],
    },
  ]);
  render(<RouterProvider router={router} />);

  // The title appears twice: the boundary's sr-only h1 (the announce/focus
  // target) plus EmptyState's visible <p>.
  expect(screen.getAllByText("Something went wrong")).toHaveLength(2);
  // The raw message stays visible (muted) so bug reports carry it.
  expect(screen.getByText(/kaboom from the page/)).toBeInTheDocument();
  expect(
    screen.getByRole("button", { name: /reload page/i }),
  ).toBeInTheDocument();
  expect(screen.getByRole("link", { name: /back to overview/i })).toHaveAttribute(
    "href",
    "/",
  );
});

test("the error is announced (role=alert) and focus lands on the boundary's h1", async () => {
  vi.spyOn(console, "error").mockImplementation(() => {});

  const router = createMemoryRouter([
    {
      errorElement: <RouteErrorBoundary />,
      children: [{ path: "/", element: <Boom /> }],
    },
  ]);
  render(<RouterProvider router={router} />);

  // Announced for AT — the boundary owns the live region (EmptyState
  // deliberately doesn't), so the crash isn't silent.
  const alert = screen.getByRole("alert");
  expect(alert).toHaveTextContent(/something went wrong/i);
  expect(alert).toHaveTextContent(/kaboom from the page/);

  // The h1 (tabIndex -1) takes focus on mount, so keyboard/SR users land on
  // the error instead of <body> / RouteAnnouncer's stale target.
  const h1 = screen.getByRole("heading", {
    level: 1,
    name: /something went wrong/i,
  });
  expect(h1).toHaveAttribute("tabindex", "-1");
  await waitFor(() => expect(h1).toHaveFocus());
});
