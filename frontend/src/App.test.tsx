import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";
import { describe, expect, test } from "vitest";

import { App } from "@/App";
import { HealthStatus } from "@/components/shell/Topbar";
import { ArtistsPage } from "@/pages/artists/ArtistsPage";
import { renderWithProviders } from "@/test/render";
import { server } from "@/test/msw-server";

const HEALTH_URL = `${window.location.origin}/api/health`;
const ARTISTS_URL = `${window.location.origin}/api/artists`;
const SEARCH_URL = `${window.location.origin}/api/search`;
const ACTIVE_IMPORT_URL = `${window.location.origin}/api/imports/active`;
const ACQUISITION_URL = `${window.location.origin}/api/acquisition/status`;
const REORGANIZE_URL = `${window.location.origin}/api/reorganize/status`;
const LYRICS_URL = `${window.location.origin}/api/lyrics/backfill`;
const ARTIST_ART_URL = `${window.location.origin}/api/artists/art/backfill`;
const DISK_SYNC_URL = `${window.location.origin}/api/disk-sync/status`;

/** Idle handlers for every probe the shell polls (health + the Review badge
 * probe + the six activity sources), so shell tests are deterministic and
 * quiet under MSW's onUnhandledRequest:"error". Spread these AFTER any
 * per-test override — within one server.use() call, earlier handlers win. */
function idleShellHandlers() {
  return [
    http.get(HEALTH_URL, () =>
      HttpResponse.json({ status: "ok", version: "0.1.0" }),
    ),
    http.get(ARTISTS_URL, () => HttpResponse.json([])),
    http.get(ACTIVE_IMPORT_URL, () =>
      HttpResponse.json({
        active: false,
        origin: "manual",
        needs_review_count: 0,
      }),
    ),
    http.get(ACQUISITION_URL, () =>
      HttpResponse.json({
        phase: "idle",
        queued: 0,
        current: null,
        processed: 0,
        set_aside: 0,
        failed: 0,
        error: null,
        inbox_pending: 0,
      }),
    ),
    http.get(REORGANIZE_URL, () =>
      HttpResponse.json({
        phase: "idle",
        job_id: null,
        scope: null,
        total: 0,
        processed: 0,
        moved: 0,
        skipped: 0,
        failed: 0,
        current: null,
        error: null,
        artist: null,
        album_id: null,
        scope_label: "library",
      }),
    ),
    http.get(LYRICS_URL, () =>
      HttpResponse.json({
        phase: "idle",
        job_id: null,
        total: 0,
        processed: 0,
        found: 0,
        not_found: 0,
        failed: 0,
        skipped: 0,
        current: null,
        writes_enabled: false,
        error: null,
        album_id: null,
        scope_label: "library",
      }),
    ),
    http.get(ARTIST_ART_URL, () =>
      HttpResponse.json({
        phase: "idle",
        job_id: null,
        total: 0,
        processed: 0,
        written: 0,
        skipped: 0,
        failed: 0,
        current: null,
        error: null,
        artist: null,
        scope_label: "library",
      }),
    ),
    http.get(DISK_SYNC_URL, () =>
      HttpResponse.json({
        phase: "idle",
        job_id: null,
        total: 0,
        processed: 0,
        removed: 0,
        updated: 0,
        unchanged: 0,
        read_errors: 0,
        emptied_albums: 0,
        current: null,
        error: null,
        failures: [],
      }),
    ),
  ];
}

describe("HealthStatus", () => {
  test("conveys a reachable backend with a non-color text label", async () => {
    server.use(
      http.get(HEALTH_URL, () =>
        HttpResponse.json({ status: "ok", version: "0.1.0" }),
      ),
    );

    renderWithProviders(<HealthStatus />);

    // Non-color carrier: a visible text label, not just a colored dot.
    expect(await screen.findByText(/online/i)).toBeInTheDocument();
    // Status role still present for assistive tech.
    expect(screen.getByRole("status")).toBeInTheDocument();
  });

  test("conveys an unreachable backend with a non-color text label", async () => {
    server.use(
      http.get(HEALTH_URL, () => new HttpResponse(null, { status: 500 })),
    );

    renderWithProviders(<HealthStatus />);

    expect(await screen.findByText(/offline/i)).toBeInTheDocument();
  });
});

