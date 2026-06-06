import { screen } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { describe, expect, test } from "vitest";

import type { components } from "@/api/schema";
import { LibraryDashboard } from "@/components/dashboard/LibraryDashboard";
import { renderWithProviders } from "@/test/render";
import { server } from "@/test/msw-server";

type StatsResponse = components["schemas"]["LibraryStatsResponse"];

const STATS_URL = `${window.location.origin}/api/stats`;

const POPULATED: StatsResponse = {
  stats: {
    track_count: 1240,
    album_count: 98,
    artist_count: 41,
    total_seconds: 544_320,
    total_bytes: 74_200_000_000,
  },
  recently_added: [
    {
      id: 7,
      album_artist: "Adele",
      title: "25",
      year: 2015,
      track_count: 11,
      genre: "Pop",
      mb_albumid: null,
    },
  ],
  size_is_estimate: true,
};

const EMPTY: StatsResponse = {
  stats: {
    track_count: 0,
    album_count: 0,
    artist_count: 0,
    total_seconds: 0,
    total_bytes: 0,
  },
  recently_added: [],
  size_is_estimate: true,
};

describe("LibraryDashboard", () => {
  test("renders the headline stats", async () => {
    server.use(http.get(STATS_URL, () => HttpResponse.json(POPULATED)));
    renderWithProviders(<LibraryDashboard />);

    expect(await screen.findByText("1,240")).toBeInTheDocument(); // tracks
    expect(screen.getByText("98")).toBeInTheDocument(); // albums
    expect(screen.getByText("41")).toBeInTheDocument(); // artists
    expect(screen.getByText("6.3 days")).toBeInTheDocument(); // duration
    expect(screen.getByText("~74.2 GB")).toBeInTheDocument(); // size (estimate ~)
  });

  test("renders recently-added albums linking to the album page", async () => {
    server.use(http.get(STATS_URL, () => HttpResponse.json(POPULATED)));
    renderWithProviders(<LibraryDashboard />);

    const link = await screen.findByRole("link", { name: /25/i });
    expect(link).toHaveAttribute("href", "/albums/7");
  });

  test("empty library shows zeros and the import hint, no recently-added", async () => {
    server.use(http.get(STATS_URL, () => HttpResponse.json(EMPTY)));
    renderWithProviders(<LibraryDashboard />);

    expect(await screen.findAllByText("0")).not.toHaveLength(0);
    expect(screen.getByText(/import some music/i)).toBeInTheDocument();
    expect(screen.queryByText(/recently added/i)).toBeNull();
  });
});
