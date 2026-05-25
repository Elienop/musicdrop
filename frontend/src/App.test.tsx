import { screen } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { describe, expect, test } from "vitest";

import { HealthStatus } from "@/App";
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

    // Imported lazily to keep the HealthStatus block self-contained.
    const { App } = await import("@/App");
    renderWithProviders(<App />);

    expect(
      screen.getByRole("heading", { level: 1, name: "MusicDrop" }),
    ).toBeInTheDocument();
    expect(await screen.findByText(/no albums/i)).toBeInTheDocument();
  });
});