/** Exposes the live router location so tests can assert URL changes. */
function LocationProbe() {
  const loc = useLocation();
  return <div data-testid="location">{`${loc.pathname}${loc.search}`}</div>;
}

/** Mount the App shell the way the router nests it: roster at the index,
 * stub elements for the routes the shell links to. */
function renderShell(initialEntry = "/") {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <Routes>
          <Route element={<App />}>
            <Route index element={<ArtistsPage />} />
            <Route path="artists" element={<div>artists route</div>} />
            <Route path="search" element={<div>search route</div>} />
            <Route path="*" element={<div>other</div>} />
          </Route>
        </Routes>
        <LocationProbe />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("App shell", () => {
  test("the brand is a link to home, not a heading", async () => {
    server.use(...idleShellHandlers());
    renderShell();

    const brand = screen.getByRole("link", { name: "MusicDrop" });
    expect(brand).toHaveAttribute("href", "/");
    expect(
      screen.queryByRole("heading", { level: 1, name: "MusicDrop" }),
    ).not.toBeInTheDocument();
    // The routed page renders through the shell's <main> Outlet.
    expect(await screen.findByText(/no artists/i)).toBeInTheDocument();
  });

  test("the topbar slot mounts ActivityButton + HealthStatus, and the toast region exists", async () => {
    // Pins the slot wiring that regressed once mid-build (commit 5a55a10):
    // deleting <ActivityButton/>, <HealthStatus/>, or <AppToaster/> from
    // App.tsx must fail a test, not just a walkthrough.
    server.use(...idleShellHandlers());
    renderShell();

    expect(
      screen.getByRole("button", { name: "Activity" }),
    ).toBeInTheDocument();
    expect(await screen.findByText(/online/i)).toBeInTheDocument();
    // sonner's single live region (AppToaster) is mounted.
    expect(
      screen.getByRole("region", { name: /notifications/i }),
    ).toBeInTheDocument();
  });

  test("sidebar nav: Overview/Artists/Browse + Review/Add from folder; no Albums, no dead slots", async () => {
    server.use(...idleShellHandlers());
    renderShell();

    expect(screen.getByRole("link", { name: "Overview" })).toHaveAttribute(
      "href",
      "/",
    );
    expect(screen.getByRole("link", { name: "Artists" })).toHaveAttribute(
      "href",
      "/artists",
    );
    expect(screen.getByRole("link", { name: "Browse" })).toHaveAttribute(
      "href",
      "/browse",
    );
    // Renamed label (route unchanged).
    expect(
      screen.getByRole("link", { name: /add from folder/i }),
    ).toHaveAttribute("href", "/import");
    // Still no flat "Albums" tab — albums are reached via the spine / Browse.
    expect(
      screen.queryByRole("link", { name: "Albums" }),
    ).not.toBeInTheDocument();
    // Find music / Downloads are designed slots, NOT rendered until they ship.
    expect(
      screen.queryByRole("link", { name: /find music/i }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: /downloads/i }),
    ).not.toBeInTheDocument();
  });

  test("the skip link is the first focusable element and targets #main-content", async () => {
    server.use(...idleShellHandlers());
    renderShell();

    await userEvent.tab();
    const skip = screen.getByRole("link", { name: /skip to content/i });
    expect(skip).toHaveFocus();
    expect(skip).toHaveAttribute("href", "#main-content");

    const main = screen.getByRole("main");
    expect(main).toHaveAttribute("id", "main-content");
    expect(main).toHaveAttribute("tabindex", "-1");
  });

  test("the active sidebar item carries aria-current AND the violet text", async () => {
    server.use(...idleShellHandlers());
    renderShell("/");

    const overview = screen.getByRole("link", { name: "Overview" });
    expect(overview).toHaveAttribute("aria-current", "page");
    // aria-current must be STYLED, not bare (spec §1): violet text.
    expect(overview.className).toContain("text-primary-light");
    expect(screen.getByRole("link", { name: "Artists" })).not.toHaveAttribute(
      "aria-current",
    );
  });

  test("the Review badge counts pending decisions ONLY — inbox backlog excluded", async () => {
    server.use(
      // Overrides FIRST (they win over the idle spread below).
      http.get(ACTIVE_IMPORT_URL, () =>
        HttpResponse.json({
          active: true,
          job_id: "job-1",
          origin: "inbox",
          needs_review_count: 2,
        }),
      ),
      http.get(ACQUISITION_URL, () =>
        HttpResponse.json({
          phase: "idle",
          queued: 0,
          current: null,
          processed: 0,
          set_aside: 0,
          failed: 0,
          error: null,
          inbox_pending: 5,
        }),
      ),
      ...idleShellHandlers(),
    );
    renderShell();

    const review = screen.getByRole("link", { name: /review/i });
    expect(await within(review).findByText("2")).toBeInTheDocument();
    // The OLD header badge summed needs_review_count + inbox_pending (=7).
    expect(within(review).queryByText("7")).not.toBeInTheDocument();
    expect(within(review).queryByText("5")).not.toBeInTheDocument();
  });

  test("has a labelled search box inside a search landmark", async () => {
    server.use(...idleShellHandlers());
    renderShell();

    const box = screen.getByRole("searchbox", { name: /search/i });
    expect(box).toBeInTheDocument();
    // Wrapped in a search landmark.
    expect(screen.getByRole("search")).toContainElement(box);
  });

  test("typing in the search box debounces before navigating to /search?q=", async () => {
    server.use(
      http.get(SEARCH_URL, () =>
        HttpResponse.json({
          artists: [],
          albums: [],
          tracks: [],
          artist_total: 0,
          album_total: 0,
          track_total: 0,
        }),
      ),
      ...idleShellHandlers(),
    );
    renderShell();

    await userEvent.type(
      screen.getByRole("searchbox", { name: /search/i }),
      "radio",
    );

    const loc = screen.getByTestId("location");
    // Synchronously after the keystrokes, the debounce timer (250ms) has NOT
    // fired yet, so the URL has NOT moved. Without a debounce the navigation
    // would already be visible here — this proves the debounce exists.
    expect(loc).not.toHaveTextContent("/search");
    expect(loc).not.toHaveTextContent("q=radio");

    // After the debounce elapses, it navigates.
    await waitFor(() => {
      expect(loc).toHaveTextContent("/search");
      expect(loc).toHaveTextContent("q=radio");
    });
  });

  test("re-syncs the box from the URL on external navigation (deep link)", async () => {
    server.use(
      http.get(SEARCH_URL, () =>
        HttpResponse.json({
          artists: [],
          albums: [],
          tracks: [],
          artist_total: 0,
          album_total: 0,
          track_total: 0,
        }),
      ),
      ...idleShellHandlers(),
    );
    // Deep-link straight to /search?q=foo: the box should reflect "foo".
    renderShell("/search?q=foo");

    expect(screen.getByRole("searchbox", { name: /search/i })).toHaveValue(
      "foo",
    );
  });

  test("navigation updates document.title via the shell-mounted RouteAnnouncer", async () => {
    server.use(...idleShellHandlers());
    renderShell();

    await waitFor(() =>
      expect(document.title).toBe("Overview - MusicDrop"),
    );

    await userEvent.click(screen.getByRole("link", { name: "Artists" }));

    expect(await screen.findByText("artists route")).toBeInTheDocument();
    await waitFor(() =>
      expect(document.title).toBe("Artists - MusicDrop"),
    );
  });
});
