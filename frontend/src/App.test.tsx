import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router";
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

  test("has no Albums/Artists nav tabs (single spine)", async () => {
    server.use(
      http.get(HEALTH_URL, () =>
        HttpResponse.json({ status: "ok", version: "0.1.0" }),
      ),
      http.get(ARTISTS_URL, () => HttpResponse.json([])),
    );

    renderShell();

    expect(screen.queryByRole("link", { name: "Albums" })).not.toBeInTheDocument();
    // No "Artists" *nav tab* — only the brand and (later) page content.
    expect(
      screen.queryByRole("navigation", { name: /primary/i }),
    ).not.toBeInTheDocument();
  });
});
