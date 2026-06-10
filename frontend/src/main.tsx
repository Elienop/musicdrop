import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { createBrowserRouter, RouterProvider } from "react-router";

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
import { ReviewPage } from "@/pages/review/ReviewPage";
import { SearchPage } from "@/pages/search/SearchPage";
import { SettingsPage } from "@/pages/settings/SettingsPage";

import "@/styles.css";

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
// Acquire (/review, /import), Manage (/playlists, /duplicates, /settings).
// Detail routes hang off the artist spine:
//   /artists/:name  that artist's albums
//   /albums/:id     album tracklist (back link is contextual — Artists,
//                   Browse, or Search via router state)
// /search is reached by typing in the topbar search; it has no sidebar item.
// Unknown routes fall to a minimal NotFound, not a page.
const router = createBrowserRouter([
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
      { path: "/import", element: <ImportPage /> },
      { path: "/import/albums/:index", element: <ImportCandidatePage /> },
      {
        path: "/import/albums/:index/duplicate",
        element: <ImportDuplicatePage />,
      },
      { path: "/settings", element: <SettingsPage /> },
      { path: "/duplicates", element: <DuplicatesPage /> },
      { path: "/playlists", element: <PlaylistsPage /> },
      { path: "/playlists/:playlistId", element: <PlaylistDetailPage /> },
      { path: "*", element: <NotFoundPage /> },
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
