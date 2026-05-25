import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router";
import { describe, expect, test } from "vitest";

import { App, HealthStatus } from "@/App";
import { AlbumsPage } from "@/pages/albums/AlbumsPage";
import { renderWithProviders } from "@/test/render";
import { server } from "@/test/msw-server";

const HEALTH_URL = `${window.location.origin}/api/health`;
const ALBUMS_URL = `${window.location.origin}/api/albums`;

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

describe("App", () => {
  test("renders the MusicDrop header and the albums page", async () => {
    server.use(
      http.get(HEALTH_URL, () =>
        HttpResponse.json({ status: "ok", version: "0.1.0" }),
      ),
      http.get(ALBUMS_URL, () =>
        HttpResponse.json({ items: [], total: 0, limit: 50, offset: 0 }),
      ),
    );

    // App is the layout shell; the Albums grid renders into its <Outlet>, so
    // mount them together the way the real router nests them.
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={["/"]}>
          <Routes>
            <Route element={<App />}>
              <Route path="*" element={<AlbumsPage />} />
            </Route>
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(
      screen.getByRole("heading", { level: 1, name: "MusicDrop" }),
    ).toBeInTheDocument();
    expect(await screen.findByText(/no albums/i)).toBeInTheDocument();
  });
});
