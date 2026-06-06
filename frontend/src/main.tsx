import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { createBrowserRouter, Navigate, RouterProvider } from "react-router";

import { App } from "@/App";
import { HomePage } from "@/pages/HomePage";
import { NotFoundPage } from "@/pages/NotFoundPage";
import { AlbumDetailPage } from "@/pages/albums/AlbumDetailPage";
import { ArtistAlbumsPage } from "@/pages/artists/ArtistAlbumsPage";
import { DuplicatesPage } from "@/pages/duplicates/DuplicatesPage";
import { ImportCandidatePage } from "@/pages/import/ImportCandidatePage";
import { ImportDuplicatePage } from "@/pages/import/ImportDuplicatePage";
import { ImportPage } from "@/pages/import/ImportPage";
import { PlaylistsPage } from "@/pages/playlists/PlaylistsPage";
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

// `App` is the persistent shell (header + <Outlet>); feature pages render into
// it. Data router (`createBrowserRouter`) so future loaders/blockers have the
// API available.
//
// Single artist spine (the browse IA): the Artists roster is home, drilling
// into an artist's albums, then into an album's tracklist. Back always walks
// UP the hierarchy.
//   /                  Artists roster (home)
//    └ /artists/:name  that artist's albums
//       └ /albums/:id  album tracklist
// `/artists` (the bare parent) redirects to home so it isn't a dead end.
// Unknown routes fall to a minimal NotFound, not a page.
const router = createBrowserRouter([
  {
    element: <App />,
    children: [
      { index: true, element: <HomePage /> },
      { path: "/artists", element: <Navigate to="/" replace /> },
      { path: "/artists/:artistName", element: <ArtistAlbumsPage /> },
      { path: "/albums/:albumId", element: <AlbumDetailPage /> },
      { path: "/search", element: <SearchPage /> },
      { path: "/import", element: <ImportPage /> },
      { path: "/import/albums/:index", element: <ImportCandidatePage /> },
      {
        path: "/import/albums/:index/duplicate",
        element: <ImportDuplicatePage />,
      },
      { path: "/settings", element: <SettingsPage /> },
      { path: "/duplicates", element: <DuplicatesPage /> },
      { path: "/playlists", element: <PlaylistsPage /> },
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
