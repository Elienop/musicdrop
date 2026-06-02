import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
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
    { id: 1, title: "15 Step", track: 1, disc: 1, duration_seconds: 230, artist: "Radiohead" },
    { id: 2, title: "Bodysnatchers", track: 2, disc: 1, duration_seconds: 242, artist: "Radiohead" },
  ],
} as unknown as AlbumDetail;

/** Wrap mock data as a 2xx openapi-fetch result (the hooks read `response.ok`). */
function ok(data: unknown) {
  return { data, error: undefined, response: { ok: true, status: 200 } } as never;
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
      }),
    );

    renderPanel();
    fireEvent.change(screen.getByLabelText(/album title/i), {
      target: { value: "In Rainbows (R)" },
    });
    fireEvent.click(screen.getByRole("button", { name: /preview/i }));

    const diff = await screen.findByRole("region", { name: /pending changes/i });
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
        move_plan: [{ item_id: 1, track: 1, old_path: "/a/x.flac", new_path: "/b/x.flac" }],
      }),
    );

    renderPanel();
    fireEvent.change(screen.getByLabelText(/album artist/i), {
      target: { value: "Radiohead (Live)" },
    });
    fireEvent.click(screen.getByRole("button", { name: /preview/i }));
    await waitFor(() => expect(screen.getByText(/1 file will be moved/i)).toBeInTheDocument());
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
      }),
    );

    renderPanel();
    fireEvent.change(screen.getByLabelText(/title of track 1/i), {
      target: { value: "16 Step" },
    });
    fireEvent.click(screen.getByRole("button", { name: /preview/i }));

    const diff = await screen.findByRole("region", { name: /pending changes/i });
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
      }),
    );

    renderPanel();
    fireEvent.click(screen.getByRole("button", { name: /preview/i }));
    await waitFor(() => expect(screen.getByText(/no changes/i)).toBeInTheDocument());
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
    expect(screen.queryByRole("region", { name: /pending changes/i })).not.toBeInTheDocument();
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
      }),
    );
    post.mockResolvedValueOnce(
      ok({
        album,
        items: [
          { item_id: 1, track: 1, title: "15 Step", written: true, moved: false, error: null },
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

    await waitFor(() => expect(screen.getByText(/wrote 1 tag/i)).toBeInTheDocument());
    // The failed track is listed with its error, not hidden.
    expect(screen.getByText(/permission denied/i)).toBeInTheDocument();
    expect(screen.getByText(/Bodysnatchers/i)).toBeInTheDocument();
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
      }),
    );
    post.mockResolvedValueOnce(
      ok({
        album,
        items: [
          { item_id: 1, track: 1, title: "15 Step", written: true, moved: false, error: null },
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
    await waitFor(() => expect(screen.getByText(/^Updated/)).toBeInTheDocument());

    // Starting a new edit clears the now-stale outcome banner.
    fireEvent.change(screen.getByLabelText("Genre"), { target: { value: "Rock" } });
    expect(screen.queryByText(/^Updated/)).not.toBeInTheDocument();
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
      }),
    );
    post.mockResolvedValueOnce(
      ok({
        album,
        items: [
          { item_id: 1, track: 1, title: "15 Step", written: false, moved: false, error: null },
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
      }),
    );
    // Apply never resolves during the assertion window.
    let resolveApply: (v: unknown) => void = () => {};
    post.mockImplementationOnce(
      () => new Promise((res) => { resolveApply = res; }) as never,
    );

    renderPanel();
    fireEvent.change(screen.getByLabelText(/album title/i), {
      target: { value: "In Rainbows (R)" },
    });
    fireEvent.click(screen.getByRole("button", { name: /preview/i }));
    await screen.findByRole("region", { name: /pending changes/i });
    fireEvent.click(screen.getByRole("button", { name: /apply/i }));

    await waitFor(() => expect(screen.getByLabelText(/album title/i)).toBeDisabled());
    expect(screen.getByLabelText("Genre")).toBeDisabled();
    expect(screen.getByLabelText(/title of track 1/i)).toBeDisabled();
    expect(screen.getByRole("button", { name: /preview/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /cancel/i })).toBeDisabled();

    resolveApply(
      ok({
        album,
        items: [{ item_id: 1, track: 1, title: "15 Step", written: true, moved: false, error: null }],
        write_failures: 0,
        move_failures: 0,
      }),
    );
    await waitFor(() => expect(screen.getByLabelText(/album title/i)).not.toBeDisabled());
  });
});
