import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, test, vi } from "vitest";

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

  test("says the merged playlist needs a sync to reach Plex", async () => {
    server.use(http.get(LIST, () => HttpResponse.json([summary(TARGET, "Keep", 1)])));
    renderDialog();
    expect(
      await screen.findByText(/needs a sync to reach plex/i),
    ).toBeInTheDocument();
  });
});
