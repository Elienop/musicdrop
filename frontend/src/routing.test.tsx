import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { createMemoryRouter, Navigate, RouterProvider } from "react-router";
import { describe, expect, test } from "vitest";

import { App } from "@/App";
import { HomePage } from "@/pages/HomePage";
import { NotFoundPage } from "@/pages/NotFoundPage";
import { AlbumDetailPage } from "@/pages/albums/AlbumDetailPage";
import { ArtistAlbumsPage } from "@/pages/artists/ArtistAlbumsPage";
import { ArtistsPage } from "@/pages/artists/ArtistsPage";
import { BrowsePage } from "@/pages/browse/BrowsePage";
import { DuplicatesPage } from "@/pages/duplicates/DuplicatesPage";
import { ImportCandidatePage } from "@/pages/import/ImportCandidatePage";
import { ImportDuplicatePage } from "@/pages/import/ImportDuplicatePage";
import { ImportPage } from "@/pages/import/ImportPage";
import { PlaylistDetailPage } from "@/pages/playlists/PlaylistDetailPage";
import { PlaylistsPage } from "@/pages/playlists/PlaylistsPage";
import { BankReviewPage } from "@/pages/review/BankReviewPage";
import { ReviewPage } from "@/pages/review/ReviewPage";
import { SearchPage } from "@/pages/search/SearchPage";
import { SettingsBeetsPage } from "@/pages/settings/SettingsBeetsPage";
import { SettingsIntegrationsPage } from "@/pages/settings/SettingsIntegrationsPage";
import { SettingsLayout } from "@/pages/settings/SettingsLayout";
import { SettingsMetadataPage } from "@/pages/settings/SettingsMetadataPage";
import { SettingsNamingPage } from "@/pages/settings/SettingsNamingPage";
import { server } from "@/test/msw-server";

const HEALTH_URL = `${window.location.origin}/api/health`;
const STATS_URL = `${window.location.origin}/api/stats`;
const ARTISTS_URL = `${window.location.origin}/api/artists`;
const ALBUMS_URL = `${window.location.origin}/api/albums`;
const CONFIG_URL = `${window.location.origin}/api/config`;
const ACTIVE_IMPORT_URL = `${window.location.origin}/api/imports/active`;
const ACQUISITION_URL = `${window.location.origin}/api/acquisition/status`;
const REORGANIZE_URL = `${window.location.origin}/api/reorganize/status`;
const LYRICS_URL = `${window.location.origin}/api/lyrics/backfill`;
const ARTIST_ART_URL = `${window.location.origin}/api/artists/art/backfill`;
const ARTIST_ART_SETTINGS_URL = `${window.location.origin}/api/artists/art/settings`;
const ARTIST_IMAGE_SETTINGS_URL = `${window.location.origin}/api/artists/image/settings`;

/** The route table mirrors main.tsx 1:1 — the same page components at the
 * same paths, inside the real App shell — so the IA contract is exercised
 * end-to-end: `/` is the Overview dashboard, `/artists` is the roster,
 * details hang off the artist spine, `/settings` index-redirects to
 * /settings/beets, unknown routes fall to NotFound. Keep this table in
 * lockstep with main.tsx whenever a route is added or moved. */
const routes = [
  {
    element: <App />,
    children: [
      { index: true, element: <HomePage /> },
      { path: "/artists", element: <ArtistsPage /> },
      { path: "/artists/:artistName", element: <ArtistAlbumsPage /> },
      { path: "/albums/:albumId", element: <AlbumDetailPage /> },
      { path: "/search", element: <SearchPage /> },
      { path: "/browse", element: <BrowsePage /> },
      { path: "/review", element: <ReviewPage /> },
      { path: "/review/bank/:itemId", element: <BankReviewPage /> },
      { path: "/import", element: <ImportPage /> },
      { path: "/import/albums/:index", element: <ImportCandidatePage /> },
      {
        path: "/import/albums/:index/duplicate",
        element: <ImportDuplicatePage />,
      },
      {
        path: "/settings",
        element: <SettingsLayout />,
        children: [
          { index: true, element: <Navigate to="/settings/beets" replace /> },
          { path: "beets", element: <SettingsBeetsPage /> },
          { path: "naming", element: <SettingsNamingPage /> },
          { path: "metadata", element: <SettingsMetadataPage /> },
          { path: "integrations", element: <SettingsIntegrationsPage /> },
        ],
      },
      { path: "/duplicates", element: <DuplicatesPage /> },
      { path: "/playlists", element: <PlaylistsPage /> },
      { path: "/playlists/:playlistId", element: <PlaylistDetailPage /> },
      { path: "*", element: <NotFoundPage /> },
    ],
  },
];

