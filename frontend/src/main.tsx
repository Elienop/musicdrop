import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { createBrowserRouter, RouterProvider } from "react-router";

import { App } from "@/App";
import { AlbumDetailPage } from "@/pages/albums/AlbumDetailPage";
import { AlbumsPage } from "@/pages/albums/AlbumsPage";

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
// API available. The catch-all index falls back to the Albums grid.
const router = createBrowserRouter([
  {
    element: <App />,
    children: [
      { path: "/albums/:albumId", element: <AlbumDetailPage /> },
      { path: "*", element: <AlbumsPage /> },
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
