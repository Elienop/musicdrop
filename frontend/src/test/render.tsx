import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter, Route, Routes } from "react-router";

export interface RenderOptions {
  /**
   * Initial entry the MemoryRouter starts at. A string URL, or a location
   * object `{ pathname, state }` when a test needs to seed router `state`
   * (e.g. an album page's contextual back link). Defaults to "/".
   */
  route?: string | { pathname: string; state?: unknown };
  /**
   * Route pattern the `ui` is mounted under (e.g. "/albums/:albumId"). Needed
   * for pages that read `useParams`. Defaults to "*" so a plain component
   * renders at any path.
   */
  path?: string;
}

/**
 * Render a component inside a fresh, retry-disabled QueryClientProvider and a
 * MemoryRouter so `<Link>`/`useNavigate`/`useParams` resolve. Mount the `ui`
 * under `path` (default "*") and start at `route` (default "/").
 */
export function renderWithProviders(
  ui: ReactElement,
  { route = "/", path = "*" }: RenderOptions = {},
) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[route]}>
        <Routes>
          <Route path={path} element={ui} />
          {/* A "/" landing target for back-links — only when the page itself
              isn't already mounted at "/" (or the catch-all "*"). */}
          {path !== "/" && path !== "*" && (
            <Route path="/" element={<div>Library home</div>} />
          )}
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}
