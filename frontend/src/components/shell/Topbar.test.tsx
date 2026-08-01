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

/** What a release image actually reports: the Docker build arg is the git tag,
 * so `version` arrives ALREADY v-prefixed. Mocks used to say "0.1.0", which is
 * why the "vv0.29.1" double-prefix shipped unnoticed. */
const RELEASE_VERSION = "v0.29.1";
/** What a dev / CI build reports — the literal default from settings.version. */
const DEV_VERSION = "dev";

describe("HealthStatus (Topbar copy)", () => {
  test("conveys a reachable backend with a non-color text label", async () => {
    server.use(
      http.get(HEALTH_URL, () =>
        HttpResponse.json({ status: "ok", version: RELEASE_VERSION }),
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
        HttpResponse.json({ status: "ok", version: RELEASE_VERSION }),
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

describe("HealthStatus version", () => {
  test("shows a release build's version with exactly one leading v", async () => {
    server.use(
      http.get(HEALTH_URL, () =>
        HttpResponse.json({ status: "ok", version: RELEASE_VERSION }),
      ),
    );

    renderWithProviders(<HealthStatus />);

    // Visible on screen next to the status label — no hover required.
    expect(await screen.findByText(RELEASE_VERSION)).toBeInTheDocument();
    // Regression guard: the old `(v${data.version})` produced "vv0.29.1".
    expect(screen.queryByText(/vv/)).not.toBeInTheDocument();
    const status = screen.getByRole("status");
    expect(status).toHaveAttribute("title", "Backend online (v0.29.1)");
    expect(status).toHaveAttribute("aria-label", "Backend online (v0.29.1)");
  });

  test("shows a dev build's version without inventing a v prefix", async () => {
    server.use(
      http.get(HEALTH_URL, () =>
        HttpResponse.json({ status: "ok", version: DEV_VERSION }),
      ),
    );

    renderWithProviders(<HealthStatus />);

    expect(await screen.findByText(DEV_VERSION)).toBeInTheDocument();
    expect(screen.queryByText("vdev")).not.toBeInTheDocument();
    const status = screen.getByRole("status");
    expect(status).toHaveAttribute("title", "Backend online (dev)");
    expect(status).toHaveAttribute("aria-label", "Backend online (dev)");
  });

  test("a reachable backend with an empty version still describes itself as online", async () => {
    // `settings.version` is a plain `str` with no min-length and is overridable
    // at runtime via MUSICDROP_VERSION, so "" arrives from a REACHABLE backend.
    server.use(
      http.get(HEALTH_URL, () =>
        HttpResponse.json({ status: "ok", version: "" }),
      ),
    );

    renderWithProviders(<HealthStatus />);

    // Reachability — not the version string — decides the description, so the
    // accessible name can never contradict the visible "Online" label.
    expect(await screen.findByText(/online/i)).toBeInTheDocument();
    const status = screen.getByRole("status");
    expect(status).toHaveAttribute("aria-label", "Backend online");
    expect(status).toHaveAttribute("title", "Backend online");
    // Reads naturally: no dangling "()" where the version would have been.
    expect(status.getAttribute("aria-label")).not.toMatch(/\(\s*\)/);
  });

  test("keeps the static version OUT of the role=status live region", async () => {
    server.use(
      http.get(HEALTH_URL, () =>
        HttpResponse.json({ status: "ok", version: RELEASE_VERSION }),
      ),
    );

    renderWithProviders(<HealthStatus />);

    // The version never changes while the app runs; announcing it on every
    // Online→Offline flip would be noise. Status stays in the live region.
    const version = await screen.findByText(RELEASE_VERSION);
    expect(screen.getByRole("status")).not.toContainElement(version);
  });

  test("compact rail renders no visible version (the title carries it)", async () => {
    server.use(
      http.get(HEALTH_URL, () =>
        HttpResponse.json({ status: "ok", version: RELEASE_VERSION }),
      ),
    );

    renderWithProviders(<HealthStatus compact />);

    expect(await screen.findByText(/online/i)).toBeInTheDocument();
    expect(screen.queryByText(RELEASE_VERSION)).not.toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveAttribute(
      "title",
      "Backend online (v0.29.1)",
    );
  });

  test("offline still describes the backend without a version", async () => {
    server.use(
      http.get(HEALTH_URL, () => new HttpResponse(null, { status: 500 })),
    );

    renderWithProviders(<HealthStatus />);

    expect(await screen.findByText(/offline/i)).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveAttribute(
      "title",
      "Backend unreachable",
    );
  });
});
