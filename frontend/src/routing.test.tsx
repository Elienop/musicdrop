import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import {
  createMemoryRouter,
  Navigate,
  RouterProvider,
} from "react-router";
import { describe, expect, test } from "vitest";

import { App } from "@/App";
import { NotFoundPage } from "@/pages/NotFoundPage";
import { AlbumDetailPage } from "@/pages/albums/AlbumDetailPage";
import { ArtistAlbumsPage } from "@/pages/artists/ArtistAlbumsPage";
import { ArtistsPage } from "@/pages/artists/ArtistsPage";
import { ImportCandidatePage } from "@/pages/import/ImportCandidatePage";
import { ImportPage } from "@/pages/import/ImportPage";
import { server } from "@/test/msw-server";

const HEALTH_URL = `${window.location.origin}/api/health`;
const ARTISTS_URL = `${window.location.origin}/api/artists`;
const ALBUMS_URL = `${window.location.origin}/api/albums`;

/** The route table mirrors main.tsx's single artist spine. Kept in lockstep
 * with main.tsx so the IA contract is exercised end-to-end. */
const routes = [
  {
    element: <App />,
    children: [
      { index: true, element: <ArtistsPage /> },
      { path: "/artists", element: <Navigate to="/" replace /> },
      { path: "/artists/:artistName", element: <ArtistAlbumsPage /> },
      { path: "/albums/:albumId", element: <AlbumDetailPage /> },
      { path: "/import", element: <ImportPage /> },
      { path: "/import/albums/:index", element: <ImportCandidatePage /> },
      { path: "*", element: <NotFoundPage /> },
    ],
  },
];

function renderAt(path: string) {
  server.use(
    http.get(HEALTH_URL, () =>
      HttpResponse.json({ status: "ok", version: "0.1.0" }),
    ),
    http.get(ARTISTS_URL, () =>
      HttpResponse.json([{ name: "Radiohead", album_count: 2 }]),
    ),
    http.get(ALBUMS_URL, () =>
      HttpResponse.json({ items: [], total: 0, limit: 50, offset: 0 }),
    ),
  );
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

describe("routing (artist spine)", () => {
  test("/ renders the Artists roster as home", async () => {
    renderAt("/");
    expect(await screen.findByText("Radiohead")).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { level: 2, name: "Artists" }),
    ).toBeInTheDocument();
  });

  test("/artists/:name renders that artist's albums page", async () => {
    renderAt("/artists/Radiohead");
    expect(
      await screen.findByRole("heading", { level: 2, name: "Radiohead" }),
    ).toBeInTheDocument();
  });

  test("/artists redirects to the roster home", async () => {
    renderAt("/artists");
    // Lands on the roster, not a dead-end empty parent.
    expect(
      await screen.findByRole("heading", { level: 2, name: "Artists" }),
    ).toBeInTheDocument();
  });

  test("/import renders the import entry page", async () => {
    renderAt("/import");
    expect(
      await screen.findByRole("heading", { level: 2, name: "Import music" }),
    ).toBeInTheDocument();
  });

  test("/import/albums/:index renders the candidate-review page", async () => {
    // No ?job= in the URL, so the review page shows its no-job notice (and
    // fires no candidate request) — enough to confirm the route resolves here.
    renderAt("/import/albums/1");
    expect(
      await screen.findByText(/nothing to review/i),
    ).toBeInTheDocument();
  });

  test("an unknown route renders the NotFound page, not the roster", async () => {
    renderAt("/does/not/exist");
    expect(await screen.findByText(/page not found/i)).toBeInTheDocument();
    // The one escape points home to the Overview (spec §1 "one home").
    const escape = screen.getByRole("link", { name: /back to overview/i });
    expect(escape).toHaveAttribute("href", "/");
    // The roster heading must NOT be present.
    expect(
      screen.queryByRole("heading", { name: "Artists" }),
    ).not.toBeInTheDocument();
  });
});
