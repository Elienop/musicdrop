import { QueryClientProvider } from "@tanstack/react-query";
import { lazy, StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { createBrowserRouter, Navigate, RouterProvider } from "react-router";

import { App } from "@/App";
import { createAppQueryClient } from "@/api/queryClient";
import { RequireAuth } from "@/components/system/RequireAuth";
import { RouteErrorBoundary } from "@/components/system/RouteErrorBoundary";
// Eager, unlike every page below: /login is where a signed-out visitor lands
// on their FIRST request, so a lazy chunk would put a second round trip in
// front of the one screen that has to work before anything else can.
import { LoginPage } from "@/pages/LoginPage";

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
const ImportPlaylistsPage = lazy(() =>
  import("@/pages/playlists/ImportPlaylistsPage").then((m) => ({
    default: m.ImportPlaylistsPage,
  })),
);
const ReviewPage = lazy(() =>
  import("@/pages/review/ReviewPage").then((m) => ({ default: m.ReviewPage })),
);
const SearchPage = lazy(() =>
  import("@/pages/search/SearchPage").then((m) => ({ default: m.SearchPage })),
);
const SettingsAccountPage = lazy(() =>
  import("@/pages/settings/SettingsAccountPage").then((m) => ({
    default: m.SettingsAccountPage,
  })),
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

// Retry/staleTime policy — including "never retry a 401" — lives in
// api/queryClient so it can be tested; the test client (see test/render.tsx)
// keeps its own `retry: false`.
const queryClient = createAppQueryClient();

// `App` is the persistent shell (sidebar + topbar + <main><Outlet>); feature
// pages render into it. Data router (`createBrowserRouter`) so future
// loaders/blockers have the API available.
//
// IA: the sidebar (shell/Sidebar NAV_SECTIONS) groups the sections —
// Library (/ Overview dashboard, /artists roster, /browse facets),
// Acquire (/review, /import), Manage (/playlists, /duplicates, /settings/* —
// beets · naming · metadata · integrations · trash · account; /settings
// redirects to /settings/beets).
// Detail routes hang off the artist spine:
//   /artists/:name  that artist's albums
//   /albums/:id     album tracklist (back link is contextual — Artists,
//                   Browse, or Search via router state)
// /search is reached by typing in the topbar search; it has no sidebar item.
// Unknown routes fall to a minimal NotFound, not a page.
//
// /login is a TOP-LEVEL sibling of the shell layout, not a child of it: the
// sign-in screen must not render inside the chrome it is the gate for, and
// leaving the layout unmounts the shell's SSE stream and query fan-out. A
// static path out-ranks the `*` splat inside the layout regardless of order,
// so "/login" can never fall through to NotFound.
const router = createBrowserRouter([
  {
    path: "/login",
    element: <LoginPage />,
    errorElement: <RouteErrorBoundary />,
  },
  {
    element: (
      <RequireAuth>
        <App />
      </RequireAuth>
    ),
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
          { path: "account", element: <SettingsAccountPage /> },
        ],
      },
      { path: "/duplicates", element: <DuplicatesPage /> },
      { path: "/playlists", element: <PlaylistsPage /> },
      // Static segment before the param route: React Router ranks static over
      // dynamic, but the order keeps "import" unambiguous at a glance.
      { path: "/playlists/import", element: <ImportPlaylistsPage /> },
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
