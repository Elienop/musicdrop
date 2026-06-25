import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { lazy, StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { createBrowserRouter, Navigate, RouterProvider } from "react-router";

import { App } from "@/App";
import { RouteErrorBoundary } from "@/components/system/RouteErrorBoundary";

import "@/styles.css";

// Route-level code-splitting: every page is its own chunk, fetched on first
// visit (App wraps the Outlet in <Suspense fallback={<RouteLoading/>}>). The
// .then() shims re-shape our named exports into lazy()'s default-export
// contract. A failed chunk load rejects into the route errorElement.
const HomePage = lazy(() =>
  import("@/pages/HomePage").then((m) => ({ default: m.HomePage })),
);
const NotFoundPage = lazy(() =>
  import("@/pages/NotFoundPage").then((m) => ({ default: m.NotFoundPage })),
);
const AlbumDetailPage = lazy(() =>
  import("@/pages/albums/AlbumDetailPage").then((m) => ({
    default: m.AlbumDetailPage,
  })),
);
const ArtistAlbumsPage = lazy(() =>
  import("@/pages/artists/ArtistAlbumsPage").then((m) => ({
    default: m.ArtistAlbumsPage,
  })),
);
const ArtistsPage = lazy(() =>
  import("@/pages/artists/ArtistsPage").then((m) => ({
    default: m.ArtistsPage,
  })),
);
const BankReviewPage = lazy(() =>
  import("@/pages/review/BankReviewPage").then((m) => ({
    default: m.BankReviewPage,
  })),
);
const BrowsePage = lazy(() =>
  import("@/pages/browse/BrowsePage").then((m) => ({ default: m.BrowsePage })),
);
const DuplicatesPage = lazy(() =>
  import("@/pages/duplicates/DuplicatesPage").then((m) => ({
    default: m.DuplicatesPage,
  })),
);
const ImportCandidatePage = lazy(() =>
  import("@/pages/import/ImportCandidatePage").then((m) => ({
    default: m.ImportCandidatePage,
  })),
);
const ImportDuplicatePage = lazy(() =>
  import("@/pages/import/ImportDuplicatePage").then((m) => ({
    default: m.ImportDuplicatePage,
  })),
);
const ImportPage = lazy(() =>
  import("@/pages/import/ImportPage").then((m) => ({ default: m.ImportPage })),
);
const PlaylistDetailPage = lazy(() =>
  import("@/pages/playlists/PlaylistDetailPage").then((m) => ({
    default: m.PlaylistDetailPage,
  })),
);
const PlaylistsPage = lazy(() =>
  import("@/pages/playlists/PlaylistsPage").then((m) => ({
    default: m.PlaylistsPage,
  })),
);
const ReviewPage = lazy(() =>
  import("@/pages/review/ReviewPage").then((m) => ({ default: m.ReviewPage })),
);
const SearchPage = lazy(() =>
  import("@/pages/search/SearchPage").then((m) => ({ default: m.SearchPage })),
);
const SettingsBeetsPage = lazy(() =>
  import("@/pages/settings/SettingsBeetsPage").then((m) => ({
    default: m.SettingsBeetsPage,
  })),
);
const SettingsIntegrationsPage = lazy(() =>
  import("@/pages/settings/SettingsIntegrationsPage").then((m) => ({
    default: m.SettingsIntegrationsPage,
  })),
);
const SettingsLayout = lazy(() =>
  import("@/pages/settings/SettingsLayout").then((m) => ({
    default: m.SettingsLayout,
  })),
);
const SettingsMetadataPage = lazy(() =>
  import("@/pages/settings/SettingsMetadataPage").then((m) => ({
    default: m.SettingsMetadataPage,
  })),
);
const SettingsNamingPage = lazy(() =>
  import("@/pages/settings/SettingsNamingPage").then((m) => ({
    default: m.SettingsNamingPage,
  })),
);
const SettingsTrashPage = lazy(() =>
  import("@/pages/settings/SettingsTrashPage").then((m) => ({
    default: m.SettingsTrashPage,
  })),
);

// Cap retries so an outage surfaces the error state promptly instead of
// hanging through TanStack's long default backoff; a short staleTime avoids
// refetching on every focus/mount for read-heavy library views. The test
// client (see test/render.tsx) keeps `retry: false`.
const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, staleTime: 30_000 } },
});

// `App` is the persistent shell (sidebar + topbar + <main><Outlet>); feature
// pages render into it. Data router (`createBrowserRouter`) so future
// loaders/blockers have the API available.
//
// IA: the sidebar (shell/Sidebar NAV_SECTIONS) groups the sections —
// Library (/ Overview dashboard, /artists roster, /browse facets),
// Acquire (/review, /import), Manage (/playlists, /duplicates, /settings/* —
// beets · naming · metadata · integrations; /settings redirects to
// /settings/beets).
// Detail routes hang off the artist spine:
//   /artists/:name  that artist's albums
//   /albums/:id     album tracklist (back link is contextual — Artists,
//                   Browse, or Search via router state)
// /search is reached by typing in the topbar search; it has no sidebar item.
// Unknown routes fall to a minimal NotFound, not a page.
const router = createBrowserRouter([
  {
    element: <App />,
    // Shell-level fallback: only reached if the App shell ITSELF throws.
    errorElement: <RouteErrorBoundary />,
    children: [
      {
        // Pathless boundary: a crashing PAGE renders the styled fallback in
        // the layout's Outlet — sidebar/topbar stay alive and navigable.
        errorElement: <RouteErrorBoundary />,
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
          { path: "trash", element: <SettingsTrashPage /> },
        ],
      },
      { path: "/duplicates", element: <DuplicatesPage /> },
      { path: "/playlists", element: <PlaylistsPage /> },
      { path: "/playlists/:playlistId", element: <PlaylistDetailPage /> },
      { path: "*", element: <NotFoundPage /> },
        ],
      },
    ],
  },
]);

const rootEl = document.getElementById("root");
if (!rootEl) {
  throw new Error("Root element #root not found");
}

createRoot(rootEl).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  </StrictMode>,
);
