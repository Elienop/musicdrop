import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";
import { beforeEach, describe, expect, test, vi } from "vitest";

import type { components } from "@/api/schema";
import { LibraryDashboard } from "@/components/dashboard/LibraryDashboard";
import { renderWithProviders } from "@/test/render";
import { server } from "@/test/msw-server";

import type { ActivityRow } from "@/api/useActivity";

let activityRows: ActivityRow[] = [];
let needsReview = 0;

// House idiom (ActivityPopover.test.tsx): drive the glance through the
// composition hook + the import probe directly, so the five underlying
// polling endpoints are never fetched (MSW handlers stay stats-only).
vi.mock("@/api/useActivity", () => ({
  useActivity: () => ({
    rows: activityRows,
    runningCount: activityRows.filter((row) => row.state === "running")
      .length,
  }),
  useActivityDismissals: () => ({
    dismissed: new Set<string>(),
    dismiss: vi.fn(),
  }),
}));

vi.mock("@/api/useActiveImport", () => ({
  useActiveImport: () => ({
    data: { active: false, origin: "manual", needs_review_count: needsReview },
  }),
}));

const runningAcquisition: ActivityRow = {
  id: "acquisition:queue",
  kind: "acquisition",
  label: "Importing from slskd",
  scope: "Adele - 25",
  state: "running",
  countsText: "2 queued",
  href: "/review",
};

const runningLyrics: ActivityRow = {
  id: "lyrics:L1",
  kind: "lyrics",
  label: "Lyrics backfill",
  scope: "library",
  state: "running",
  progress: { done: 3, total: 10 },
  href: "/settings/metadata",
};

const failedReorganize: ActivityRow = {
  id: "reorganize:r1",
  kind: "reorganize",
  label: "Reorganize",
  scope: "library",
  state: "failed",
  countsText: "library locked",
  href: "/settings/beets",
};

const doneArtistArt: ActivityRow = {
  id: "artist-art:a1",
  kind: "artist-art",
  label: "Writing artist art",
  state: "done",
  countsText: "3 written",
  href: "/settings/metadata",
};

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

/** Probe mounted at /albums/:albumId: prints the router-state origin an
 * AlbumCard sent along, so origin threading is assertable end-to-end. */
function ProbeAlbumPage() {
  const state = useLocation().state as
    | { from?: { label: string; to: string } }
    | null;
  return (
    <p>{state?.from ? `${state.from.label} → ${state.from.to}` : "no origin"}</p>
  );
}