/** Idle/minimal handlers for every probe the shell + the visited pages fire
 * (App.test.tsx's idleShellHandlers idiom, replicated because that helper is
 * module-local): health + the five activity sources for the shell chrome,
 * stats for the Overview, roster/albums for the artist spine, the config
 * snapshot + artist art/image settings for the heavier pages. Unvisited
 * routes simply leave their handlers unused. */
function appHandlers() {
  return [
    http.get(HEALTH_URL, () =>
      HttpResponse.json({ status: "ok", version: "0.1.0" }),
    ),
    http.get(STATS_URL, () =>
      HttpResponse.json({
        stats: {
          track_count: 22,
          album_count: 2,
          artist_count: 1,
          total_seconds: 5400,
          total_bytes: 123456789,
        },
        recently_added: [],
        size_is_estimate: true,
      }),
    ),
    http.get(ARTISTS_URL, () =>
      HttpResponse.json([{ name: "Radiohead", album_count: 2 }]),
    ),
    http.get(ALBUMS_URL, () =>
      HttpResponse.json({ items: [], total: 0, limit: 48, offset: 0 }),
    ),
    http.get(CONFIG_URL, () =>
      HttpResponse.json({
        yaml_text: "directory: /music\n",
        config_path: "/config/beets/config.yaml",
        loaded_at: "2026-06-10T12:00:00Z",
        file_modified_at: null,
        sha256: "0123456789abcdef",
        apply_pending: false,
      }),
    ),
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
    http.get(ARTIST_ART_SETTINGS_URL, () =>
      HttpResponse.json({ enabled: false }),
    ),
    http.get(ARTIST_IMAGE_SETTINGS_URL, () =>
      HttpResponse.json({ enabled: false }),
    ),
  ];
}

function renderAt(path: string) {
  server.use(...appHandlers());
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
}

describe("routing (sidebar IA)", () => {
  test("/ renders the Overview dashboard as home — not the roster", async () => {
    renderAt("/");
    expect(
      await screen.findByRole("heading", { level: 1, name: "Overview" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "Artists" }),
    ).not.toBeInTheDocument();
  });

  test("/artists renders the Artists roster", async () => {
    renderAt("/artists");
    expect(
      await screen.findByRole("heading", { level: 1, name: "Artists" }),
    ).toBeInTheDocument();
    expect(await screen.findByText("Radiohead")).toBeInTheDocument();
  });

  test("/artists/:name renders that artist's albums page", async () => {
    renderAt("/artists/Radiohead");
    expect(
      await screen.findByRole("heading", { level: 1, name: "Radiohead" }),
    ).toBeInTheDocument();
  });

  test("/settings redirects to the beets section", async () => {
    renderAt("/settings");
    expect(
      await screen.findByRole("heading", { level: 1, name: "Settings" }),
    ).toBeInTheDocument();
    // The redirect landed on /settings/beets: its section link is current.
    expect(
      await screen.findByRole("link", { name: "Beets" }),
    ).toHaveAttribute("aria-current", "page");
  });

  test("/import renders the import entry page", async () => {
    renderAt("/import");
    expect(
      await screen.findByRole("heading", { level: 1, name: "Add from folder" }),
    ).toBeInTheDocument();
  });

  test("/import/albums/:index without a job shows the no-review notice", async () => {
    // No ?job= in the URL, so the review page shows its no-job notice (and
    // fires no candidate request) — enough to confirm the route resolves here.
    renderAt("/import/albums/1");
    expect(await screen.findByText(/nothing to review/i)).toBeInTheDocument();
  });

  test("an unknown route renders NotFound with the one Overview escape", async () => {
    renderAt("/does/not/exist");
    expect(await screen.findByText(/page not found/i)).toBeInTheDocument();
    // The one escape points home to the Overview (spec §1 "one home").
    const escape = screen.getByRole("link", { name: /back to overview/i });
    expect(escape).toHaveAttribute("href", "/");
    // No page rendered behind it: neither home's nor the roster's h1.
    expect(
      screen.queryByRole("heading", { name: "Overview" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "Artists" }),
    ).not.toBeInTheDocument();
  });
});
