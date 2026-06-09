import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";
import { describe, expect, test } from "vitest";

import { App, HealthStatus } from "@/App";
import { ArtistsPage } from "@/pages/artists/ArtistsPage";
import { renderWithProviders } from "@/test/render";
import { server } from "@/test/msw-server";

const HEALTH_URL = `${window.location.origin}/api/health`;
const ARTISTS_URL = `${window.location.origin}/api/artists`;

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
    server.use(http.get(HEALTH_URL, () => new HttpResponse(null, { status: 500 })));

    renderWithProviders(<HealthStatus />);

    expect(await screen.findByText(/offline/i)).toBeInTheDocument();
  });
});

/** Mount App shell with the roster in its <Outlet>, the way the router nests
 * them. */
function renderShell() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/"]}>
        <Routes>
          <Route element={<App />}>
            <Route path="*" element={<ArtistsPage />} />
          </Route>
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("App", () => {
  test("renders the MusicDrop header and the roster home", async () => {
    server.use(
      http.get(HEALTH_URL, () =>
        HttpResponse.json({ status: "ok", version: "0.1.0" }),
      ),
      http.get(ARTISTS_URL, () => HttpResponse.json([])),
    );

    renderShell();

    expect(
      screen.getByRole("heading", { level: 1, name: "MusicDrop" }),
    ).toBeInTheDocument();
    expect(await screen.findByText(/no artists/i)).toBeInTheDocument();
  });

  test("the brand links to the roster home", async () => {
    server.use(
      http.get(HEALTH_URL, () =>
        HttpResponse.json({ status: "ok", version: "0.1.0" }),
      ),
      http.get(ARTISTS_URL, () => HttpResponse.json([])),
    );

    renderShell();

    const brand = screen.getByRole("link", { name: "MusicDrop" });
    expect(brand).toHaveAttribute("href", "/");
  });

  test("has an Artists nav link → /artists, and no flat Albums tab", async () => {
    server.use(
      http.get(HEALTH_URL, () =>
        HttpResponse.json({ status: "ok", version: "0.1.0" }),
      ),
      http.get(ARTISTS_URL, () => HttpResponse.json([])),
    );

    renderShell();

    // Browse-by-artist (the roster) now has its own nav entry → /artists.
    expect(screen.getByRole("link", { name: "Artists" })).toHaveAttribute(
      "href",
      "/artists",
    );
    // Still no flat "Albums" tab — albums are reached via the spine / Browse.
    expect(
      screen.queryByRole("link", { name: "Albums" }),
    ).not.toBeInTheDocument();
  });

  test("has a labelled search box inside a search landmark", async () => {
    server.use(
      http.get(HEALTH_URL, () =>
        HttpResponse.json({ status: "ok", version: "0.1.0" }),
      ),
      http.get(ARTISTS_URL, () => HttpResponse.json([])),
    );

    renderShell();

    const box = screen.getByRole("searchbox", { name: /search/i });
    expect(box).toBeInTheDocument();
    // Wrapped in a search landmark.
    expect(screen.getByRole("search")).toContainElement(box);
  });

  test("typing in the search box debounces before navigating to /search?q=", async () => {
    server.use(
      http.get(HEALTH_URL, () =>
        HttpResponse.json({ status: "ok", version: "0.1.0" }),
      ),
      http.get(ARTISTS_URL, () => HttpResponse.json([])),
      http.get(`${window.location.origin}/api/search`, () =>
        HttpResponse.json({
          artists: [],
          albums: [],
          tracks: [],
          artist_total: 0,
          album_total: 0,
          track_total: 0,
        }),
      ),
    );

    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={["/"]}>
          <Routes>
            <Route element={<App />}>
              <Route index element={<ArtistsPage />} />
              <Route path="search" element={<div>search route</div>} />
              <Route path="*" element={<div>other</div>} />
            </Route>
          </Routes>
          <LocationProbe />
        </MemoryRouter>
      </QueryClientProvider>,
    );

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
      http.get(HEALTH_URL, () =>
        HttpResponse.json({ status: "ok", version: "0.1.0" }),
      ),
      http.get(`${window.location.origin}/api/search`, () =>
        HttpResponse.json({
          artists: [],
          albums: [],
          tracks: [],
          artist_total: 0,
          album_total: 0,
          track_total: 0,
        }),
      ),
    );

    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    // Deep-link straight to /search?q=foo: the box should reflect "foo".
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={["/search?q=foo"]}>
          <Routes>
            <Route element={<App />}>
              <Route path="search" element={<div>search route</div>} />
              <Route path="*" element={<div>other</div>} />
            </Route>
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(screen.getByRole("searchbox", { name: /search/i })).toHaveValue(
      "foo",
    );
  });
});

/** Exposes the live router location so tests can assert URL changes. */
function LocationProbe() {
  const loc = useLocation();
  return <div data-testid="location">{`${loc.pathname}${loc.search}`}</div>;
}
