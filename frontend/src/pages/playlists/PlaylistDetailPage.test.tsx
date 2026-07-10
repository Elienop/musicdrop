import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { delay, http, HttpResponse } from "msw";
import { beforeEach, describe, expect, test, vi } from "vitest";

import { PlaylistDetailPage } from "@/pages/playlists/PlaylistDetailPage";
import { renderWithProviders } from "@/test/render";
import { server } from "@/test/msw-server";

// Cross-page/async outcomes toast (spec §2). Mocked module-wide: the page
// imports `toast` from sonner; no Toaster is mounted in unit tests.
vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}));

import { toast } from "sonner";

const toastSuccess = vi.mocked(toast.success);

// Per-test override for the remove mutation. Null (the default) passes through
// to the real msw-backed hook, so every other test is unaffected. The
// rapid-remove race test swaps in a fake whose mutate() records each call's
// onSuccess — letting both run against the same render, the concurrency a
// single shared TanStack observer would otherwise collapse to the last call
// (hiding the resurrection this fix targets).
const removeOverride = vi.hoisted(() => ({ current: null as null | (() => unknown) }));

// Per-test override for the reorder mutation. Null (the default) passes through
// to the real msw-backed hook. The same-snapshot test swaps in a fake whose
// mutate() records each PUT body WITHOUT settling — no refetch reseeds the
// optimistic tracklist, so the rendered order stays exactly what the optimistic
// update produced when we compare it against the last recorded body.
const reorderOverride = vi.hoisted(() => ({ current: null as null | (() => unknown) }));

vi.mock("@/api/usePlaylists", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/api/usePlaylists")>();
  return {
    ...actual,
    useRemoveEntry: ((id: string) =>
      removeOverride.current
        ? removeOverride.current()
        : actual.useRemoveEntry(id)) as typeof actual.useRemoveEntry,
    useReorderTracks: ((id: string) =>
      reorderOverride.current
        ? reorderOverride.current()
        : actual.useReorderTracks(id)) as typeof actual.useReorderTracks,
  };
});

const ID = "a".repeat(32);
const BASE = `${window.location.origin}/api/playlists/${ID}`;
const USERS = `${window.location.origin}/api/plex/users`;
const SEARCH = `${window.location.origin}/api/search`;

function detail(tracks: Array<Record<string, unknown>>, name = "Late night") {
  const pending = tracks.filter((t) => t.pending).length;
  return {
    id: ID,
    name,
    description: "",
    track_count: tracks.length - pending,
    pending_count: pending,
    target_plex_users: [],
    plex: {},
    created_at: "2026-06-06T00:00:00+00:00",
    updated_at: "2026-06-06T00:00:00+00:00",
    artwork_hash: null,
    cover_album_ids: [],
    tracks,
  };
}

/** A resolved row: carries a library `id` and a stable `uid` (here `u<id>`). */
function track(id: number, title: string, available = true) {
  return {
    uid: `u${id}`,
    id,
    title,
    artist: "Artist",
    album: "Album",
    duration_seconds: 200,
    available,
    pending: false,
  };
}

/** A pending (unmatched) entry: no library id, remembered metadata only. */
function pendingTrack(uid: string, title: string, extra: Record<string, unknown> = {}) {
  return {
    uid,
    id: null,
    title,
    artist: "Ghost",
    album: "",
    duration_seconds: null,
    available: false,
    pending: true,
    ...extra,
  };
}

/** A `TypedSearchPage` (`type=tracks`) carrying the given track hits. */
function trackSearchPage(
  hits: Array<{
    id: number;
    title: string;
    artist: string;
    album: string;
    album_id: number | null;
    duration_seconds: number | null;
  }>,
) {
  return {
    type: "tracks",
    artists: [],
    albums: [],
    tracks: hits,
    total: hits.length,
    limit: 20,
    offset: 0,
  };
}

