import { render, screen } from "@testing-library/react";
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

  expect(screen.getByText("Something went wrong")).toBeInTheDocument();
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
