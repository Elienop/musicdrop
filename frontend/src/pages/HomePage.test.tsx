import { screen } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { describe, expect, test } from "vitest";

import { HomePage } from "@/pages/HomePage";
import { renderWithProviders } from "@/test/render";
import { server } from "@/test/msw-server";

const STATS_URL = `${window.location.origin}/api/stats`;

describe("HomePage (Overview)", () => {
  test("renders the library dashboard, not the artists roster", async () => {
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

    // Dashboard stat present (track count)...
    expect(await screen.findByText("5")).toBeInTheDocument();
    // ...and the artists roster is NOT on the Overview — it lives at /artists,
    // so there's no roster <h2>Artists</h2> heading here.
    expect(
      screen.queryByRole("heading", { name: "Artists" }),
    ).not.toBeInTheDocument();
  });
});