describe("PlaylistDetailPage", () => {
  // The detail page now discovers Plex users for the target picker. Default to
  // "no users" so existing tests don't hit an unhandled request; tests that care
  // register their own /api/plex/users handler (which takes precedence).
  beforeEach(() => {
    toastSuccess.mockClear();
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

  test("the header shows the playlist cover collage", async () => {
    server.use(
      http.get(BASE, () =>
        HttpResponse.json({
          ...detail([track(1, "Alpha")]),
          cover_album_ids: [11, 22, 33, 44],
        }),
      ),
    );
    renderWithProviders(<PlaylistDetailPage />, {
      route: `/playlists/${ID}`,
      path: "/playlists/:playlistId",
    });
    await screen.findByText("Late night");
    const collage = document.querySelector('[data-slot="playlist-cover-collage"]');
    expect(collage).not.toBeNull();
    expect(collage?.querySelectorAll("img")).toHaveLength(4);
  });

  test("removes a track", async () => {
    let removed = false;
    server.use(
      http.get(BASE, () =>
        HttpResponse.json(detail(removed ? [track(2, "Beta")] : [track(1, "Alpha"), track(2, "Beta")])),
      ),
      http.delete(`${BASE}/entries/u1`, () => {
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

  test("reorder posts entry uids", async () => {
    let body: string[] | null = null;
    server.use(
      http.get(BASE, () => HttpResponse.json(detail([track(1, "Alpha"), track(2, "Beta")]))),
      http.put(`${BASE}/tracks`, async ({ request }) => {
        body = ((await request.json()) as { entry_uids: string[] }).entry_uids;
        return HttpResponse.json(detail([track(2, "Beta"), track(1, "Alpha")]));
      }),
    );
    renderWithProviders(<PlaylistDetailPage />, {
      route: `/playlists/${ID}`,
      path: "/playlists/:playlistId",
    });
    await screen.findByText("Alpha");
    await userEvent.click(screen.getByRole("button", { name: /move alpha down/i }));
    // The new order is Beta (u2) then Alpha (u1) — sent as the full uid list.
    await waitFor(() => expect(body).toEqual(["u2", "u1"]));
  });

  test("the reorder PUT body derives from the same snapshot as the optimistic order", async () => {
    server.use(
      http.get(BASE, () =>
        HttpResponse.json(detail([track(1, "Alpha"), track(2, "Beta"), track(3, "Gamma")])),
      ),
    );
    const bodies: string[][] = [];
    reorderOverride.current = () => ({
      mutate: (uids: string[]) => bodies.push(uids),
      isError: false,
    });
    try {
      renderWithProviders(<PlaylistDetailPage />, {
        route: `/playlists/${ID}`,
        path: "/playlists/:playlistId",
      });
      await screen.findByText("Alpha");
      // Two "move Alpha down" clicks in ONE commit (native .click, no act flush
      // between them, so both hit the same render's handler). The buggy build
      // applied both swaps to the optimistic state (functional updater) but
      // computed each PUT body from the stale render snapshot — the last body
      // then lagged the visible order by a move.
      const down = screen.getByRole("button", { name: /move alpha down/i });
      act(() => {
        down.click();
        down.click();
      });
      // Read the optimistic order off the DOM and map it back to uids.
      const uidByTitle: Record<string, string> = { Alpha: "u1", Beta: "u2", Gamma: "u3" };
      const rows = screen.getAllByRole("row").slice(1); // drop the header row
      const domOrder = rows.map(
        (row) =>
          uidByTitle[
            (["Alpha", "Beta", "Gamma"].find((t) => within(row).queryByText(t)) ?? "") as string
          ],
      );
      // The order the server would persist must equal the order on screen.
      expect(bodies.at(-1)).toEqual(domOrder);
    } finally {
      reorderOverride.current = null;
    }
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
      http.delete(`${BASE}/entries/u1`, () => HttpResponse.json(detail([track(2, "Beta")]))),
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
      http.delete(`${BASE}/entries/u1`, () => new HttpResponse(null, { status: 500 })),
    );
    renderWithProviders(<PlaylistDetailPage />, {
      route: `/playlists/${ID}`,
      path: "/playlists/:playlistId",
    });
    await screen.findByText("Alpha");
    await userEvent.click(screen.getByRole("button", { name: /remove alpha/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/couldn.t remove the track/i);
  });

  test("two rapid removes drop both rows without resurrecting the first-removed", async () => {
    server.use(
      http.get(BASE, () =>
        HttpResponse.json(detail([track(1, "Alpha"), track(2, "Beta")])),
      ),
    );
    // Capture each remove's onSuccess so BOTH run against the same render — the
    // window where a closure-captured array re-adds the first-removed row.
    const pending: Array<() => void> = [];
    removeOverride.current = () => ({
      mutate: (_uid: string, opts?: { onSuccess?: () => void }) => {
        if (opts?.onSuccess) pending.push(opts.onSuccess);
      },
      isError: false,
    });
    try {
      renderWithProviders(<PlaylistDetailPage />, {
        route: `/playlists/${ID}`,
        path: "/playlists/:playlistId",
      });
      await screen.findByText("Alpha");
      // Fire both removes off the SAME render (onSuccess deferred to `pending`).
      fireEvent.click(screen.getByRole("button", { name: /remove alpha/i }));
      fireEvent.click(screen.getByRole("button", { name: /remove beta/i }));
      expect(pending).toHaveLength(2);
      // Run both onSuccess: each must drop only its OWN uid from the latest
      // list, so both rows are gone — the buggy closure array resurrects one.
      act(() => pending.forEach((cb) => cb()));
      expect(screen.queryByText("Alpha")).not.toBeInTheDocument();
      expect(screen.queryByText("Beta")).not.toBeInTheDocument();
    } finally {
      removeOverride.current = null;
    }
  });

  test("two rapid removes emptying the list focus the empty state, not a vanished survivor", async () => {
    server.use(
      http.get(BASE, () => HttpResponse.json(detail([track(1, "Alpha"), track(2, "Beta")]))),
    );
    // Defer both removes' onSuccess so they run against the SAME render — the
    // window where each afterRemoval snapshot (render list minus only its own
    // uid) still shows one survivor, so neither would see length 0.
    const pending: Array<() => void> = [];
    removeOverride.current = () => ({
      mutate: (_uid: string, opts?: { onSuccess?: () => void }) => {
        if (opts?.onSuccess) pending.push(opts.onSuccess);
      },
      isError: false,
    });
    try {
      renderWithProviders(<PlaylistDetailPage />, {
        route: `/playlists/${ID}`,
        path: "/playlists/:playlistId",
      });
      await screen.findByText("Alpha");
      fireEvent.click(screen.getByRole("button", { name: /remove alpha/i }));
      fireEvent.click(screen.getByRole("button", { name: /remove beta/i }));
      expect(pending).toHaveLength(2);
      // Run both onSuccess: the post-removal state empties, so the LAST focus
      // request must land on the empty state. Deciding survivor/empty off the
      // stale render list leaves focus stranded (the requested survivor row is
      // now unmounted), so the empty copy never takes focus.
      act(() => pending.forEach((cb) => cb()));
      await waitFor(() => {
        const active = document.activeElement as HTMLElement | null;
        expect(active?.textContent ?? "").toContain("No tracks yet");
      });
    } finally {
      removeOverride.current = null;
    }
  });

  test("shows unavailable tracks as such", async () => {
    server.use(http.get(BASE, () => HttpResponse.json(detail([track(9, "Gone", false)]))));
    renderWithProviders(<PlaylistDetailPage />, {
      route: `/playlists/${ID}`,
      path: "/playlists/:playlistId",
    });
    expect(await screen.findByText(/unavailable/i)).toBeInTheDocument();
  });

  // ——— Pending (unmatched import) rows ———

  test("renders a pending row with its badge and source metadata", async () => {
    server.use(
      http.get(BASE, () =>
        HttpResponse.json(
          detail([pendingTrack("u1", "Lost", { artist: "X", source: "Ghost - Lost" })]),
        ),
      ),
    );
    renderWithProviders(<PlaylistDetailPage />, {
      route: `/playlists/${ID}`,
      path: "/playlists/:playlistId",
    });
    // The remembered metadata renders…
    expect(await screen.findByText("Lost")).toBeInTheDocument();
    expect(screen.getByText("X")).toBeInTheDocument();
    // …with the original source text carried as a title tooltip (for a bare-path
    // m3u entry it's the only "it was this" identity).
    expect(screen.getByTitle("Ghost - Lost")).toBeInTheDocument();
    // …flagged with a "Pending" badge…
    expect(screen.getByText(/pending/i)).toBeInTheDocument();
    // …offering a Match action instead of a playable/linked title (there is no
    // library track behind it, so nothing links out).
    expect(screen.getByRole("button", { name: /match lost/i })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /lost/i })).not.toBeInTheDocument();
    // The header counts it as unmatched.
    expect(screen.getByText(/1 unmatched/i)).toBeInTheDocument();
  });

  test("resolves a pending row via the match picker", async () => {
    let patchBody: unknown = null;
    let resolved = false;
    server.use(
      http.get(BASE, () =>
        HttpResponse.json(
          resolved
            ? detail([track(55, "Found")])
            : detail([pendingTrack("u1", "Lost", { artist: "X" })]),
        ),
      ),
      http.get(SEARCH, () =>
        HttpResponse.json(
          trackSearchPage([
            {
              id: 55,
              title: "Found",
              artist: "Real",
              album: "Disc",
              album_id: 3,
              duration_seconds: 180,
            },
          ]),
        ),
      ),
      http.patch(`${BASE}/entries/u1`, async ({ request }) => {
        patchBody = await request.json();
        resolved = true;
        return HttpResponse.json(detail([track(55, "Found")]));
      }),
    );
    renderWithProviders(<PlaylistDetailPage />, {
      route: `/playlists/${ID}`,
      path: "/playlists/:playlistId",
    });
    await screen.findByText("Lost");
    await userEvent.click(screen.getByRole("button", { name: /match lost/i }));

    // The picker opens; searching surfaces a library track.
    const dialog = await screen.findByRole("dialog");
    await userEvent.type(within(dialog).getByRole("textbox"), "Found");
    const pick = await within(dialog).findByRole("button", { name: /select found/i });
    await userEvent.click(pick);

    // The entry is PATCHed to the picked library item id…
    await waitFor(() => expect(patchBody).toEqual({ item_id: 55 }));
    // …and the detail cache swaps to the resolved track.
    expect(await screen.findByText("Found")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText("Lost")).not.toBeInTheDocument());
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
    expect(screen.getByRole("link", { name: /settings/i })).toHaveAttribute(
      "href",
      "/settings/integrations",
    );
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

  test("toggling a target is optimistic and keeps the checkbox enabled", async () => {
    server.use(
      http.get(USERS, () =>
        HttpResponse.json({ users: [{ id: "7", name: "Partner", home: true }] }),
      ),
      http.get(BASE, () =>
        HttpResponse.json({ ...detail([track(1, "Alpha")]), target_plex_users: [] }),
      ),
      // The PATCH hangs so we observe the OPTIMISTIC state without a settle/refetch
      // reseeding the local target set.
      http.patch(BASE, async () => {
        await delay("infinite");
        return HttpResponse.json(detail([track(1, "Alpha")]));
      }),
    );
    renderWithProviders(<PlaylistDetailPage />, {
      route: `/playlists/${ID}`,
      path: "/playlists/:playlistId",
    });
    const cb = await screen.findByRole("checkbox", { name: /partner/i });
    expect(cb).not.toBeChecked();
    await userEvent.click(cb);
    // Reflects intent immediately and stays interactive (no shared-pending disable
    // that would strand focus).
    expect(cb).toBeChecked();
    expect(cb).toBeEnabled();
    // A freshly-checked target with no sync state yet reads "Not synced yet".
    expect(await screen.findByText(/not synced yet/i)).toBeInTheDocument();
  });

  test("collapses rapid target toggles into a single PATCH carrying the final set", async () => {
    let patchCount = 0;
    let lastBody: string[] | null = null;
    let targets: string[] = [];
    server.use(
      http.get(USERS, () =>
        HttpResponse.json({
          users: [
            { id: "7", name: "Partner", home: true },
            { id: "8", name: "Kid", home: true },
          ],
        }),
      ),
      http.get(BASE, () =>
        HttpResponse.json({ ...detail([track(1, "Alpha")]), target_plex_users: targets }),
      ),
      http.patch(BASE, async ({ request }) => {
        patchCount += 1;
        lastBody = ((await request.json()) as { target_plex_users: string[] }).target_plex_users;
        targets = lastBody;
        return HttpResponse.json({ ...detail([track(1, "Alpha")]), target_plex_users: targets });
      }),
    );
    renderWithProviders(<PlaylistDetailPage />, {
      route: `/playlists/${ID}`,
      path: "/playlists/:playlistId",
    });
    // Load under real timers, then take manual control of the debounce clock.
    // (fireEvent — not userEvent — for the toggles: it uses no timers of its own,
    // so the debounce is the only fake timer in play and we advance it explicitly.)
    const partner = await screen.findByRole("checkbox", { name: /partner/i });
    const kid = screen.getByRole("checkbox", { name: /kid/i });

    vi.useFakeTimers();
    try {
      // Tick two targets on in quick succession, INSIDE the debounce window.
      act(() => void fireEvent.click(partner));
      act(() => void fireEvent.click(kid));
      // Nothing sent yet — the burst is still coalescing behind the debounce.
      expect(patchCount).toBe(0);
      // Cross the debounce ONCE: exactly one PATCH, carrying BOTH ids.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(400);
      });
      expect(patchCount).toBe(1);
      expect(new Set(lastBody)).toEqual(new Set(["7", "8"]));
      // Both checkboxes stay checked after the save settles.
      expect(partner).toBeChecked();
      expect(kid).toBeChecked();
    } finally {
      vi.useRealTimers();
    }
  });

  test("a settle landing a subset while a newer toggle is pending doesn't drop the newer target", async () => {
    const patches: string[][] = [];
    let targets: string[] = [];
    server.use(
      http.get(USERS, () =>
        HttpResponse.json({
          users: [
            { id: "7", name: "Partner", home: true },
            { id: "8", name: "Kid", home: true },
          ],
        }),
      ),
      http.get(BASE, () =>
        HttpResponse.json({ ...detail([track(1, "Alpha")]), target_plex_users: targets }),
      ),
      http.patch(BASE, async ({ request }) => {
        const body = ((await request.json()) as { target_plex_users: string[] })
          .target_plex_users;
        patches.push(body);
        targets = body;
        // Hold the response so the second toggle can land while this PATCH is in
        // flight — the out-of-order-race window that used to drop a target.
        await delay(1000);
        return HttpResponse.json({ ...detail([track(1, "Alpha")]), target_plex_users: targets });
      }),
    );
    renderWithProviders(<PlaylistDetailPage />, {
      route: `/playlists/${ID}`,
      path: "/playlists/:playlistId",
    });
    const partner = await screen.findByRole("checkbox", { name: /partner/i });
    const kid = screen.getByRole("checkbox", { name: /kid/i });

    vi.useFakeTimers();
    try {
      // 1) Tick Partner and let the debounced PATCH #1 ([7]) go in flight.
      act(() => void fireEvent.click(partner));
      await act(async () => {
        await vi.advanceTimersByTimeAsync(400);
      });
      expect(patches).toEqual([["7"]]);
      // 2) Tick Kid WHILE PATCH #1 is still in flight (single-flight defers it).
      act(() => void fireEvent.click(kid));
      await act(async () => {
        await vi.advanceTimersByTimeAsync(400);
      });
      // Still only the first PATCH — the second is held behind single-flight.
      expect(patches).toEqual([["7"]]);
      // 3) Let PATCH #1 settle. Its refetch reports the SUBSET [7]; the reseed
      //    must NOT revert Kid (a newer, not-yet-persisted change).
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1000);
      });
      expect(kid).toBeChecked();
      // 4) Convergence fires PATCH #2 with the full desired set; let it settle.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1000);
      });
      expect(patches).toEqual([["7"], ["7", "8"]]);
      expect(partner).toBeChecked();
      expect(kid).toBeChecked();
    } finally {
      vi.useRealTimers();
    }
  });

  test("a checked target with no sync state reads 'Not synced yet'", async () => {
    server.use(
      http.get(USERS, () =>
        HttpResponse.json({ users: [{ id: "7", name: "Partner", home: true }] }),
      ),
      http.get(BASE, () =>
        HttpResponse.json({
          ...detail([track(1, "Alpha")]),
          target_plex_users: ["7"],
          plex: {}, // no per-target bookkeeping recorded yet
        }),
      ),
    );
    renderWithProviders(<PlaylistDetailPage />, {
      route: `/playlists/${ID}`,
      path: "/playlists/:playlistId",
    });
    expect(await screen.findByRole("checkbox", { name: /partner/i })).toBeChecked();
    expect(await screen.findByText(/not synced yet/i)).toBeInTheDocument();
  });

  test("points the picker at Settings when Plex isn't configured (409)", async () => {
    server.use(
      http.get(USERS, () => new HttpResponse(null, { status: 409 })),
      http.get(BASE, () => HttpResponse.json(detail([track(1, "Alpha")]))),
    );
    renderWithProviders(<PlaylistDetailPage />, {
      route: `/playlists/${ID}`,
      path: "/playlists/:playlistId",
    });
    await screen.findByText("Alpha");
    expect(await screen.findByText(/choose who gets this playlist/i)).toBeInTheDocument();
    // The generic "couldn't load" copy is NOT shown for a 409.
    expect(screen.queryByText(/couldn.t load plex accounts/i)).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: /settings/i })).toHaveAttribute(
      "href",
      "/settings/integrations",
    );
  });

  test("offers Retry in the picker when Plex accounts fail to load (non-409)", async () => {
    let calls = 0;
    server.use(
      http.get(USERS, () => {
        calls += 1;
        return calls === 1
          ? new HttpResponse(null, { status: 500 })
          : HttpResponse.json({ users: [{ id: "7", name: "Partner", home: true }] });
      }),
      http.get(BASE, () => HttpResponse.json(detail([track(1, "Alpha")]))),
    );
    renderWithProviders(<PlaylistDetailPage />, {
      route: `/playlists/${ID}`,
      path: "/playlists/:playlistId",
    });
    expect(await screen.findByText(/couldn.t load plex accounts/i)).toBeInTheDocument();
    // Not the 409 "configure Plex" copy.
    expect(screen.queryByText(/choose who gets this playlist/i)).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /retry/i }));
    expect(await screen.findByRole("checkbox", { name: /partner/i })).toBeInTheDocument();
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

  test("delete dialog notes Plex removal when the playlist has Plex copies", async () => {
    server.use(
      http.get(BASE, () =>
        HttpResponse.json({
          ...detail([track(1, "Alpha")]),
          plex: {
            admin: {
              rating_key: "1",
              status: "ok",
              missing: 0,
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
    await screen.findByText("Alpha");
    await userEvent.click(screen.getByRole("button", { name: /delete playlist/i }));
    expect(await screen.findByText(/also removes it from Plex/i)).toBeInTheDocument();
  });

  test("delete dialog omits the Plex note when there are no Plex copies", async () => {
    server.use(
      http.get(BASE, () => HttpResponse.json({ ...detail([track(1, "Alpha")]), plex: {} })),
    );
    renderWithProviders(<PlaylistDetailPage />, {
      route: `/playlists/${ID}`,
      path: "/playlists/:playlistId",
    });
    await screen.findByText("Alpha");
    await userEvent.click(screen.getByRole("button", { name: /delete playlist/i }));
    expect(screen.queryByText(/also removes it from Plex/i)).not.toBeInTheDocument();
  });

  // ——— Focus restoration (characterization: pins the pendingFocus engine the
  // useFocusAfterMutation swap must preserve exactly) ———

  test("removing a middle track focuses the surviving row's Remove button", async () => {
    let removed = false;
    const before = [track(1, "Alpha"), track(2, "Beta"), track(3, "Gamma")];
    const after = [track(1, "Alpha"), track(3, "Gamma")];
    server.use(
      http.get(BASE, () => HttpResponse.json(detail(removed ? after : before))),
      http.delete(`${BASE}/entries/u2`, () => {
        removed = true;
        return HttpResponse.json(detail(after));
      }),
    );
    renderWithProviders(<PlaylistDetailPage />, {
      route: `/playlists/${ID}`,
      path: "/playlists/:playlistId",
    });
    await screen.findByText("Beta");
    await userEvent.click(screen.getByRole("button", { name: /remove beta/i }));
    // Gamma slides up into Beta's slot — its Remove button takes focus.
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /remove gamma/i })).toHaveFocus(),
    );
  });

  test("removing the last remaining track moves focus to the empty state", async () => {
    let removed = false;
    server.use(
      http.get(BASE, () =>
        HttpResponse.json(detail(removed ? [] : [track(1, "Alpha")])),
      ),
      http.delete(`${BASE}/entries/u1`, () => {
        removed = true;
        return HttpResponse.json(detail([]));
      }),
    );
    renderWithProviders(<PlaylistDetailPage />, {
      route: `/playlists/${ID}`,
      path: "/playlists/:playlistId",
    });
    await screen.findByText("Alpha");
    await userEvent.click(screen.getByRole("button", { name: /remove alpha/i }));
    // Deliberately element-shape-agnostic (today a <p>, post-migration an
    // EmptyState wrapper): whatever holds focus must carry the empty copy.
    await waitFor(() => {
      const active = document.activeElement as HTMLElement | null;
      expect(active?.textContent ?? "").toContain("No tracks yet");
    });
  });

  test("moving a track to the end keeps focus on its enabled reorder button", async () => {
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
    // Alpha is now last: its "down" button is disabled, so focus falls to the
    // sibling "up" button — never stranded on <body>.
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /move alpha up/i })).toHaveFocus(),
    );
  });

  // ——— Phase 3 redesign contract ———

  test("renders the playlist name as the page h1 (focusable for RouteAnnouncer)", async () => {
    server.use(http.get(BASE, () => HttpResponse.json(detail([track(1, "Alpha")]))));
    renderWithProviders(<PlaylistDetailPage />, {
      route: `/playlists/${ID}`,
      path: "/playlists/:playlistId",
    });
    const h1 = await screen.findByRole("heading", { level: 1, name: "Late night" });
    expect(h1).toHaveAttribute("tabindex", "-1");
  });

  test("a successful reorder also fires a visible toast", async () => {
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
    await waitFor(() =>
      expect(toastSuccess).toHaveBeenCalledWith("Moved Alpha to position 2"),
    );
  });

  test("a successful removal also fires a visible toast", async () => {
    let removed = false;
    server.use(
      http.get(BASE, () =>
        HttpResponse.json(detail(removed ? [track(2, "Beta")] : [track(1, "Alpha"), track(2, "Beta")])),
      ),
      http.delete(`${BASE}/entries/u1`, () => {
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
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith("Removed Alpha"));
  });
});
