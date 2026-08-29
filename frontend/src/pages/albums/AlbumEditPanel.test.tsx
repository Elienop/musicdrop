import { describe, expect, it, vi, beforeEach } from "vitest";
import {
  render,
  screen,
  fireEvent,
  waitFor,
  within,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AlbumEditPanel } from "@/pages/albums/AlbumEditPanel";
import { client } from "@/api/client";
import type { AlbumDetail } from "@/api/useAlbum";

const album = {
  id: 7,
  album_artist: "Radiohead",
  title: "In Rainbows",
  year: 2007,
  genre: "Alternative Rock",
  track_count: 2,
  tracks: [
    {
      id: 1,
      title: "15 Step",
      track: 1,
      disc: 1,
      duration_seconds: 230,
      artist: "Radiohead",
    },
    {
      id: 2,
      title: "Bodysnatchers",
      track: 2,
      disc: 1,
      duration_seconds: 242,
      artist: "Radiohead",
    },
  ],
} as unknown as AlbumDetail;

/** Wrap mock data as a 2xx openapi-fetch result (the hooks read `response.ok`). */
function ok(data: unknown) {
  return {
    data,
    error: undefined,
    response: { ok: true, status: 200 },
  } as never;
}

function renderPanel() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <AlbumEditPanel album={album} onClose={() => {}} />
    </QueryClientProvider>,
  );
}

