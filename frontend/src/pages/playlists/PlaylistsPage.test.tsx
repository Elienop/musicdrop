import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router";
import { describe, expect, test } from "vitest";

import { PlaylistsPage } from "@/pages/playlists/PlaylistsPage";
import { renderWithProviders } from "@/test/render";
import { server } from "@/test/msw-server";

const URL = `${window.location.origin}/api/playlists`;

function playlist(over: Partial<Record<string, unknown>> = {}) {
  return {
    id: "a".repeat(32),
    name: "Late night",
    description: "",
    track_count: 3,
    target_plex_users: [],
    created_at: "2026-06-06T00:00:00+00:00",
    updated_at: "2026-06-06T00:00:00+00:00",
    artwork_hash: null,
    cover_album_ids: [],
    ...over,
  };
}

describe("PlaylistsPage", () => {
  test("renders the page h1 with the live count in the header meta", async () => {
    server.use(http.get(URL, () => HttpResponse.json([playlist()])));
    renderWithProviders(<PlaylistsPage />);
    const h1 = screen.getByRole("heading", { level: 1, name: "Playlists" });
    expect(h1).toHaveAttribute("tabindex", "-1");
    expect(await screen.findByText("1 playlist")).toBeInTheDocument();
  });

  test("renders the playlist list", async () => {
    server.use(http.get(URL, () => HttpResponse.json([playlist()])));
    renderWithProviders(<PlaylistsPage />);
    expect(await screen.findByText("Late night")).toBeInTheDocument();
    expect(screen.getByText(/3 tracks/i)).toBeInTheDocument();
  });

  test("a playlist with album covers shows a collage on its row", async () => {
    server.use(
      http.get(URL, () => HttpResponse.json([playlist({ cover_album_ids: [11, 22] })])),
    );
    renderWithProviders(<PlaylistsPage />);
    await screen.findByText("Late night");
    const collage = document.querySelector('[data-slot="playlist-cover-collage"]');
    expect(collage).not.toBeNull();
    expect(collage?.querySelectorAll("img")).toHaveLength(2);
  });

  test("empty state when there are no playlists", async () => {
    server.use(http.get(URL, () => HttpResponse.json([])));
    renderWithProviders(<PlaylistsPage />);
    expect(await screen.findByText(/no playlists yet/i)).toBeInTheDocument();
    expect(screen.getByText("0 playlists")).toBeInTheDocument();
  });

  test("failed load shows the error state with a working Retry", async () => {
    let calls = 0;
    server.use(
      http.get(URL, () => {
        calls += 1;
        return calls === 1
          ? new HttpResponse(null, { status: 500 })
          : HttpResponse.json([playlist()]);
      }),
    );
    renderWithProviders(<PlaylistsPage />);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/couldn.t load playlists/i);
    await userEvent.click(screen.getByRole("button", { name: /retry/i }));
    expect(await screen.findByText("Late night")).toBeInTheDocument();
  });

  test("creates a playlist via the dialog", async () => {
    const created = playlist({ id: "b".repeat(32), name: "Workout", track_count: 0 });
    let posted = false;
    server.use(
      http.get(URL, () => HttpResponse.json(posted ? [created] : [])),
      http.post(URL, async () => {
        posted = true;
        return HttpResponse.json(created);
      }),
    );
    renderWithProviders(<PlaylistsPage />);
    await screen.findByText(/no playlists yet/i);

    await userEvent.click(screen.getByRole("button", { name: /new playlist/i }));
    await userEvent.type(screen.getByLabelText(/name/i), "Workout");
    await userEvent.click(screen.getByRole("button", { name: /^create$/i }));

    await waitFor(() => expect(screen.getByText("Workout")).toBeInTheDocument());
  });

  test("the Import button navigates to the import flow", async () => {
    server.use(http.get(URL, () => HttpResponse.json([])));
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={["/playlists"]}>
          <Routes>
            <Route path="/playlists" element={<PlaylistsPage />} />
            <Route path="/playlists/import" element={<div>Import flow</div>} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    await screen.findByText(/no playlists yet/i);

    await userEvent.click(screen.getByRole("link", { name: /import/i }));

    expect(await screen.findByText("Import flow")).toBeInTheDocument();
  });
});
