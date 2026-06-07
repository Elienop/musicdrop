import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, test } from "vitest";

import { AddToPlaylistMenu } from "@/components/playlists/AddToPlaylistMenu";
import { renderWithProviders } from "@/test/render";
import { server } from "@/test/msw-server";

const LIST = `${window.location.origin}/api/playlists`;
const PID = "a".repeat(32);

function playlist() {
  return {
    id: PID,
    name: "Late night",
    description: "",
    track_count: 0,
    target_plex_users: [],
    created_at: "2026-06-06T00:00:00+00:00",
    updated_at: "2026-06-06T00:00:00+00:00",
  };
}

describe("AddToPlaylistMenu", () => {
  test("adds a track to an existing playlist", async () => {
    let added: number[] | null = null;
    server.use(
      http.get(LIST, () => HttpResponse.json([playlist()])),
      http.post(`${LIST}/${PID}/tracks`, async ({ request }) => {
        added = ((await request.json()) as { track_ids: number[] }).track_ids;
        return HttpResponse.json({ ...playlist(), tracks: [] });
      }),
    );
    renderWithProviders(<AddToPlaylistMenu trackIds={[42]} label="Add to playlist" />);

    await userEvent.click(screen.getByRole("button", { name: /add to playlist/i }));
    await userEvent.click(await screen.findByRole("menuitem", { name: /late night/i }));

    await waitFor(() => expect(added).toEqual([42]));
  });

  test("announces which playlist a track was added to", async () => {
    server.use(
      http.get(LIST, () => HttpResponse.json([playlist()])),
      http.post(`${LIST}/${PID}/tracks`, () =>
        HttpResponse.json({ ...playlist(), tracks: [] }),
      ),
    );
    renderWithProviders(<AddToPlaylistMenu trackIds={[42]} label="Add to playlist" />);

    await userEvent.click(screen.getByRole("button", { name: /add to playlist/i }));
    await userEvent.click(await screen.findByRole("menuitem", { name: /late night/i }));

    // The dropdown content unmounts on select, so the confirmation must live on
    // the always-mounted root.
    expect(await screen.findByText(/added to late night/i)).toBeInTheDocument();
  });
});
