import { act, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { useState } from "react";
import { describe, expect, test, vi } from "vitest";

import type { PlaylistDetail } from "@/api/usePlaylists";
import { MergePlaylistDialog } from "@/components/playlists/MergePlaylistDialog";
import { renderWithProviders } from "@/test/render";
import { server } from "@/test/msw-server";

const TARGET = "a".repeat(32);
const SOURCE = "b".repeat(32);
const LIST = `${window.location.origin}/api/playlists`;

function summary(id: string, name: string, trackCount: number) {
  return {
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
  };
}

function track(id: number | null, uid: string, title: string) {
  return {
    uid,
    id,
    title,
    artist: "Artist",
    album: "Album",
    duration_seconds: 200,
    available: id !== null,
    pending: id === null,
  };
}

function targetDetail(tracks: ReturnType<typeof track>[]) {
  return { ...summary(TARGET, "Keep", tracks.length), tracks };
}

/** Mounts the dialog CLOSED behind a button, so a test can watch what the
 * component does before anyone opens it. */
function Harness({ target }: { target: PlaylistDetail }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        open it
      </button>
      <MergePlaylistDialog
        target={target}
        open={open}
        onOpenChange={setOpen}
        onMerged={vi.fn()}
      />
    </>
  );
}

function renderDialog(
  target = targetDetail([track(1, "t1", "Alpha")]),
  onMerged = vi.fn(),
) {
  renderWithProviders(
    <MergePlaylistDialog target={target} open onOpenChange={() => {}} onMerged={onMerged} />,
  );
  return onMerged;
}

