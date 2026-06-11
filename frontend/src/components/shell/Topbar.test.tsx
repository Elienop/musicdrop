import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { useLocation } from "react-router";
import { describe, expect, test } from "vitest";

import { AppTopbar, HealthStatus } from "@/components/shell/Topbar";
import { renderWithProviders } from "@/test/render";
import { server } from "@/test/msw-server";

const HEALTH_URL = `${window.location.origin}/api/health`;

/** Exposes the live router location so tests can assert URL changes. */
function LocationProbe() {
  const location = useLocation();
  return (
    <div data-testid="location">{`${location.pathname}${location.search}`}</div>
  );
}

describe("AppTopbar", () => {
  test("renders the hamburger, a labelled search box, and the right slot", () => {
    renderWithProviders(
      <AppTopbar>
        <span>slot-content</span>
      </AppTopbar>,
    );

    expect(
      screen.getByRole("button", { name: "Open navigation" }),
    ).toBeInTheDocument();
    const box = screen.getByRole("searchbox", { name: /search library/i });
    expect(screen.getByRole("search")).toContainElement(box);
    expect(screen.getByText("slot-content")).toBeInTheDocument();
    // The shortcut hint is visible chrome (decorative for AT).
    expect(screen.getByText("⌘K")).toBeInTheDocument();
  });

  test("typing in the search box debounces before navigating to /search?q=", async () => {
    renderWithProviders(
      <>
        <AppTopbar />
        <LocationProbe />
      </>,
    );

    await userEvent.type(
      screen.getByRole("searchbox", { name: /search library/i }),
      "radio",
    );

    const loc = screen.getByTestId("location");
    // Synchronously after the keystrokes, the 250ms debounce has NOT fired,
    // so the URL has NOT moved — this proves the debounce survived the move.
    expect(loc).not.toHaveTextContent("/search");
    expect(loc).not.toHaveTextContent("q=radio");

    await waitFor(() => {
      expect(loc).toHaveTextContent("/search");
      expect(loc).toHaveTextContent("q=radio");
    });
  });

  test("re-syncs the box from the URL on external navigation (deep link)", () => {
    renderWithProviders(<AppTopbar />, { route: "/search?q=foo" });

    expect(
      screen.getByRole("searchbox", { name: /search library/i }),
    ).toHaveValue("foo");
  });

  test("Cmd+K focuses the search box from anywhere", async () => {
    const user = userEvent.setup();
    renderWithProviders(<AppTopbar />);

    await user.keyboard("{Meta>}k{/Meta}");

    expect(
      screen.getByRole("searchbox", { name: /search library/i }),
    ).toHaveFocus();
  });

  test("Ctrl+K focuses the search box from anywhere", async () => {
    const user = userEvent.setup();
    renderWithProviders(<AppTopbar />);

    await user.keyboard("{Control>}k{/Control}");

    expect(
      screen.getByRole("searchbox", { name: /search library/i }),
    ).toHaveFocus();
  });
});

describe("HealthStatus (Topbar copy)", () => {
  test("conveys a reachable backend with a non-color text label", async () => {
    server.use(
      http.get(HEALTH_URL, () =>
        HttpResponse.json({ status: "ok", version: "0.1.0" }),
      ),
    );

    renderWithProviders(<HealthStatus />);

    expect(await screen.findByText(/online/i)).toBeInTheDocument();
    expect(screen.getByRole("status")).toBeInTheDocument();
  });

  test("conveys an unreachable backend with a non-color text label", async () => {
    server.use(
      http.get(HEALTH_URL, () => new HttpResponse(null, { status: 500 })),
    );

    renderWithProviders(<HealthStatus />);

    expect(await screen.findByText(/offline/i)).toBeInTheDocument();
  });

  test("compact mode keeps the status text in the live region (sr-only, not hidden)", async () => {
    server.use(
      http.get(HEALTH_URL, () =>
        HttpResponse.json({ status: "ok", version: "0.1.0" }),
      ),
    );

    renderWithProviders(<HealthStatus compact />);

    // Live regions announce content changes, not aria-label changes — the
    // collapsed rail must keep an announceable text node, just visually hidden.
    const label = await screen.findByText(/online/i);
    expect(label).toHaveClass("sr-only");
    expect(label.className).not.toMatch(/\bhidden\b/);
    expect(screen.getByRole("status")).toContainElement(label);
  });
});
