import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
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
    ...over,
  };
}

describe("PlaylistsPage", () => {
  test("renders the playlist list", async () => {
    server.use(http.get(URL, () => HttpResponse.json([playlist()])));
    renderWithProviders(<PlaylistsPage />);
    expect(await screen.findByText("Late night")).toBeInTheDocument();
    expect(screen.getByText(/3 tracks/i)).toBeInTheDocument();
  });

  test("empty state when there are no playlists", async () => {
    server.use(http.get(URL, () => HttpResponse.json([])));
    renderWithProviders(<PlaylistsPage />);
    expect(await screen.findByText(/no playlists yet/i)).toBeInTheDocument();
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
});
