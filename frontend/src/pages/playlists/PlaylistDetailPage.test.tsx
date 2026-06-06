import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, test } from "vitest";

import { PlaylistDetailPage } from "@/pages/playlists/PlaylistDetailPage";
import { renderWithProviders } from "@/test/render";
import { server } from "@/test/msw-server";

const ID = "a".repeat(32);
const BASE = `${window.location.origin}/api/playlists/${ID}`;
const USERS = `${window.location.origin}/api/plex/users`;

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
  // The detail page now discovers Plex users for the target picker. Default to
  // "no users" so existing tests don't hit an unhandled request; tests that care
  // register their own /api/plex/users handler (which takes precedence).
  beforeEach(() => {
    server.use(http.get(USERS, () => HttpResponse.json({ users: [] })));
  });

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

  test("syncs to plex, shows the synced state, and announces the real outcome", async () => {
    let synced = false;
    // Realistic timestamps: the playlist was last edited (updated_at) BEFORE its
    // last push (synced_at), so the now-fixed backend reads as "Synced", not
    // "Out of date" (set_plex_state no longer bumps updated_at).
    const okAdmin = {
      admin: {
        rating_key: "777",
        status: "ok",
        missing: 0,
        synced_at: "2026-06-07T01:00:00+00:00",
        error: null,
      },
    };
    server.use(
      http.get(BASE, () =>
        HttpResponse.json({ ...detail([track(1, "Alpha")]), plex: synced ? okAdmin : {} }),
      ),
      http.post(`${BASE}/sync`, () => {
        synced = true;
        return HttpResponse.json({ ...detail([track(1, "Alpha")]), plex: okAdmin });
      }),
    );
    renderWithProviders(<PlaylistDetailPage />, {
      route: `/playlists/${ID}`,
      path: "/playlists/:playlistId",
    });
    await screen.findByText("Alpha");
    // Before syncing the status line reads "Not synced to Plex".
    expect(screen.getByText(/not synced to plex/i)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /sync to plex/i }));
    // The polite region announces the derived outcome — not a generic "complete"
    // and not the ambiguous "Not synced".
    expect(await screen.findByText(/plex sync — synced/i)).toBeInTheDocument();
    // And the visible per-playlist status line settles on exactly "Synced".
    await waitFor(() => expect(screen.getByText("Synced")).toBeInTheDocument());
  });

  test("points to Settings when sync fails because Plex isn't connected", async () => {
    server.use(
      http.get(BASE, () => HttpResponse.json(detail([track(1, "Alpha")]))),
      http.post(`${BASE}/sync`, () =>
        HttpResponse.json({ detail: "Connect Plex first" }, { status: 409 }),
      ),
    );
    renderWithProviders(<PlaylistDetailPage />, {
      route: `/playlists/${ID}`,
      path: "/playlists/:playlistId",
    });
    await screen.findByText("Alpha");
    await userEvent.click(screen.getByRole("button", { name: /sync to plex/i }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/connect plex in\s*settings\s*first/i);
    expect(screen.getByRole("link", { name: /settings/i })).toHaveAttribute("href", "/settings");
  });

  test("shows the target-user picker and saves a selection", async () => {
    let targets: string[] = [];
    server.use(
      http.get(USERS, () =>
        HttpResponse.json({ users: [{ id: "7", name: "Partner", home: true }] }),
      ),
      http.get(BASE, () =>
        HttpResponse.json({ ...detail([track(1, "Alpha")]), target_plex_users: targets }),
      ),
      http.patch(BASE, async ({ request }) => {
        targets = ((await request.json()) as { target_plex_users: string[] }).target_plex_users;
        return HttpResponse.json({ ...detail([track(1, "Alpha")]), target_plex_users: targets });
      }),
    );
    renderWithProviders(<PlaylistDetailPage />, {
      route: `/playlists/${ID}`,
      path: "/playlists/:playlistId",
    });
    const cb = await screen.findByRole("checkbox", { name: /partner/i });
    await userEvent.click(cb);
    await waitFor(() => expect(targets).toEqual(["7"]));
  });

  test("per-target sync status names admin and a user", async () => {
    server.use(
      http.get(USERS, () =>
        HttpResponse.json({ users: [{ id: "7", name: "Partner", home: true }] }),
      ),
      http.get(BASE, () =>
        HttpResponse.json({
          ...detail([track(1, "Alpha")]),
          target_plex_users: ["7"],
          plex: {
            admin: {
              rating_key: "1",
              status: "ok",
              missing: 0,
              synced_at: "2026-06-07T01:00:00+00:00",
              error: null,
            },
            "7": {
              rating_key: "2",
              status: "partial",
              missing: 1,
              synced_at: "2026-06-07T01:00:00+00:00",
              error: null,
            },
          },
        }),
      ),
    );
    renderWithProviders(<PlaylistDetailPage />, {
      route: `/playlists/${ID}`,
      path: "/playlists/:playlistId",
    });
    expect(await screen.findByText(/you \(admin\)/i)).toBeInTheDocument();
    expect(await screen.findByText(/partner/i)).toBeInTheDocument();
  });
});