describe("MergePlaylistDialog", () => {
  test("lists the other playlists with their counts and excludes this one", async () => {
    server.use(
      http.get(LIST, () =>
        HttpResponse.json([summary(TARGET, "Keep", 1), summary(SOURCE, "Road trip", 42)]),
      ),
    );
    renderDialog();
    expect(await screen.findByRole("button", { name: /road trip/i })).toBeInTheDocument();
    expect(screen.getByText("42 tracks")).toBeInTheDocument();
    // Merging a playlist into itself is refused server-side; never offer it.
    expect(screen.queryByRole("button", { name: /^keep/i })).toBeNull();
  });

  test("the delete checkbox defaults off and is only offered once a source is picked", async () => {
    server.use(
      http.get(LIST, () =>
        HttpResponse.json([summary(TARGET, "Keep", 1), summary(SOURCE, "Road trip", 2)]),
      ),
      http.get(`${LIST}/${SOURCE}`, () =>
        HttpResponse.json({ ...summary(SOURCE, "Road trip", 2), tracks: [track(9, "s1", "Nine")] }),
      ),
    );
    renderDialog();
    expect(screen.queryByRole("checkbox")).toBeNull();
    await userEvent.click(await screen.findByRole("button", { name: /road trip/i }));
    const box = await screen.findByRole("checkbox", { name: /delete road trip afterwards/i });
    expect(box).toHaveAttribute("aria-checked", "false");
  });

  test("the visible delete label is clickable, named once, and keyboard-toggleable", async () => {
    server.use(
      http.get(LIST, () =>
        HttpResponse.json([summary(TARGET, "Keep", 1), summary(SOURCE, "Road trip", 2)]),
      ),
      http.get(`${LIST}/${SOURCE}`, () =>
        HttpResponse.json({ ...summary(SOURCE, "Road trip", 2), tracks: [track(9, "s1", "Nine")] }),
      ),
    );
    renderDialog();
    await userEvent.click(await screen.findByRole("button", { name: /road trip/i }));
    const box = await screen.findByRole("checkbox", { name: /delete road trip afterwards/i });
    const label = screen.getByText("Delete Road trip afterwards");

    // People click the words, not the 16px square.
    await userEvent.click(label);
    expect(box).toHaveAttribute("aria-checked", "true");
    await userEvent.click(label);
    expect(box).toHaveAttribute("aria-checked", "false");

    // Exactly ONE accessible name: the words ride on the control's aria-label
    // (a <label for> does not associate with Radix's role="checkbox" button),
    // so the visible copy must stay out of the accessibility tree or a screen
    // reader announces the same sentence twice.
    expect(box).toHaveAccessibleName("Delete Road trip afterwards");
    expect(label).toHaveAttribute("aria-hidden", "true");

    // The control itself is still the focusable, operable one - the clickable
    // words add no second tab stop.
    (box as HTMLElement).focus();
    expect(box).toHaveFocus();
    await userEvent.keyboard(" ");
    expect(box).toHaveAttribute("aria-checked", "true");
  });

  test("previews what will come across and what is already here", async () => {
    server.use(
      http.get(LIST, () =>
        HttpResponse.json([summary(TARGET, "Keep", 1), summary(SOURCE, "Road trip", 3)]),
      ),
      http.get(`${LIST}/${SOURCE}`, () =>
        HttpResponse.json({
          ...summary(SOURCE, "Road trip", 3),
          // id 1 is already in the target; id 9 is not. The pending row has no
          // id at all, and its remembered title deliberately COLLIDES with the
          // target's resolved "Alpha": grey rows are copied verbatim and never
          // text-matched, so it still comes across as its own row. A preview
          // that matched pending rows on title would report "1 track will come
          // across; 2 already here" and fail here.
          tracks: [track(1, "s1", "Alpha"), track(9, "s2", "Nine"), track(null, "s3", "Alpha")],
        }),
      ),
    );
    renderDialog();
    await userEvent.click(await screen.findByRole("button", { name: /road trip/i }));
    expect(await screen.findByText(/2 tracks will come across; 1 already here\./i)).toBeInTheDocument();
  });

  test("merges with the ticked delete flag and hands back the server's counts", async () => {
    let body: unknown = null;
    server.use(
      http.get(LIST, () =>
        HttpResponse.json([summary(TARGET, "Keep", 1), summary(SOURCE, "Road trip", 2)]),
      ),
      http.get(`${LIST}/${SOURCE}`, () =>
        HttpResponse.json({ ...summary(SOURCE, "Road trip", 2), tracks: [track(9, "s1", "Nine")] }),
      ),
      http.post(`${LIST}/${TARGET}/merge`, async ({ request }) => {
        body = await request.json();
        return HttpResponse.json({
          playlist: targetDetail([track(1, "t1", "Alpha"), track(9, "n1", "Nine")]),
          added: 1,
          skipped_duplicates: 0,
          source_deleted: true,
        });
      }),
    );
    const onMerged = renderDialog();
    await userEvent.click(await screen.findByRole("button", { name: /road trip/i }));
    await userEvent.click(
      await screen.findByRole("checkbox", { name: /delete road trip afterwards/i }),
    );
    await userEvent.click(screen.getByRole("button", { name: /^merge$/i }));

    await waitFor(() => expect(body).toEqual({ source_id: SOURCE, delete_source: true }));
    await waitFor(() =>
      expect(onMerged).toHaveBeenCalledWith(
        expect.objectContaining({ added: 1, skipped_duplicates: 0, source_deleted: true }),
        "Road trip",
      ),
    );
  });

  test("requests nothing until it is opened", async () => {
    let listCalls = 0;
    server.use(
      http.get(LIST, () => {
        listCalls += 1;
        return HttpResponse.json([summary(TARGET, "Keep", 1), summary(SOURCE, "Road trip", 2)]);
      }),
    );
    // Mounted CLOSED, the way a page that keeps the dialog around would do it:
    // the playlist list is only needed once there is a picker to fill, and
    // several pages' tests serve no /api/playlists handler at all (msw runs
    // onUnhandledRequest: "error"), so an eager fetch is a live hazard.
    renderWithProviders(<Harness target={targetDetail([track(1, "t1", "Alpha")])} />);
    // Let a mount-time query start and let msw run its handler.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    expect(listCalls).toBe(0);

    await userEvent.click(screen.getByRole("button", { name: /open it/i }));

    // The counter is live - opening does fetch - so the 0 above is a real
    // absence, not a probe that never worked.
    expect(await screen.findByRole("button", { name: /road trip/i })).toBeInTheDocument();
    expect(listCalls).toBe(1);
  });

  test("says the merged playlist needs a sync to reach Plex", async () => {
    server.use(http.get(LIST, () => HttpResponse.json([summary(TARGET, "Keep", 1)])));
    renderDialog();
    expect(
      await screen.findByText(/needs a sync to reach plex/i),
    ).toBeInTheDocument();
  });
});