describe("AlbumEditPanel", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("previews a title change then shows the before/after diff", async () => {
    vi.spyOn(client, "POST").mockResolvedValue(
      ok({
        changed_fields: ["title"],
        album_before: { title: "In Rainbows" },
        album_after: { title: "In Rainbows (R)" },
        tracks: [],
        move_enabled: false,
        move_plan: [],
        move_refusals: [],
      }),
    );

    renderPanel();
    fireEvent.change(screen.getByLabelText(/album title/i), {
      target: { value: "In Rainbows (R)" },
    });
    fireEvent.click(screen.getByRole("button", { name: /preview/i }));

    const diff = await screen.findByRole("region", {
      name: /pending changes/i,
    });
    expect(within(diff).getByText("In Rainbows")).toBeInTheDocument();
    expect(within(diff).getByText("In Rainbows (R)")).toBeInTheDocument();
  });

  it("shows the move notice when move_enabled and move_plan present", async () => {
    vi.spyOn(client, "POST").mockResolvedValue(
      ok({
        changed_fields: ["album_artist"],
        album_before: { album_artist: "Radiohead" },
        album_after: { album_artist: "Radiohead (Live)" },
        tracks: [],
        move_enabled: true,
        move_plan: [
          {
            item_id: 1,
            track: 1,
            old_path: "/a/x.flac",
            new_path: "/b/x.flac",
          },
        ],
        move_refusals: [],
      }),
    );

    renderPanel();
    fireEvent.change(screen.getByLabelText(/album artist/i), {
      target: { value: "Radiohead (Live)" },
    });
    fireEvent.click(screen.getByRole("button", { name: /preview/i }));
    expect(
      await screen.findByText(/1 file will be moved/i),
    ).toBeInTheDocument();

    // Nothing refusal-related exists on a clean plan: no list, no count line, and
    // the notice does not trail a "0 others cannot be moved" clause.
    expect(
      screen.queryByRole("list", { name: /cannot be moved/i }),
    ).not.toBeInTheDocument();
    expect(screen.queryByText(/cannot be moved/i)).not.toBeInTheDocument();
  });

  it("lists refused moves with their reason, apart from the moved count", async () => {
    vi.spyOn(client, "POST").mockResolvedValue(
      ok({
        changed_fields: [],
        album_before: {},
        album_after: {},
        tracks: [
          {
            item_id: 2,
            title_before: "Bodysnatchers",
            title_after: "15 Step",
            track_before: 2,
            track_after: 2,
            artist_before: "Radiohead",
            artist_after: "Radiohead",
          },
        ],
        move_enabled: true,
        // The backend partitions: two renames can happen, one cannot. `move_plan`
        // never carries the refused one, so the headline count is already honest.
        move_plan: [
          {
            item_id: 3,
            track: 3,
            old_path: "/music/Radiohead/In Rainbows/03 Nude.flac",
            new_path: "/music/Radiohead/In Rainbows/03 Nude (remastered).flac",
          },
          {
            item_id: 4,
            track: 4,
            old_path: "/music/Radiohead/In Rainbows/04 Reckoner.flac",
            new_path:
              "/music/Radiohead/In Rainbows/04 Reckoner (remastered).flac",
          },
        ],
        move_refusals: [
          {
            item_id: 2,
            track: 2,
            old_path: "/music/Radiohead/In Rainbows/02 Bodysnatchers.flac",
            new_path: "/music/Radiohead/In Rainbows/01 15 Step.flac",
            detail:
              "Radiohead/In Rainbows/01 15 Step.flac: already exists on disk and holds 15 Step",
          },
        ],
      }),
    );

    renderPanel();
    fireEvent.change(screen.getByLabelText(/title of track 2/i), {
      target: { value: "15 Step" },
    });
    fireEvent.click(screen.getByRole("button", { name: /preview/i }));

    // The moved count counts only what will actually move; the refusal is counted
    // separately and never folded into it.
    const notice = await screen.findByText(/2 files will be moved/i);
    expect(notice).toHaveTextContent(/1 other file cannot be moved/i);

    // The refused rename is named per-track, with the self-contained reason.
    const refused = screen.getByRole("list", {
      name: /files that cannot be moved/i,
    });
    expect(refused).toHaveClass("text-destructive");
    const row = within(refused).getByText(
      /02 Bodysnatchers\.flac → 01 15 Step\.flac/,
    );
    expect(row).toHaveTextContent(/^#2 /);
    expect(
      within(refused).getByText(/already exists on disk and holds 15 Step/),
    ).toBeInTheDocument();

    // A user-opened preview never shouts: the refusal block is not an alert.
    expect(refused.closest("[role='alert']")).toBeNull();

    // The header states the consequence, so a partly-refused edit does not read
    // as dangerous and the apply banner is not new news.
    expect(
      screen.getByText(/tags will still be updated.*keep their current names/i),
    ).toBeInTheDocument();

    // Refusals do not gate the edit — the tags still write.
    expect(screen.getByRole("button", { name: /apply/i })).not.toBeDisabled();
  });

  it("names the parent folder when a refused move leaves the file name unchanged", async () => {
    vi.spyOn(client, "POST").mockResolvedValue(
      ok({
        changed_fields: ["title"],
        album_before: { title: "In Rainbows" },
        album_after: { title: "In Rainbows (Remaster)" },
        tracks: [],
        move_enabled: true,
        move_plan: [],
        // An album-title edit relocates the DIRECTORY: every basename is
        // unchanged, so basenames alone would read "01 15 Step.flac → 01 15
        // Step.flac" — a rename to itself.
        move_refusals: [
          {
            item_id: 1,
            track: 1,
            old_path: "/music/Radiohead/In Rainbows/01 15 Step.flac",
            new_path: "/music/Radiohead/In Rainbows (Remaster)/01 15 Step.flac",
            detail:
              "Radiohead/In Rainbows (Remaster)/01 15 Step.flac: already exists on disk and holds 15 Step",
          },
        ],
      }),
    );

    renderPanel();
    fireEvent.change(screen.getByLabelText(/album title/i), {
      target: { value: "In Rainbows (Remaster)" },
    });
    fireEvent.click(screen.getByRole("button", { name: /preview/i }));

    const refused = await screen.findByRole("list", {
      name: /files that cannot be moved/i,
    });
    const row = within(refused).getByText(/01 15 Step\.flac →/);
    expect(row).toHaveTextContent(
      "#1 In Rainbows/01 15 Step.flac → In Rainbows (Remaster)/01 15 Step.flac",
    );
  });

  it("shows the refusal block even when nothing can move", async () => {
    vi.spyOn(client, "POST").mockResolvedValue(
      ok({
        changed_fields: [],
        album_before: {},
        album_after: {},
        tracks: [
          {
            item_id: 2,
            title_before: "Bodysnatchers",
            title_after: "15 Step",
            track_before: 2,
            track_after: 2,
            artist_before: "Radiohead",
            artist_after: "Radiohead",
          },
        ],
        move_enabled: true,
        move_plan: [],
        move_refusals: [
          {
            item_id: 2,
            track: 2,
            old_path: "/music/Radiohead/In Rainbows/02 Bodysnatchers.flac",
            new_path: "/music/Radiohead/In Rainbows/01 15 Step.flac",
            detail:
              "Radiohead/In Rainbows/01 15 Step.flac: 2 tracks resolve to this same name: 01 15 Step, 02 Bodysnatchers",
          },
        ],
      }),
    );

    renderPanel();
    fireEvent.change(screen.getByLabelText(/title of track 2/i), {
      target: { value: "15 Step" },
    });
    fireEvent.click(screen.getByRole("button", { name: /preview/i }));

    const refused = await screen.findByRole("list", {
      name: /files that cannot be moved/i,
    });
    expect(
      within(refused).getByText(/2 tracks resolve to this same name/),
    ).toBeInTheDocument();
    // An empty move plan drops the notice entirely rather than promising 0 moves.
    expect(
      screen.queryByText(/will be moved on disk/i),
    ).not.toBeInTheDocument();
  });

  it("renders per-track before/after rows in the diff table", async () => {
    vi.spyOn(client, "POST").mockResolvedValue(
      ok({
        changed_fields: [],
        album_before: {},
        album_after: {},
        tracks: [
          {
            item_id: 1,
            title_before: "15 Step",
            title_after: "16 Step",
            track_before: 1,
            track_after: 1,
            artist_before: "Radiohead",
            artist_after: "Radiohead",
          },
        ],
        move_enabled: false,
        move_plan: [],
        move_refusals: [],
      }),
    );

    renderPanel();
    fireEvent.change(screen.getByLabelText(/title of track 1/i), {
      target: { value: "16 Step" },
    });
    fireEvent.click(screen.getByRole("button", { name: /preview/i }));

    const diff = await screen.findByRole("region", {
      name: /pending changes/i,
    });
    // "Now" still shows the old title; "After" shows the new one.
    expect(within(diff).getByText(/15 Step/)).toBeInTheDocument();
    expect(within(diff).getByText(/16 Step/)).toBeInTheDocument();
  });

  it("shows a No changes message when nothing differs", async () => {
    vi.spyOn(client, "POST").mockResolvedValue(
      ok({
        changed_fields: [],
        album_before: {},
        album_after: {},
        tracks: [],
        move_enabled: false,
        move_plan: [],
        move_refusals: [],
      }),
    );

    renderPanel();
    fireEvent.click(screen.getByRole("button", { name: /preview/i }));
    expect(await screen.findByText(/no changes/i)).toBeInTheDocument();
  });

  it("clears the stale preview when an input is edited", async () => {
    vi.spyOn(client, "POST").mockResolvedValue(
      ok({
        changed_fields: ["title"],
        album_before: { title: "In Rainbows" },
        album_after: { title: "In Rainbows (R)" },
        tracks: [],
        move_enabled: false,
        move_plan: [],
        move_refusals: [],
      }),
    );

    renderPanel();
    fireEvent.change(screen.getByLabelText(/album title/i), {
      target: { value: "In Rainbows (R)" },
    });
    fireEvent.click(screen.getByRole("button", { name: /preview/i }));
    await screen.findByRole("region", { name: /pending changes/i });

    // Apply enabled while a fresh preview is shown.
    expect(screen.getByRole("button", { name: /apply/i })).not.toBeDisabled();

    // Editing any field clears the preview and re-gates Apply.
    fireEvent.change(screen.getByLabelText("Genre"), {
      target: { value: "Rock" },
    });
    expect(
      screen.queryByRole("region", { name: /pending changes/i }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /apply/i })).toBeDisabled();
  });

  it("surfaces per-track write failures after apply", async () => {
    const post = vi.spyOn(client, "POST");
    // First POST = preview, second POST = apply result.
    post.mockResolvedValueOnce(
      ok({
        changed_fields: ["title"],
        album_before: { title: "In Rainbows" },
        album_after: { title: "In Rainbows (R)" },
        tracks: [],
        move_enabled: false,
        move_plan: [],
        move_refusals: [],
      }),
    );
    post.mockResolvedValueOnce(
      ok({
        album,
        items: [
          {
            item_id: 1,
            track: 1,
            title: "15 Step",
            written: true,
            moved: false,
            error: null,
          },
          {
            item_id: 2,
            track: 2,
            title: "Bodysnatchers",
            written: false,
            moved: false,
            error: "permission denied",
          },
        ],
        write_failures: 1,
        move_failures: 0,
      }),
    );

    renderPanel();
    fireEvent.change(screen.getByLabelText(/album title/i), {
      target: { value: "In Rainbows (R)" },
    });
    fireEvent.click(screen.getByRole("button", { name: /preview/i }));
    await screen.findByRole("region", { name: /pending changes/i });
    fireEvent.click(screen.getByRole("button", { name: /apply/i }));

    expect(await screen.findByText(/wrote 1 tag/i)).toBeInTheDocument();
    // The failed track is listed with its error, not hidden.
    expect(screen.getByText(/permission denied/i)).toBeInTheDocument();
    expect(screen.getByText(/Bodysnatchers/i)).toBeInTheDocument();
  });

  it("omits the track number on an apply row for an untracked file", async () => {
    const post = vi.spyOn(client, "POST");
    post.mockResolvedValueOnce(
      ok({
        changed_fields: ["title"],
        album_before: { title: "In Rainbows" },
        album_after: { title: "In Rainbows (R)" },
        tracks: [],
        move_enabled: false,
        move_plan: [],
        move_refusals: [],
      }),
    );
    post.mockResolvedValueOnce(
      ok({
        album,
        // beets reports a file with no track number as 0, not null — the preview
        // already suppresses it, so the apply row must not print "#0".
        items: [
          {
            item_id: 9,
            track: 0,
            title: "Hidden Track",
            written: false,
            moved: false,
            error: "permission denied",
          },
        ],
        write_failures: 1,
        move_failures: 0,
      }),
    );

    renderPanel();
    fireEvent.change(screen.getByLabelText(/album title/i), {
      target: { value: "In Rainbows (R)" },
    });
    fireEvent.click(screen.getByRole("button", { name: /preview/i }));
    await screen.findByRole("region", { name: /pending changes/i });
    fireEvent.click(screen.getByRole("button", { name: /apply/i }));

    const row = await screen.findByText(/Hidden Track/);
    expect(row).toHaveTextContent("Hidden Track: permission denied");
    expect(row).not.toHaveTextContent("#0");
  });

  it("clears the apply-outcome banner when a field is edited after apply", async () => {
    const post = vi.spyOn(client, "POST");
    post.mockResolvedValueOnce(
      ok({
        changed_fields: ["title"],
        album_before: { title: "In Rainbows" },
        album_after: { title: "In Rainbows (R)" },
        tracks: [],
        move_enabled: false,
        move_plan: [],
        move_refusals: [],
      }),
    );
    post.mockResolvedValueOnce(
      ok({
        album,
        items: [
          {
            item_id: 1,
            track: 1,
            title: "15 Step",
            written: true,
            moved: false,
            error: null,
          },
        ],
        write_failures: 0,
        move_failures: 0,
      }),
    );

    renderPanel();
    fireEvent.change(screen.getByLabelText(/album title/i), {
      target: { value: "In Rainbows (R)" },
    });
    fireEvent.click(screen.getByRole("button", { name: /preview/i }));
    await screen.findByRole("region", { name: /pending changes/i });
    fireEvent.click(screen.getByRole("button", { name: /apply/i }));

    // The success line appears after apply.
    expect(await screen.findByText(/^Updated/)).toBeInTheDocument();
    // ...and the close button reads "Done" — the edit is saved, nothing to cancel.
    expect(screen.getByRole("button", { name: /^done$/i })).toBeInTheDocument();

    // Starting a new edit clears the now-stale outcome banner.
    fireEvent.change(screen.getByLabelText("Genre"), {
      target: { value: "Rock" },
    });
    expect(screen.queryByText(/^Updated/)).not.toBeInTheDocument();
    // ...and the button reverts to "Cancel" now that there are edits to discard.
    expect(
      screen.getByRole("button", { name: /^cancel$/i }),
    ).toBeInTheDocument();
  });

  it("omits the tag count when nothing was written and there are no failures", async () => {
    const post = vi.spyOn(client, "POST");
    post.mockResolvedValueOnce(
      ok({
        changed_fields: ["title"],
        album_before: { title: "In Rainbows" },
        album_after: { title: "In Rainbows (R)" },
        tracks: [],
        move_enabled: false,
        move_plan: [],
        move_refusals: [],
      }),
    );
    post.mockResolvedValueOnce(
      ok({
        album,
        items: [
          {
            item_id: 1,
            track: 1,
            title: "15 Step",
            written: false,
            moved: false,
            error: null,
          },
        ],
        write_failures: 0,
        move_failures: 0,
      }),
    );

    renderPanel();
    fireEvent.change(screen.getByLabelText(/album title/i), {
      target: { value: "In Rainbows (R)" },
    });
    fireEvent.click(screen.getByRole("button", { name: /preview/i }));
    await screen.findByRole("region", { name: /pending changes/i });
    fireEvent.click(screen.getByRole("button", { name: /apply/i }));

    const status = await screen.findByRole("status");
    expect(status).toHaveTextContent(/^Updated$/);
    expect(status).not.toHaveTextContent(/wrote 0 tags/i);
  });

  it("counts the playlists an apply re-exported, after the tag and file counts", async () => {
    const post = vi.spyOn(client, "POST");
    post.mockResolvedValueOnce(
      ok({
        changed_fields: ["title"],
        album_before: { title: "In Rainbows" },
        album_after: { title: "In Rainbows (R)" },
        tracks: [],
        move_enabled: true,
        move_plan: [],
        move_refusals: [],
      }),
    );
    post.mockResolvedValueOnce(
      ok({
        album,
        items: [
          {
            item_id: 1,
            track: 1,
            title: "15 Step",
            written: true,
            moved: true,
            error: null,
          },
        ],
        write_failures: 0,
        move_failures: 0,
        playlists_reexported: 3,
      }),
    );

    renderPanel();
    fireEvent.change(screen.getByLabelText(/album title/i), {
      target: { value: "In Rainbows (R)" },
    });
    fireEvent.click(screen.getByRole("button", { name: /preview/i }));
    await screen.findByRole("region", { name: /pending changes/i });
    fireEvent.click(screen.getByRole("button", { name: /apply/i }));

    // Whole line, anchored: the wording, the order, the " · " separators AND
    // the absence of trailing junk are all pinned — moving files is what
    // re-exports playlists, so it reads last.
    const status = await screen.findByRole("status");
    expect(status).toHaveTextContent(
      /^Updated · wrote 1 tag · moved 1 file · re-exported 3 playlists$/,
    );
  });

  it("keeps the re-export count singular for one playlist", async () => {
    const post = vi.spyOn(client, "POST");
    post.mockResolvedValueOnce(
      ok({
        changed_fields: ["title"],
        album_before: { title: "In Rainbows" },
        album_after: { title: "In Rainbows (R)" },
        tracks: [],
        move_enabled: true,
        move_plan: [],
        move_refusals: [],
      }),
    );
    post.mockResolvedValueOnce(
      ok({
        album,
        items: [
          {
            item_id: 1,
            track: 1,
            title: "15 Step",
            written: false,
            moved: true,
            error: null,
          },
        ],
        write_failures: 0,
        move_failures: 0,
        playlists_reexported: 1,
      }),
    );

    renderPanel();
    fireEvent.change(screen.getByLabelText(/album title/i), {
      target: { value: "In Rainbows (R)" },
    });
    fireEvent.click(screen.getByRole("button", { name: /preview/i }));
    await screen.findByRole("region", { name: /pending changes/i });
    fireEvent.click(screen.getByRole("button", { name: /apply/i }));

    const status = await screen.findByRole("status");
    expect(status).toHaveTextContent("Updated · moved 1 file · re-exported 1 playlist");
  });

  it("omits the re-export count when the apply touched no playlist", async () => {
    const post = vi.spyOn(client, "POST");
    post.mockResolvedValueOnce(
      ok({
        changed_fields: ["title"],
        album_before: { title: "In Rainbows" },
        album_after: { title: "In Rainbows (R)" },
        tracks: [],
        move_enabled: false,
        move_plan: [],
        move_refusals: [],
      }),
    );
    post.mockResolvedValueOnce(
      ok({
        album,
        items: [
          {
            item_id: 1,
            track: 1,
            title: "15 Step",
            written: true,
            moved: false,
            error: null,
          },
        ],
        write_failures: 0,
        move_failures: 0,
        // An explicit zero, not an absent field: this line drops zero counts,
        // so "re-exported 0 playlists" must never reach the user.
        playlists_reexported: 0,
      }),
    );

    renderPanel();
    fireEvent.change(screen.getByLabelText(/album title/i), {
      target: { value: "In Rainbows (R)" },
    });
    fireEvent.click(screen.getByRole("button", { name: /preview/i }));
    await screen.findByRole("region", { name: /pending changes/i });
    fireEvent.click(screen.getByRole("button", { name: /apply/i }));

    const status = await screen.findByRole("status");
    expect(status).toHaveTextContent(/^Updated · wrote 1 tag$/);
    expect(status).not.toHaveTextContent(/re-exported/i);
  });

  it("disables all inputs and buttons while apply is pending", async () => {
    const post = vi.spyOn(client, "POST");
    post.mockResolvedValueOnce(
      ok({
        changed_fields: ["title"],
        album_before: { title: "In Rainbows" },
        album_after: { title: "In Rainbows (R)" },
        tracks: [],
        move_enabled: false,
        move_plan: [],
        move_refusals: [],
      }),
    );
    // Apply never resolves during the assertion window.
    let resolveApply: (v: unknown) => void = () => {};
    post.mockImplementationOnce(
      () =>
        new Promise((res) => {
          resolveApply = res;
        }) as never,
    );

    renderPanel();
    fireEvent.change(screen.getByLabelText(/album title/i), {
      target: { value: "In Rainbows (R)" },
    });
    fireEvent.click(screen.getByRole("button", { name: /preview/i }));
    await screen.findByRole("region", { name: /pending changes/i });
    fireEvent.click(screen.getByRole("button", { name: /apply/i }));

    await waitFor(() =>
      expect(screen.getByLabelText(/album title/i)).toBeDisabled(),
    );
    expect(screen.getByLabelText("Genre")).toBeDisabled();
    expect(screen.getByLabelText(/title of track 1/i)).toBeDisabled();
    expect(screen.getByRole("button", { name: /preview/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /cancel/i })).toBeDisabled();

    resolveApply(
      ok({
        album,
        items: [
          {
            item_id: 1,
            track: 1,
            title: "15 Step",
            written: true,
            moved: false,
            error: null,
          },
        ],
        write_failures: 0,
        move_failures: 0,
      }),
    );
    await waitFor(() =>
      expect(screen.getByLabelText(/album title/i)).not.toBeDisabled(),
    );
  });

  it("builds a preview request that omits a live-added track absent from the draft", async () => {
    const post = vi.spyOn(client, "POST").mockResolvedValue(
      ok({
        changed_fields: [],
        album_before: {},
        album_after: {},
        tracks: [],
        move_enabled: false,
        move_plan: [],
        move_refusals: [],
      }),
    );

    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const { rerender } = render(
      <QueryClientProvider client={qc}>
        <AlbumEditPanel album={album} onClose={() => {}} />
      </QueryClientProvider>,
    );

    // A real user edit to a track the seeded draft DOES know about.
    fireEvent.change(screen.getByLabelText(/title of track 1/i), {
      target: { value: "16 Step" },
    });

    // An SSE library:changed refetch adds item id 3 that the once-seeded draft
    // has no entry for.
    const withExtra = {
      ...album,
      tracks: [
        ...album.tracks,
        {
          id: 3,
          title: "Nude",
          track: 3,
          disc: 1,
          duration_seconds: 200,
          artist: "Radiohead",
        },
      ],
    } as unknown as AlbumDetail;
    rerender(
      <QueryClientProvider client={qc}>
        <AlbumEditPanel album={withExtra} onClose={() => {}} />
      </QueryClientProvider>,
    );

    // Preview must not throw on the stale draft: buildRequest dereferencing the
    // missing id-3 entry would crash the click handler and silently no-op.
    fireEvent.click(screen.getByRole("button", { name: /preview/i }));

    await waitFor(() => expect(post).toHaveBeenCalled());
    const body = (
      post.mock.calls[0][1] as { body: { tracks: { item_id: number }[] } }
    ).body;
    // The known edit survives; the un-drafted live-added track is omitted.
    expect(body.tracks).toEqual([{ item_id: 1, title: "16 Step" }]);
    expect(body.tracks.some((t) => t.item_id === 3)).toBe(false);
  });

  it("does not crash when a live refetch adds a track not in the seeded draft", () => {
    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const { rerender } = render(
      <QueryClientProvider client={qc}>
        <AlbumEditPanel album={album} onClose={() => {}} />
      </QueryClientProvider>,
    );
    // The seeded rows render.
    expect(screen.getByLabelText(/title of track 1/i)).toBeInTheDocument();

    // An SSE library:changed refetch changes THIS album's track membership: a
    // new item id (3) appears that the once-seeded draft has no entry for.
    const withExtra = {
      ...album,
      tracks: [
        ...album.tracks,
        {
          id: 3,
          title: "Nude",
          track: 3,
          disc: 1,
          duration_seconds: 200,
          artist: "Radiohead",
        },
      ],
    } as unknown as AlbumDetail;
    expect(() =>
      rerender(
        <QueryClientProvider client={qc}>
          <AlbumEditPanel album={withExtra} onClose={() => {}} />
        </QueryClientProvider>,
      ),
    ).not.toThrow();

    // The un-drafted row is skipped until the draft reseeds — the seeded rows
    // stay editable and nothing throws.
    expect(screen.getByLabelText(/title of track 1/i)).toBeInTheDocument();
    expect(
      screen.queryByLabelText(/title of track 3/i),
    ).not.toBeInTheDocument();
  });
});
