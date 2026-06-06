import { screen } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { describe, expect, test } from "vitest";

import { HomePage } from "@/pages/HomePage";
import { renderWithProviders } from "@/test/render";
import { server } from "@/test/msw-server";

const STATS_URL = `${window.location.origin}/api/stats`;
const ARTISTS_URL = `${window.location.origin}/api/artists`;

describe("HomePage", () => {
  test("renders the dashboard above the artists roster", async () => {
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
      http.get(ARTISTS_URL, () =>
        HttpResponse.json([{ name: "Radiohead", album_count: 9 }]),
      ),
    );
    renderWithProviders(<HomePage />);

    // dashboard stat (5 tracks) and roster (artist name) both present
    expect(await screen.findByText("5")).toBeInTheDocument();
    expect(await screen.findByText("Radiohead")).toBeInTheDocument();
  });
});
