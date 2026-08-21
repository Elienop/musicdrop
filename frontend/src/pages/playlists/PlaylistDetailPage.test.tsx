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

// A render probe for the memoized rows: PlaylistTrackRow calls formatDuration
// exactly once per render (its only call site in the page), so counting the
// spy's invocations tells us precisely which rows re-rendered. Passthrough (the
// real formatter still runs) so no other test's duration text changes.
vi.mock("@/lib/format", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/format")>();
  return { ...actual, formatDuration: vi.fn(actual.formatDuration) };
});

import { formatDuration } from "@/lib/format";

const formatDurationSpy = vi.mocked(formatDuration);

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

/** A tiny image File for the artwork picker (PNG magic bytes; content is
 * irrelevant — the server sniffs the type, the client just ships the bytes). */
function makeImageFile(name = "art.png", type = "image/png"): File {
  return new File([new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10])], name, { type });
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
    formatDurationSpy.mockClear();
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

  // ——— Row memoization (perf: no whole-list reconcile on header state churn) ———

  test("typing in the rename input does not re-render any track row", async () => {
    // A few rows is enough to prove the mechanism (the real playlists hold
    // thousands, which is what made the per-keystroke reconcile expensive).
    server.use(
      http.get(BASE, () =>
        HttpResponse.json(detail([track(1, "Alpha"), track(2, "Beta"), track(3, "Gamma")])),
      ),
    );
    renderWithProviders(<PlaylistDetailPage />, {
      route: `/playlists/${ID}`,
      path: "/playlists/:playlistId",
    });
    await screen.findByText("Alpha");
    // Enter rename mode — a header-only state change (no tracklist mutation).
    await userEvent.click(screen.getByRole("button", { name: /edit name/i }));
    const input = screen.getByRole("textbox", { name: /playlist name/i });
    // Forget every render up to here; from now on we only churn header state.
    formatDurationSpy.mockClear();
    // Each keystroke bumps draftName → the page re-renders. With memoized rows
    // whose callbacks are stable, NONE of the three rows' props change, so not
    // one row re-renders (5 keystrokes × 3 rows = 15 avoided reconciles). The
    // pre-fix build recreated per-row closures every render, re-rendering all.
    await userEvent.type(input, "abcde");
    expect(formatDurationSpy).not.toHaveBeenCalled();
  });

  test("a reorder re-renders only the rows whose position changed, not untouched rows", async () => {
    // Distinct durations so the render probe can attribute each re-render to a
    // specific row by its formatDuration argument.
    const rows = [
      { ...track(1, "Alpha"), duration_seconds: 101 },
      { ...track(2, "Beta"), duration_seconds: 102 },
      { ...track(3, "Gamma"), duration_seconds: 103 },
    ];
    server.use(http.get(BASE, () => HttpResponse.json(detail(rows))));
    // Record the PUT without settling, so no refetch reseed muddies the count —
    // we observe exactly the optimistic re-render.
    reorderOverride.current = () => ({ mutate: () => {}, isError: false });
    try {
      renderWithProviders(<PlaylistDetailPage />, {
        route: `/playlists/${ID}`,
        path: "/playlists/:playlistId",
      });
      await screen.findByText("Alpha");
      formatDurationSpy.mockClear();
      // Swap Alpha (pos 1) and Beta (pos 2). Gamma (pos 3) is untouched.
      fireEvent.click(screen.getByRole("button", { name: /move alpha down/i }));
      await waitFor(() => {
        const order = screen.getAllByRole("row").slice(1).map((r) => r.textContent);
        expect(order[0]).toContain("Beta");
      });
      const durations = formatDurationSpy.mock.calls.map((c) => c[0]);
      // The two swapped rows re-render (their position/isFirst/isLast changed)…
      expect(durations).toContain(101); // Alpha
      expect(durations).toContain(102); // Beta
      // …while the untouched row stays memoized (never re-formatted).
      expect(durations).not.toContain(103); // Gamma
    } finally {
      reorderOverride.current = null;
    }
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
    expect(await screen.findByText(/plex sync: synced/i)).toBeInTheDocument();
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
              // The wire always carries this (Pydantic default), so the fixture
              // does too — the status label reads its length.
              missing_tracks: [
                { item_id: 1, title: "Alpha", albumartist: "A", album: "B", reason: "not_found" },
              ],
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

  test("marks the rows Plex could not find, with the reason", async () => {
    server.use(
      http.get(BASE, () =>
        HttpResponse.json({
          ...detail([track(11, "Found"), track(12, "Lost"), track(13, "Twins")]),
          plex: {
            admin: {
              rating_key: "500",
              status: "partial",
              missing: 2,
              missing_tracks: [
                {
                  item_id: 12,
                  title: "Lost",
                  albumartist: "A",
                  album: "B",
                  reason: "not_found",
                },
                {
                  item_id: 13,
                  title: "Twins",
                  albumartist: "A",
                  album: "B",
                  reason: "ambiguous",
                },
              ],
              synced_at: "2026-08-15T10:00:00+00:00",
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
    const badges = await screen.findAllByText("Not in Plex");
    expect(badges).toHaveLength(2);
    // Each flagged row carries ITS OWN reason — the two reasons are different
    // remedies (nothing matched vs. several matched), so a shared generic
    // wording would be a lie on one of them. Sighted hover reads the `title`…
    const lost = within(screen.getByRole("row", { name: /Lost/ }));
    const twins = within(screen.getByRole("row", { name: /Twins/ }));
    expect(lost.getByText("Not in Plex")).toHaveAttribute(
      "title",
      "Plex has no track with this file, and nothing matched by artist and title. Check the file is in your Plex library, then sync again.",
    );
    expect(twins.getByText("Not in Plex")).toHaveAttribute(
      "title",
      "Several Plex tracks share this artist and title, and none has this file, so MusicDrop won't guess which one. Sort out the copies in Plex, then sync again.",
    );
    // …and assistive tech gets the same reason as real text, because `title` on
    // a Badge's generic <span> is not reliably announced.
    expect(
      lost.getByText(
        "Not in Plex: Plex has no track with this file, and nothing matched by artist and title. Check the file is in your Plex library, then sync again.",
      ),
    ).toBeInTheDocument();
    expect(
      twins.getByText(
        "Not in Plex: Several Plex tracks share this artist and title, and none has this file, so MusicDrop won't guess which one. Sort out the copies in Plex, then sync again.",
      ),
    ).toBeInTheDocument();
    // …and the row Plex DID place stays unmarked.
    expect(
      within(screen.getByRole("row", { name: /Found/ })).queryByText("Not in Plex"),
    ).toBeNull();
  });

  test("the title cell wraps, so a badge can't widen the row past a phone", async () => {
    server.use(
      http.get(BASE, () =>
        HttpResponse.json({
          ...detail([track(12, "Lost")]),
          plex: {
            admin: {
              rating_key: "500",
              status: "partial",
              missing: 1,
              missing_tracks: [
                { item_id: 12, title: "Lost", albumartist: "A", album: "B", reason: "not_found" },
              ],
              synced_at: "2026-08-15T10:00:00+00:00",
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
    // A TRIPWIRE, not a measurement: the real invariant is "at 375px no row
    // action sits outside the table container's visible box", and jsdom has no
    // layout engine to check it. It was verified in a browser (with badges the
    // container measures scrollWidth === clientWidth === 327, identical to the
    // no-badge control; without the wrap it was 357 vs 327 and every Remove
    // button sat outside). The badges are shrink-0, so this class is what keeps
    // the Title column's min-content small — pinned so a refactor can't drop it
    // silently.
    const badge = await screen.findByText("Not in Plex");
    expect(badge.parentElement).toHaveClass("flex-wrap");
  });

  test("marks both rows when one library item sits in the playlist twice", async () => {
    server.use(
      http.get(BASE, () =>
        HttpResponse.json({
          ...detail([
            track(11, "Found"),
            { ...track(12, "Lost"), uid: "u12a" },
            { ...track(12, "Lost"), uid: "u12b" },
          ]),
          plex: {
            admin: {
              rating_key: "500",
              status: "partial",
              missing: 1,
              missing_tracks: [
                { item_id: 12, title: "Lost", albumartist: "A", album: "B", reason: "not_found" },
              ],
              synced_at: "2026-08-15T10:00:00+00:00",
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
    // The resolve is per LIBRARY ITEM, so both rows for item 12 are missing —
    // which is why the miss map is keyed by item id and not by row uid (one
    // entry, two badges).
    expect(await screen.findAllByText("Not in Plex")).toHaveLength(2);
  });

  test("does not stack the Plex badge on a row whose library item is gone", async () => {
    server.use(
      http.get(BASE, () =>
        HttpResponse.json({
          ...detail([track(12, "Lost", false)]),
          plex: {
            admin: {
              rating_key: "500",
              status: "partial",
              missing: 1,
              missing_tracks: [
                { item_id: 12, title: "Lost", albumartist: "A", album: "B", reason: "not_found" },
              ],
              synced_at: "2026-08-15T10:00:00+00:00",
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
    // Once the beets item is gone the row already says so; a Plex miss on top
    // is noise about the smaller of the two problems.
    expect(await screen.findByText(/unavailable/i)).toBeInTheDocument();
    expect(screen.queryByText("Not in Plex")).toBeNull();
  });

  /** A `partial` admin state carrying `marked` miss identities out of `missing`
   * total — the shape the cap (MISSING_TRACKS_CAP = 200) produces on a very
   * lossy sync. */
  function partialAdmin(missing: number, marked: number) {
    return {
      admin: {
        rating_key: "500",
        status: "partial",
        missing,
        missing_tracks: Array.from({ length: marked }, (_, i) => ({
          item_id: 1000 + i,
          title: `T${i}`,
          albumartist: "A",
          album: "B",
          reason: "not_found",
        })),
        synced_at: "2026-08-15T10:00:00+00:00",
        error: null,
      },
    };
  }

  test("says how many misses are marked when the identity list is capped", async () => {
    server.use(
      http.get(BASE, () =>
        HttpResponse.json({ ...detail([track(1, "Alpha")]), plex: partialAdmin(350, 200) }),
      ),
    );
    renderWithProviders(<PlaylistDetailPage />, {
      route: `/playlists/${ID}`,
      path: "/playlists/:playlistId",
    });
    // Only 200 rows can wear a badge, so the count alone would let the other
    // 150 unbadged rows read as fine.
    expect(await screen.findByText("350 not in Plex; first 200 marked")).toBeInTheDocument();
  });

  test("keeps the plain count when every miss is marked", async () => {
    server.use(
      http.get(BASE, () =>
        HttpResponse.json({ ...detail([track(1, "Alpha")]), plex: partialAdmin(2, 2) }),
      ),
    );
    renderWithProviders(<PlaylistDetailPage />, {
      route: `/playlists/${ID}`,
      path: "/playlists/:playlistId",
    });
    // The common case: every miss has a badge, so there is nothing to qualify.
    expect(await screen.findByText("2 not in Plex")).toBeInTheDocument();
  });

  test("says none are marked when the state carries no miss identities", async () => {
    server.use(
      http.get(BASE, () =>
        HttpResponse.json({ ...detail([track(1, "Alpha")]), plex: partialAdmin(3, 0) }),
      ),
    );
    renderWithProviders(<PlaylistDetailPage />, {
      route: `/playlists/${ID}`,
      path: "/playlists/:playlistId",
    });
    // A target last synced before the identities existed carries none (the cap
    // truncates to 200, never to 0), and one re-sync is what fixes it.
    expect(await screen.findByText("3 not in Plex; re-sync to see which")).toBeInTheDocument();
  });

  test("says the Plex copy was left alone when nothing resolved but a copy exists", async () => {
    server.use(
      http.get(BASE, () =>
        HttpResponse.json({
          ...detail([track(1, "Alpha")]),
          plex: {
            admin: {
              rating_key: "500",
              status: "empty",
              missing: 3,
              missing_tracks: [],
              synced_at: "2026-08-15T10:00:00+00:00",
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
    expect(
      await screen.findByText("No matching tracks; Plex copy left as is"),
    ).toBeInTheDocument();
  });

  test("says no matching tracks when nothing resolved and no copy exists", async () => {
    server.use(
      http.get(BASE, () =>
        HttpResponse.json({
          ...detail([track(1, "Alpha")]),
          plex: {
            admin: {
              rating_key: null,
              status: "empty",
              missing: 3,
              missing_tracks: [],
              synced_at: "2026-08-15T10:00:00+00:00",
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
    // The worse of the two empty outcomes — the user pressed Sync and Plex got
    // nothing — so it must not read quieter than "copy left as is": same
    // warning tone, and the label says what actually happened.
    const line = await screen.findByText("No matching tracks; nothing sent to Plex");
    expect(line).toHaveClass("text-warning");
  });

  test("an empty playlist that synced nothing is not an alarm", async () => {
    server.use(
      http.get(BASE, () =>
        HttpResponse.json({
          ...detail([]),
          plex: {
            admin: {
              rating_key: null,
              status: "empty",
              missing: 0,
              missing_tracks: [],
              synced_at: "2026-08-15T10:00:00+00:00",
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
    // No tracks resolved because there were none to resolve — nothing went
    // wrong, so no warning tone; "nothing sent to Plex" is for a real miss.
    const line = await screen.findByText("Nothing to sync");
    expect(line).not.toHaveClass("text-warning");
    expect(screen.queryByText("No matching tracks; nothing sent to Plex")).toBeNull();
  });

  // ——— How the tracks matched (PlexMatchCounts). The bug this exists for: a
  // wrong library_path made every path lookup miss for the app's whole life
  // while the weakest fallback carried 100% of every sync, and every sync still
  // reported a flat "ok". These pin that a sync now says HOW it matched — and,
  // just as hard, that a record with no tally stays silent instead of reading
  // as "0 by file". ———

  /** The exact caveat the mixed case shows; asserted verbatim so a reworded
   * warning can't quietly stop saying what a weak match risks. */
  const WEAK_NOTE =
    "The rest were matched on their tags rather than their files, which can land on a different copy of a track.";

  /** An admin target that pushed cleanly, carrying a per-rung tally. Unnamed
   * rungs default to 0 exactly as the server's model does. */
  function adminMatched(counts: { path?: number; artist_title?: number; album_length?: number }) {
    return {
      admin: {
        rating_key: "900",
        status: "ok",
        missing: 0,
        missing_tracks: [],
        matched_by: { path: 0, artist_title: 0, album_length: 0, ...counts },
        synced_at: "2026-08-16T10:00:00+00:00",
        error: null,
      },
    };
  }

  /** Render the detail page against one `plex` state map. */
  function renderWithPlex(plex: Record<string, unknown>) {
    server.use(
      http.get(BASE, () => HttpResponse.json({ ...detail([track(1, "Alpha")]), plex })),
    );
    renderWithProviders(<PlaylistDetailPage />, {
      route: `/playlists/${ID}`,
      path: "/playlists/:playlistId",
    });
  }

  test("a sync that found every track by file says so, and says nothing else", async () => {
    renderWithPlex(adminMatched({ path: 28 }));
    expect(await screen.findByText("Matched 28 tracks by file.")).toBeInTheDocument();
    // The overwhelmingly common outcome: one muted line and no escalation —
    // neither the weak-match caveat nor the broken-path warning.
    expect(screen.getByText("Matched 28 tracks by file.")).toHaveClass("text-muted-foreground");
    expect(screen.queryByText(WEAK_NOTE)).not.toBeInTheDocument();
    expect(screen.queryByText(/nothing matched by file/i)).not.toBeInTheDocument();
  });

  test("a partly-weak sync counts each method and stays out of the warning box", async () => {
    renderWithPlex(adminMatched({ path: 22, artist_title: 4, album_length: 2 }));
    expect(
      await screen.findByText(
        "Matched 28 tracks: 22 by file, 4 by artist and title, 2 by album and length.",
      ),
    ).toBeInTheDocument();
    // Some tracks DID match by file, so the path config is working: the caveat
    // is muted body text, not the warning box. Escalating an ordinary handful
    // of tag matches would teach the user to ignore the box that matters.
    expect(screen.getByText(WEAK_NOTE)).toHaveClass("text-muted-foreground");
    expect(screen.queryByText(/nothing matched by file/i)).not.toBeInTheDocument();
  });

  test("a sync where nothing matched by file warns, and names the setting to fix", async () => {
    renderWithPlex(adminMatched({ artist_title: 22, album_length: 6 }));
    // The zero is SPELLED OUT. Dropping the clause because it is zero would
    // hide the one fact the owner needed for years.
    expect(
      await screen.findByText(
        "Matched 28 tracks: none by file, 22 by artist and title, 6 by album and length.",
      ),
    ).toBeInTheDocument();
    const warning = screen.getByText(/Nothing matched by file\./).closest("p");
    expect(warning).toHaveClass("bg-warning/10");
    expect(screen.getByRole("link", { name: "Settings" })).toHaveAttribute(
      "href",
      "/settings/integrations",
    );
  });

  test("a failed target's all-zero tally is silence, not a 'none by file' alarm", async () => {
    renderWithPlex({
      admin: {
        rating_key: "900",
        status: "failed",
        missing: 0,
        missing_tracks: [],
        // The server zeroes the tally on a per-target failure even when the
        // library resolution itself succeeded — so zero here is "no news",
        // and reading it as evidence would invent the false alarm this whole
        // surface exists to avoid.
        matched_by: { path: 0, artist_title: 0, album_length: 0 },
        synced_at: "2026-08-16T10:00:00+00:00",
        error: "Couldn’t sync to this Plex account.",
      },
    });
    expect(await screen.findByText("Couldn’t sync to this Plex account.")).toBeInTheDocument();
    expect(screen.queryByText(/none by file/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/nothing matched by file/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/^Matched \d/)).not.toBeInTheDocument();
  });

  test("a playlist synced before the tally existed says nothing about matching", async () => {
    renderWithPlex({
      // No `matched_by` at all: the field is optional on the wire, and every
      // record written before the server started tallying looks like this.
      admin: {
        rating_key: "900",
        status: "ok",
        missing: 0,
        missing_tracks: [],
        synced_at: "2026-08-16T10:00:00+00:00",
        error: null,
      },
    });
    expect(await screen.findByText("Synced")).toBeInTheDocument();
    expect(screen.queryByText(/by file/i)).not.toBeInTheDocument();
  });

  test("the tally is reported once for the sync, not once per Plex account", async () => {
    server.use(
      http.get(USERS, () =>
        HttpResponse.json({ users: [{ id: "7", name: "Partner", home: true }] }),
      ),
      http.get(BASE, () =>
        HttpResponse.json({
          ...detail([track(1, "Alpha")]),
          target_plex_users: ["7"],
          // One library lookup serves every account, so both targets carry the
          // SAME numbers. Printing them under each name would say each account
          // was looked up separately.
          plex: {
            ...adminMatched({ path: 28 }),
            "7": { ...adminMatched({ path: 28 }).admin, rating_key: "901" },
          },
        }),
      ),
    );
    renderWithProviders(<PlaylistDetailPage />, {
      route: `/playlists/${ID}`,
      path: "/playlists/:playlistId",
    });
    expect(await screen.findByRole("checkbox", { name: /partner/i })).toBeChecked();
    expect(screen.getAllByText("Matched 28 tracks by file.")).toHaveLength(1);
  });

  test("a failed admin push doesn't erase the tally a target that went through recorded", async () => {
    server.use(
      http.get(USERS, () =>
        HttpResponse.json({ users: [{ id: "7", name: "Partner", home: true }] }),
      ),
      http.get(BASE, () =>
        HttpResponse.json({
          ...detail([track(1, "Alpha")]),
          target_plex_users: ["7"],
          plex: {
            // The owner's own push failed, so the server zeroed ITS tally. The
            // library scan runs ONCE, before the per-target loop
            // (`sync_playlist_to_targets`), so the target that did go through
            // carries the numbers that scan produced — the same ones admin
            // would have carried.
            admin: {
              rating_key: "900",
              status: "failed",
              missing: 0,
              missing_tracks: [],
              matched_by: { path: 0, artist_title: 0, album_length: 0 },
              synced_at: "2026-08-16T10:00:00+00:00",
              error: "Couldn’t sync to this Plex account.",
            },
            "7": { ...adminMatched({ artist_title: 28 }).admin, rating_key: "901" },
          },
        }),
      ),
    );
    renderWithProviders(<PlaylistDetailPage />, {
      route: `/playlists/${ID}`,
      path: "/playlists/:playlistId",
    });

    // Reading `admin` alone renders NOTHING here: the summary, and with it the
    // "nothing matched by file" warning this surface exists for, disappears on
    // a sync whose library scan did run and matched every track by tags alone —
    // the exact signature of a wrong library path.
    expect(
      await screen.findByText("Matched 28 tracks: none by file, 28 by artist and title."),
    ).toBeInTheDocument();
    expect(screen.getByText(/Nothing matched by file\./).closest("p")).toHaveClass(
      "bg-warning/10",
    );
    // …and it says so WITHOUT covering for the push that failed.
    expect(screen.getByText("Couldn’t sync to this Plex account.")).toBeInTheDocument();
  });

  test("a sync where every target failed still says nothing about matching", async () => {
    const failed = (ratingKey: string) => ({
      rating_key: ratingKey,
      status: "failed",
      missing: 0,
      missing_tracks: [],
      matched_by: { path: 0, artist_title: 0, album_length: 0 },
      synced_at: "2026-08-16T10:00:00+00:00",
      error: "Couldn’t sync to this Plex account.",
    });
    renderWithPlex({ admin: failed("900"), "7": failed("901") });

    expect(await screen.findByText("Alpha")).toBeInTheDocument();
    // Looking past admin for numbers must not turn "no target has any" into "0
    // by file": to a user whose sync never got far enough to look, that reads
    // as a broken library path.
    expect(screen.queryByText(/^Matched \d/)).not.toBeInTheDocument();
    expect(screen.queryByText(/none by file/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/nothing matched by file/i)).not.toBeInTheDocument();
  });

  test("a sync that matched nothing by file announces it in the live region", async () => {
    let synced = false;
    const after = adminMatched({ artist_title: 3 });
    server.use(
      http.get(BASE, () =>
        HttpResponse.json({ ...detail([track(1, "Alpha")]), plex: synced ? after : {} }),
      ),
      http.post(`${BASE}/sync`, () => {
        synced = true;
        return HttpResponse.json({ ...detail([track(1, "Alpha")]), plex: after });
      }),
    );
    renderWithProviders(<PlaylistDetailPage />, {
      route: `/playlists/${ID}`,
      path: "/playlists/:playlistId",
    });
    await screen.findByText("Alpha");
    // The region is MOUNTED and empty before the sync — a live region inserted
    // together with its text is not announced. The assertions below then watch
    // that same element gain the sentence.
    const region = document.querySelector('p[aria-live="polite"]');
    expect(region).not.toBeNull();
    expect(region?.textContent).toBe("");
    await userEvent.click(screen.getByRole("button", { name: /sync to plex/i }));
    await waitFor(() =>
      expect(region).toHaveTextContent(
        "Plex sync: Synced. Matched 3 tracks: none by file, 3 by artist and title. " +
          "Nothing matched by file — check the Plex library path in Settings.",
      ),
    );
  });

  test("announces the tally after a failed admin push, as one readable sentence", async () => {
    let synced = false;
    const after = {
      admin: {
        rating_key: "900",
        status: "failed",
        missing: 0,
        missing_tracks: [],
        matched_by: { path: 0, artist_title: 0, album_length: 0 },
        synced_at: "2026-08-16T10:00:00+00:00",
        error: "Couldn’t sync to this Plex account.",
      },
      "7": { ...adminMatched({ artist_title: 3 }).admin, rating_key: "901" },
    };
    server.use(
      http.get(BASE, () =>
        HttpResponse.json({ ...detail([track(1, "Alpha")]), plex: synced ? after : {} }),
      ),
      http.post(`${BASE}/sync`, () => {
        synced = true;
        return HttpResponse.json({ ...detail([track(1, "Alpha")]), plex: after });
      }),
    );
    renderWithProviders(<PlaylistDetailPage />, {
      route: `/playlists/${ID}`,
      path: "/playlists/:playlistId",
    });
    await screen.findByText("Alpha");
    const region = document.querySelector('p[aria-live="polite"]');
    await userEvent.click(screen.getByRole("button", { name: /sync to plex/i }));

    // Read aloud in one go, so the seam between the status and the tally has to
    // be one full stop: a server error label already ends in one, where
    // "Synced" does not, and only a failed target with the numbers surviving
    // elsewhere puts the two together.
    await waitFor(() =>
      expect(region?.textContent).toBe(
        "Plex sync: Couldn’t sync to this Plex account. " +
          "Matched 3 tracks: none by file, 3 by artist and title. " +
          "Nothing matched by file — check the Plex library path in Settings.",
      ),
    );
  });

  test("a healthy sync announces exactly what it always did", async () => {
    let synced = false;
    const after = adminMatched({ path: 3 });
    server.use(
      http.get(BASE, () =>
        HttpResponse.json({ ...detail([track(1, "Alpha")]), plex: synced ? after : {} }),
      ),
      http.post(`${BASE}/sync`, () => {
        synced = true;
        return HttpResponse.json({ ...detail([track(1, "Alpha")]), plex: after });
      }),
    );
    renderWithProviders(<PlaylistDetailPage />, {
      route: `/playlists/${ID}`,
      path: "/playlists/:playlistId",
    });
    await screen.findByText("Alpha");
    const region = document.querySelector('p[aria-live="polite"]');
    await userEvent.click(screen.getByRole("button", { name: /sync to plex/i }));
    // Exact equality is the point: everything-by-file is the normal case, so
    // the announcement gains not one word. The counts are still on screen for
    // anyone who goes looking.
    await waitFor(() => expect(region?.textContent).toBe("Plex sync: Synced"));
    expect(await screen.findByText("Matched 3 tracks by file.")).toBeInTheDocument();
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

  test("delete dialog counts the synced Plex copies that actually exist", async () => {
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
            "7": {
              rating_key: "2",
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
    expect(
      await screen.findByText(/Also removes its 2 synced Plex copies on Plex\./),
    ).toBeInTheDocument();
  });

  test("delete dialog omits the synced-copies note when no copy has a rating key", async () => {
    server.use(
      http.get(BASE, () =>
        HttpResponse.json({
          ...detail([track(1, "Alpha")]),
          // A target that was never created on Plex (no rating_key) must not be
          // counted as a synced copy — even though it occupies a plex slot.
          plex: {
            admin: {
              rating_key: null,
              status: "failed",
              missing: 0,
              synced_at: null,
              error: "boom",
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
    expect(screen.queryByText(/synced Plex/)).not.toBeInTheDocument();
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

  // ——— Artwork editor (Task 6) ———

  test("uploads custom artwork and the header cover shows the new hash", async () => {
    let uploaded = false;
    let bodyLen = -1;
    server.use(
      http.get(BASE, () =>
        HttpResponse.json({
          ...detail([track(1, "Alpha")]),
          artwork_hash: uploaded ? "newhash" : null,
        }),
      ),
      http.put(`${BASE}/artwork`, async ({ request }) => {
        bodyLen = (await request.arrayBuffer()).byteLength;
        uploaded = true;
        return HttpResponse.json({ ...detail([track(1, "Alpha")]), artwork_hash: "newhash" });
      }),
    );
    renderWithProviders(<PlaylistDetailPage />, {
      route: `/playlists/${ID}`,
      path: "/playlists/:playlistId",
    });
    await screen.findByText("Alpha");
    // No custom artwork yet (no cover-art image in the header).
    expect(document.querySelector('[data-slot="cover-art"]')).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: /edit artwork/i }));
    fireEvent.change(screen.getByLabelText(/upload artwork image/i), {
      target: { files: [makeImageFile()] },
    });
    // The raw image bytes reach the endpoint…
    await waitFor(() => expect(bodyLen).toBeGreaterThan(0));
    // …and the detail refetch re-renders the header cover with the content-hash
    // cache-bust (Task 5's `?v=<hash>`).
    await waitFor(() => {
      const img = document.querySelector('[data-slot="cover-art"]');
      expect(img?.getAttribute("src") ?? "").toContain("newhash");
    });
  });

  test("removes custom artwork and the header falls back to the collage", async () => {
    let removed = false;
    server.use(
      http.get(BASE, () =>
        HttpResponse.json({
          ...detail([track(1, "Alpha")]),
          artwork_hash: removed ? null : "abc",
          cover_album_ids: [11, 22],
        }),
      ),
      http.delete(`${BASE}/artwork`, () => {
        removed = true;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    renderWithProviders(<PlaylistDetailPage />, {
      route: `/playlists/${ID}`,
      path: "/playlists/:playlistId",
    });
    await screen.findByText("Alpha");
    // Custom artwork shows first (a single cover image, not the collage).
    expect(document.querySelector('[data-slot="cover-art"]')).not.toBeNull();
    await userEvent.click(screen.getByRole("button", { name: /edit artwork/i }));
    await userEvent.click(screen.getByRole("button", { name: /remove artwork/i }));
    // With the custom art gone the album-cover collage takes over.
    await waitFor(() =>
      expect(document.querySelector('[data-slot="playlist-cover-collage"]')).not.toBeNull(),
    );
  });

  test("the Remove button is hidden when there is no custom artwork", async () => {
    server.use(
      http.get(BASE, () =>
        HttpResponse.json({ ...detail([track(1, "Alpha")]), artwork_hash: null }),
      ),
    );
    renderWithProviders(<PlaylistDetailPage />, {
      route: `/playlists/${ID}`,
      path: "/playlists/:playlistId",
    });
    await screen.findByText("Alpha");
    await userEvent.click(screen.getByRole("button", { name: /edit artwork/i }));
    expect(screen.queryByRole("button", { name: /remove artwork/i })).not.toBeInTheDocument();
  });

  test("surfaces the server's message when the image type is rejected (415)", async () => {
    server.use(
      http.get(BASE, () => HttpResponse.json(detail([track(1, "Alpha")]))),
      http.put(`${BASE}/artwork`, () =>
        HttpResponse.json(
          { detail: "Unsupported image type (JPEG or PNG only)" },
          { status: 415 },
        ),
      ),
    );
    renderWithProviders(<PlaylistDetailPage />, {
      route: `/playlists/${ID}`,
      path: "/playlists/:playlistId",
    });
    await screen.findByText("Alpha");
    await userEvent.click(screen.getByRole("button", { name: /edit artwork/i }));
    fireEvent.change(screen.getByLabelText(/upload artwork image/i), {
      target: { files: [makeImageFile("art.png", "image/png")] },
    });
    expect(await screen.findByText(/unsupported image type/i)).toBeInTheDocument();
  });

  test("Match is offered on pending, unavailable and not-in-Plex rows, not on a clean one", async () => {
    server.use(
      http.get(BASE, () =>
        HttpResponse.json({
          ...detail([
            track(1, "Clean"),
            track(2, "Missed"),
            track(3, "Gone", false),
            pendingTrack("u4", "Ghost"),
          ]),
          plex: {
            admin: {
              rating_key: "500",
              status: "partial",
              missing: 1,
              missing_tracks: [
                { item_id: 2, title: "Missed", albumartist: "A", album: "B", reason: "not_found" },
              ],
              synced_at: "2026-08-15T10:00:00+00:00",
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
    await screen.findByText("Clean");
    // A healthy, placed row keeps no action - 61 identical buttons down a
    // healthy playlist is noise.
    expect(screen.queryByRole("button", { name: /match clean/i })).toBeNull();
    // Everything that needs attention gets one.
    expect(screen.getByRole("button", { name: /match missed/i })).toBeInTheDocument();
    // `track(3, "Gone", false)` is available:false with a non-empty title, so
    // `displayTitle` (PlaylistDetailPage.tsx:73-75) returns "Gone", not
    // "(removed track)" - that fallback only fires on an EMPTY title.
    expect(screen.getByRole("button", { name: /match gone/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /match ghost/i })).toBeInTheDocument();
  });

  test("re-pointing a resolved row says it replaces the track and announces that", async () => {
    let patchBody: unknown = null;
    server.use(
      http.get(BASE, () =>
        HttpResponse.json({
          ...detail([track(2, "Missed")]),
          plex: {
            admin: {
              rating_key: "500",
              status: "partial",
              missing: 1,
              missing_tracks: [
                { item_id: 2, title: "Missed", albumartist: "A", album: "B", reason: "not_found" },
              ],
              synced_at: "2026-08-15T10:00:00+00:00",
              error: null,
            },
          },
        }),
      ),
      http.get(SEARCH, () =>
        HttpResponse.json(
          trackSearchPage([
            {
              id: 77,
              title: "Other copy",
              artist: "Real",
              album: "Disc",
              album_id: 3,
              duration_seconds: 180,
            },
          ]),
        ),
      ),
      http.patch(`${BASE}/entries/u2`, async ({ request }) => {
        patchBody = await request.json();
        return HttpResponse.json(detail([track(77, "Other copy")]));
      }),
    );
    renderWithProviders(<PlaylistDetailPage />, {
      route: `/playlists/${ID}`,
      path: "/playlists/:playlistId",
    });
    await screen.findByText("Missed");
    await userEvent.click(screen.getByRole("button", { name: /match missed/i }));

    // The picker says what the pick will DO on a row that already has a track:
    // it replaces it and keeps the slot. It must not imply a row is added.
    const dialog = await screen.findByRole("dialog");
    expect(
      within(dialog).getByText(
        "Search your library and pick the track this row should point to instead. It replaces the current track and keeps its position.",
      ),
    ).toBeInTheDocument();

    await userEvent.type(within(dialog).getByRole("textbox"), "Other");
    await userEvent.click(await within(dialog).findByRole("button", { name: /select other copy/i }));

    await waitFor(() => expect(patchBody).toEqual({ item_id: 77 }));
    // The live region announces a REPLACEMENT, not a first match.
    await waitFor(() =>
      expect(screen.getByText("Replaced the track; the row kept its position")).toBeInTheDocument(),
    );
  });

  // The mirror of the test above, and the one that pins `armedReplaces`'s OTHER
  // branch. Without it, widening the flag to `armed !== null` — i.e. "any armed
  // row replaces" — passes the whole suite, and every pending row starts
  // claiming it replaced a track it never had.
  test("a pending row is told it is being matched, not that a track is being replaced", async () => {
    let patchBody: unknown = null;
    server.use(
      http.get(BASE, () => HttpResponse.json(detail([pendingTrack("u1", "Lost", { artist: "X" })]))),
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
        return HttpResponse.json(detail([track(55, "Found")]));
      }),
    );
    renderWithProviders(<PlaylistDetailPage />, {
      route: `/playlists/${ID}`,
      path: "/playlists/:playlistId",
    });
    await screen.findByText("Lost");
    await userEvent.click(screen.getByRole("button", { name: /match lost/i }));

    // A pending row has no track behind it, so the picker must use the MATCH
    // wording: this slot gains a track, it does not swap one out.
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("Match to a library track")).toBeInTheDocument();
    expect(
      within(dialog).getByText("Search your library and pick the track this entry should point to."),
    ).toBeInTheDocument();
    expect(within(dialog).queryByText(/replaces the current track/i)).not.toBeInTheDocument();

    await userEvent.type(within(dialog).getByRole("textbox"), "Found");
    await userEvent.click(await within(dialog).findByRole("button", { name: /select found/i }));

    await waitFor(() => expect(patchBody).toEqual({ item_id: 55 }));
    // …and the live region announces a MATCH, not a replacement.
    await waitFor(() =>
      expect(screen.getByText("Matched the track to your library")).toBeInTheDocument(),
    );
    expect(
      screen.queryByText("Replaced the track; the row kept its position"),
    ).not.toBeInTheDocument();
  });

  /** This page + a "Road trip" source to merge from, with the merge itself
   * answering `result`. The counts in `result` are deliberately free of the
   * fixtures: the announcement must quote the SERVER, never recount. */
  function serveMerge(result: {
    added: number;
    skipped_duplicates: number;
    source_deleted: boolean;
  }) {
    const LIST = `${window.location.origin}/api/playlists`;
    const SOURCE = "b".repeat(32);
    const listed = (id: string, name: string, trackCount: number) => ({
      id,
      name,
      description: "",
      track_count: trackCount,
      pending_count: 0,
      target_plex_users: [],
      plex: {},
      created_at: "2026-08-16T00:00:00+00:00",
      updated_at: "2026-08-16T00:00:00+00:00",
      artwork_hash: null,
      cover_album_ids: [],
    });
    server.use(
      http.get(BASE, () => HttpResponse.json(detail([track(1, "Alpha")]))),
      http.get(LIST, () =>
        HttpResponse.json([listed(ID, "Late night", 1), listed(SOURCE, "Road trip", 2)]),
      ),
      http.get(`${LIST}/${SOURCE}`, () =>
        HttpResponse.json({
          ...detail([track(9, "Nine")], "Road trip"),
          id: SOURCE,
        }),
      ),
      http.post(`${BASE}/merge`, () =>
        HttpResponse.json({
          playlist: detail([track(1, "Alpha"), track(9, "Nine")]),
          ...result,
        }),
      ),
    );
  }

  /** Open the merge dialog, pick "Road trip", commit. */
  async function mergeRoadTrip() {
    renderWithProviders(<PlaylistDetailPage />, {
      route: `/playlists/${ID}`,
      path: "/playlists/:playlistId",
    });
    await screen.findByText("Alpha");

    await userEvent.click(screen.getByRole("button", { name: /merge another playlist/i }));
    await userEvent.click(await screen.findByRole("button", { name: /road trip/i }));
    await userEvent.click(screen.getByRole("button", { name: /^merge$/i }));
  }

  test("merging another playlist announces the counts the server returned", async () => {
    serveMerge({ added: 4, skipped_duplicates: 2, source_deleted: false });

    await mergeRoadTrip();

    await waitFor(() =>
      expect(
        screen.getByText(
          "Merged Road trip: added 4 tracks, skipped 2 already here. Sync to Plex to push the change.",
        ),
      ).toBeInTheDocument(),
    );
    expect(await screen.findByText("Nine")).toBeInTheDocument();
  });

  test("a merge that deleted the source says so, and counts one track in the singular", async () => {
    // The two clauses the counts-quoting test above never reaches: the ticked
    // "delete afterwards" outcome, and the singular "track" for added === 1.
    // Skipping stays at 0, so the middle clause is correctly absent.
    serveMerge({ added: 1, skipped_duplicates: 0, source_deleted: true });

    await mergeRoadTrip();

    await waitFor(() =>
      expect(
        screen.getByText(
          "Merged Road trip: added 1 track, and deleted Road trip. Sync to Plex to push the change.",
        ),
      ).toBeInTheDocument(),
    );
  });
});
