import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, test } from "vitest";

import { SettingsTrashPage } from "@/pages/settings/SettingsTrashPage";
import { server } from "@/test/msw-server";

const TRASH_URL = `${window.location.origin}/api/trash`;
const RESTORE_URL = `${window.location.origin}/api/trash/restore`;
const ALL_URL = `${window.location.origin}/api/trash/all`;

const album = {
  folder: "2 Brothers - Dreams",
  album_artist: "2 Brothers",
  album: "Dreams",
  year: 1994,
  track_count: 2,
  format: "FLAC",
};

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <SettingsTrashPage />
    </QueryClientProvider>,
  );
}

describe("SettingsTrashPage", () => {
  test("lists trashed albums with their meta", async () => {
    server.use(
      http.get(TRASH_URL, () =>
        HttpResponse.json({ albums: [album], trash_path: "/data/beets/trash" }),
      ),
    );
    renderPage();
    expect(await screen.findByText(/2 Brothers - Dreams/)).toBeInTheDocument();
    expect(screen.getByText("2 tracks · FLAC · 1994")).toBeInTheDocument();
    expect(screen.getByText("/data/beets/trash")).toBeInTheDocument();
  });

  test("shows an empty state when Trash is empty", async () => {
    server.use(http.get(TRASH_URL, () => HttpResponse.json({ albums: [], trash_path: "/t" })));
    renderPage();
    expect(await screen.findByText(/Trash is empty/i)).toBeInTheDocument();
  });

  test("Empty confirms then DELETEs that folder", async () => {
    let deleted: string | null = null;
    server.use(
      http.get(TRASH_URL, () => HttpResponse.json({ albums: [album], trash_path: "/t" })),
      http.delete(TRASH_URL, ({ request }) => {
        deleted = new URL(request.url).searchParams.get("folder");
        return HttpResponse.json({ removed: 1 });
      }),
    );
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: /Empty Dreams/i }));
    const dialog = await screen.findByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: "Delete" }));

    await waitFor(() => expect(deleted).toBe("2 Brothers - Dreams"));
  });

  test("Empty all DELETEs /api/trash/all", async () => {
    let called = false;
    server.use(
      http.get(TRASH_URL, () => HttpResponse.json({ albums: [album], trash_path: "/t" })),
      http.delete(ALL_URL, () => {
        called = true;
        return HttpResponse.json({ removed: 1 });
      }),
    );
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Empty all" }));
    const dialog = await screen.findByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: "Empty all" }));

    await waitFor(() => expect(called).toBe(true));
  });

  test("Restore posts the folder and shows the restored result", async () => {
    let posted: unknown = null;
    server.use(
      http.get(TRASH_URL, () => HttpResponse.json({ albums: [album], trash_path: "/t" })),
      http.post(RESTORE_URL, async ({ request }) => {
        posted = await request.json();
        return HttpResponse.json({ restored: true, reason: "restored", album_id: 7 });
      }),
    );
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: /^Restore$/ }));
    await waitFor(() => expect(posted).toEqual({ folder: "2 Brothers - Dreams" }));
    expect(await screen.findByText(/Restored to your library/i)).toBeInTheDocument();
  });

  test("a zero-track row keeps Restore enabled and explains the uncertainty", async () => {
    // track_count === 0 means "nothing here produced a readable media Item",
    // NOT "no audio" (see the comment in trash_manage.py) — beets' importer
    // reads more than Item.from_path does, so Restore may genuinely work. The
    // row must say so without taking the button away.
    const emptyAlbum = {
      folder: "No Audio - Ghost",
      album_artist: null,
      album: null,
      year: null,
      track_count: 0,
      format: null,
    };
    server.use(
      http.get(TRASH_URL, () =>
        HttpResponse.json({ albums: [emptyAlbum], trash_path: "/t" }),
      ),
    );
    renderPage();

    const restore = await screen.findByRole("button", { name: /^Restore$/ });
    expect(restore).toBeEnabled();
    expect(
      await screen.findByText(/couldn.t read audio tags here/i),
    ).toBeInTheDocument();
    expect(restore).toHaveAccessibleDescription(/restore may still work/i);
  });

  test("Restore surfaces the already-in-library result", async () => {
    server.use(
      http.get(TRASH_URL, () => HttpResponse.json({ albums: [album], trash_path: "/t" })),
      http.post(RESTORE_URL, () =>
        HttpResponse.json({ restored: false, reason: "already_in_library", album_id: null }),
      ),
    );
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: /^Restore$/ }));
    expect(await screen.findByText(/Already in your library/i)).toBeInTheDocument();
  });
});
