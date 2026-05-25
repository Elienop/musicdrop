import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { createBrowserRouter, RouterProvider } from "react-router";

import { App } from "@/App";

import "@/styles.css";

// Cap retries so an outage surfaces the error state promptly instead of
// hanging through TanStack's long default backoff; a short staleTime avoids
// refetching on every focus/mount for read-heavy library views. The test
// client (see test/render.tsx) keeps `retry: false`.
const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, staleTime: 30_000 } },
});

// Single route for now — the app shell. Feature routes (Albums, etc.) land in
// later slices. Data router (`createBrowserRouter`) so future loaders/blockers
// have the API available.
const router = createBrowserRouter([{ path: "*", element: <App /> }]);

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
