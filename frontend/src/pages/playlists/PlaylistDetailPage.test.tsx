import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, test } from "vitest";

import { PlaylistDetailPage } from "@/pages/playlists/PlaylistDetailPage";
import { renderWithProviders } from "@/test/render";
import { server } from "@/test/msw-server";

const ID = "a".repeat(32);
const BASE = `${window.location.origin}/api/playlists/${ID}`;

function detail(tracks: unknown[], name = "Late night") {
  return {
    id: ID,
    name,
    description: "",
    track_count: tracks.length,
    target_plex_users: [],
    created_at: "2026-06-06T00:00:00+00:00",
    updated_at: "2026-06-06T00:00:00+00:00",
    tracks,
  };
}

function track(id: number, title: string, available = true) {
  return { id, title, artist: "Artist", album: "Album", duration_seconds: 200, available };
}

describe("PlaylistDetailPage", () => {
  test("renders the tracklist", async () => {
    server.use(http.get(BASE, () => HttpResponse.json(detail([track(1, "Alpha"), track(2, "Beta")]))));
    renderWithProviders(<PlaylistDetailPage />, {
      route: `/playlists/${ID}`,
      path: "/playlists/:playlistId",
    });
    expect(await screen.findByText("Late night")).toBeInTheDocument();
    expect(screen.getByText("Alpha")).toBeInTheDocument();
    expect(screen.getByText("Beta")).toBeInTheDocument();
  });

  test("removes a track", async () => {
    let removed = false;
    server.use(
      http.get(BASE, () =>
        HttpResponse.json(detail(removed ? [track(2, "Beta")] : [track(1, "Alpha"), track(2, "Beta")])),
      ),
      http.delete(`${BASE}/tracks/1`, () => {
        removed = true;
        return HttpResponse.json(detail([track(2, "Beta")]));
      }),
    );
    renderWithProviders(<PlaylistDetailPage />, {
      route: `/playlists/${ID}`,
      path: "/playlists/:playlistId",
    });
    await screen.findByText("Alpha");
    await userEvent.click(screen.getByRole("button", { name: /remove alpha/i }));
    await waitFor(() => expect(screen.queryByText("Alpha")).not.toBeInTheDocument());
  });

  test("reorders a track down", async () => {
    let body: number[] | null = null;
    server.use(
      http.get(BASE, () => HttpResponse.json(detail([track(1, "Alpha"), track(2, "Beta")]))),
      http.put(`${BASE}/tracks`, async ({ request }) => {
        body = ((await request.json()) as { track_ids: number[] }).track_ids;
        return HttpResponse.json(detail([track(2, "Beta"), track(1, "Alpha")]));
      }),
    );
    renderWithProviders(<PlaylistDetailPage />, {
      route: `/playlists/${ID}`,
      path: "/playlists/:playlistId",
    });
    await screen.findByText("Alpha");
    await userEvent.click(screen.getByRole("button", { name: /move alpha down/i }));
    await waitFor(() => expect(body).toEqual([2, 1]));
  });

  test("announces a reorder via the status region", async () => {
    server.use(
      http.get(BASE, () => HttpResponse.json(detail([track(1, "Alpha"), track(2, "Beta")]))),
      http.put(`${BASE}/tracks`, () =>
        HttpResponse.json(detail([track(2, "Beta"), track(1, "Alpha")])),
      ),
    );
    renderWithProviders(<PlaylistDetailPage />, {
      route: `/playlists/${ID}`,
      path: "/playlists/:playlistId",
    });
    await screen.findByText("Alpha");
    await userEvent.click(screen.getByRole("button", { name: /move alpha down/i }));
    expect(await screen.findByText(/moved alpha to position 2/i)).toBeInTheDocument();
  });

  test("announces a removal via the status region", async () => {
    server.use(
      http.get(BASE, () => HttpResponse.json(detail([track(1, "Alpha"), track(2, "Beta")]))),
      http.delete(`${BASE}/tracks/1`, () => HttpResponse.json(detail([track(2, "Beta")]))),
    );
    renderWithProviders(<PlaylistDetailPage />, {
      route: `/playlists/${ID}`,
      path: "/playlists/:playlistId",
    });
    await screen.findByText("Alpha");
    await userEvent.click(screen.getByRole("button", { name: /remove alpha/i }));
    expect(await screen.findByText(/removed alpha/i)).toBeInTheDocument();
  });

  test("surfaces a reorder failure", async () => {
    server.use(
      http.get(BASE, () => HttpResponse.json(detail([track(1, "Alpha"), track(2, "Beta")]))),
      http.put(`${BASE}/tracks`, () => new HttpResponse(null, { status: 500 })),
    );
    renderWithProviders(<PlaylistDetailPage />, {
      route: `/playlists/${ID}`,
      path: "/playlists/:playlistId",
    });
    await screen.findByText("Alpha");
    await userEvent.click(screen.getByRole("button", { name: /move alpha down/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/couldn.t save the new order/i);
  });

  test("surfaces a remove failure", async () => {
    server.use(
      http.get(BASE, () => HttpResponse.json(detail([track(1, "Alpha"), track(2, "Beta")]))),
      http.delete(`${BASE}/tracks/1`, () => new HttpResponse(null, { status: 500 })),
    );
    renderWithProviders(<PlaylistDetailPage />, {
      route: `/playlists/${ID}`,
      path: "/playlists/:playlistId",
    });
    await screen.findByText("Alpha");
    await userEvent.click(screen.getByRole("button", { name: /remove alpha/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/couldn.t remove the track/i);
  });

  test("shows unavailable tracks as such", async () => {
    server.use(http.get(BASE, () => HttpResponse.json(detail([track(9, "Gone", false)]))));
    renderWithProviders(<PlaylistDetailPage />, {
      route: `/playlists/${ID}`,
      path: "/playlists/:playlistId",
    });
    expect(await screen.findByText(/unavailable/i)).toBeInTheDocument();
  });

  test("syncs to plex and shows status", async () => {
    let synced = false;
    server.use(
      http.get(BASE, () =>
        HttpResponse.json({
          ...detail([track(1, "Alpha")]),
          plex: synced
            ? {
                admin: {
                  rating_key: "777",
                  status: "ok",
                  missing: 0,
                  synced_at: "2026-06-07T01:00:00+00:00",
                  error: null,
                },
              }
            : {},
        }),
      ),
      http.post(`${BASE}/sync`, () => {
        synced = true;
        return HttpResponse.json({
          ...detail([track(1, "Alpha")]),
          plex: {
            admin: {
              rating_key: "777",
              status: "ok",
              missing: 0,
              synced_at: "2026-06-07T01:00:00+00:00",
              error: null,
            },
          },
        });
      }),
    );
    renderWithProviders(<PlaylistDetailPage />, {
      route: `/playlists/${ID}`,
      path: "/playlists/:playlistId",
    });
    await screen.findByText("Alpha");
    await userEvent.click(screen.getByRole("button", { name: /sync to plex/i }));
    await waitFor(() => expect(screen.getByText(/synced/i)).toBeInTheDocument());
  });
});
