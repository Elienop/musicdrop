import { screen } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { describe, expect, test } from "vitest";

import { HomePage } from "@/pages/HomePage";
import { renderWithProviders } from "@/test/render";
import { server } from "@/test/msw-server";

const STATS_URL = `${window.location.origin}/api/stats`;

describe("HomePage (Overview)", () => {
  test("renders the Overview h1 and dashboard, not the artists roster", async () => {
    server.use(
      http.get(STATS_URL, () =>
        HttpResponse.json({
          stats: {
            track_count: 5,
            album_count: 2,
            artist_count: 1,
            total_seconds: 600,
            total_bytes: 1_000_000,
          },
          recently_added: [],
          size_is_estimate: true,
        }),
      ),
    );
    renderWithProviders(<HomePage />);

    // THE route h1 — PageHeader renders it for RouteAnnouncer's focus contract.
    expect(
      await screen.findByRole("heading", { level: 1, name: "Overview" }),
    ).toBeInTheDocument();
    // Dashboard stat present (track count) — awaited: the h1 mounts
    // synchronously, ahead of the async stats.
    expect(await screen.findByText("5")).toBeInTheDocument();
    // ...and the artists roster is NOT on the Overview — it lives at /artists.
    expect(
      screen.queryByRole("heading", { name: "Artists" }),
    ).not.toBeInTheDocument();
  });
});