function renderWithAlbumProbe() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/"]}>
        <Routes>
          <Route path="/" element={<LibraryDashboard />} />
          <Route path="/albums/:albumId" element={<ProbeAlbumPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("LibraryDashboard", () => {
  beforeEach(() => {
    activityRows = [];
    needsReview = 0;
  });

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

  test("stats failure shows an error with a retry that refetches", async () => {
    let calls = 0;
    server.use(
      http.get(STATS_URL, () => {
        calls += 1;
        if (calls === 1) {
          return new HttpResponse(null, { status: 500 });
        }
        return HttpResponse.json(POPULATED);
      }),
    );
    renderWithProviders(<LibraryDashboard />);

    expect(
      await screen.findByText(/could not load library stats/i),
    ).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /retry/i }));
    expect(await screen.findByText("1,240")).toBeInTheDocument();
  });

  test("recently-added cards carry the Overview origin in router state", async () => {
    server.use(http.get(STATS_URL, () => HttpResponse.json(POPULATED)));
    renderWithAlbumProbe();

    await userEvent.click(await screen.findByRole("link", { name: /25/i }));
    expect(await screen.findByText("Overview → /")).toBeInTheDocument();
  });

  test("an empty library offers the import CTA", async () => {
    server.use(http.get(STATS_URL, () => HttpResponse.json(EMPTY)));
    renderWithProviders(<LibraryDashboard />);

    const cta = await screen.findByRole("link", {
      name: /add music from a folder/i,
    });
    expect(cta).toHaveAttribute("href", "/import");
  });

  test("the route h1 is Overview with the counts meta line", async () => {
    server.use(http.get(STATS_URL, () => HttpResponse.json(POPULATED)));
    renderWithProviders(<LibraryDashboard />);

    expect(
      await screen.findByRole("heading", { level: 1, name: "Overview" }),
    ).toBeInTheDocument();
    // findByText: the h1 mounts synchronously (always-rendered PageHeader),
    // so the async meta line must be awaited separately.
    expect(
      await screen.findByText("98 albums · 1,240 tracks"),
    ).toBeInTheDocument();
  });

  test("no Acquisition glance or review banner when everything is idle", async () => {
    server.use(http.get(STATS_URL, () => HttpResponse.json(POPULATED)));
    renderWithProviders(<LibraryDashboard />);

    await screen.findByText("1,240");
    // Idle = zero dead chrome: no glance section, no review banner.
    expect(screen.queryByRole("heading", { name: "Acquisition" })).toBeNull();
    expect(screen.queryByText(/awaiting review/i)).toBeNull();
  });

  test("running jobs surface in the Acquisition glance with a View Review link", async () => {
    activityRows = [runningAcquisition, runningLyrics];
    server.use(http.get(STATS_URL, () => HttpResponse.json(POPULATED)));
    renderWithProviders(<LibraryDashboard />);

    expect(
      await screen.findByRole("heading", { level: 2, name: "Acquisition" }),
    ).toBeInTheDocument();
    // JobProgress rows render verbatim (label + progress fraction).
    expect(screen.getByText("Importing from slskd")).toBeInTheDocument();
    expect(screen.getByText("3 / 10")).toBeInTheDocument();
    // An acquisition-kind row is present → the Review deep link shows.
    expect(screen.getByRole("link", { name: "View Review" })).toHaveAttribute(
      "href",
      "/review",
    );
  });

  test("a failed job shows in the glance without the View Review link", async () => {
    activityRows = [failedReorganize];
    server.use(http.get(STATS_URL, () => HttpResponse.json(POPULATED)));
    renderWithProviders(<LibraryDashboard />);

    expect(
      await screen.findByRole("heading", { level: 2, name: "Acquisition" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Reorganize")).toBeInTheDocument();
    expect(screen.getByText("Failed")).toBeInTheDocument(); // state chip text
    // No import/acquisition rows → no Review deep link.
    expect(screen.queryByRole("link", { name: "View Review" })).toBeNull();
  });

  test("the glance lists at most three rows", async () => {
    activityRows = [
      runningAcquisition,
      runningLyrics,
      failedReorganize,
      doneArtistArt,
    ];
    server.use(http.get(STATS_URL, () => HttpResponse.json(POPULATED)));
    renderWithProviders(<LibraryDashboard />);

    await screen.findByRole("heading", { level: 2, name: "Acquisition" });
    expect(screen.getByText("Importing from slskd")).toBeInTheDocument();
    expect(screen.getByText("Reorganize")).toBeInTheDocument();
    // Row 4 is cut — a glance, not the popover.
    expect(screen.queryByText("Writing artist art")).toBeNull();
  });

  test("pending review decisions render a neutral banner linking to /review", async () => {
    needsReview = 2;
    server.use(http.get(STATS_URL, () => HttpResponse.json(POPULATED)));
    renderWithProviders(<LibraryDashboard />);

    const line = await screen.findByText(/2 decisions awaiting review/i);
    // Neutral tone = ambient role="status", never role="alert".
    expect(line.closest('[role="status"]')).not.toBeNull();
    // getByRole name-matching is exact, so "Review" ≠ the glance's
    // "View Review" (absent here anyway: no rows).
    expect(screen.getByRole("link", { name: "Review" })).toHaveAttribute(
      "href",
      "/review",
    );
  });
});
