import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, test } from "vitest";

import type { components } from "@/api/schema";
import { DuplicatesPage } from "@/pages/duplicates/DuplicatesPage";
import { renderWithProviders } from "@/test/render";
import { server } from "@/test/msw-server";

type DuplicatesReport = components["schemas"]["DuplicatesReport"];
type DuplicateAlbum = components["schemas"]["DuplicateAlbum"];

const DUP_URL = `${window.location.origin}/api/duplicates`;
const RESOLVE_URL = `${window.location.origin}/api/duplicates/resolve`;

function album(overrides: Partial<DuplicateAlbum> = {}): DuplicateAlbum {
  return {
    id: 1,
    album_artist: "Radiohead",
    title: "In Rainbows",
    year: 2007,
    track_count: 10,
    genre: null,
    mb_albumid: null,
    format: "FLAC",
    bitrate_kbps: 900,
    folder: "/music/Radiohead/In Rainbows",
    is_suggested_keeper: true,
    ...overrides,
  };
}

function reportWithOneGroup(): DuplicatesReport {
  return {
    mode: "strict",
    group_count: 1,
    album_count: 2,
    groups: [
      {
        match_reason: "MusicBrainz album id",
        suggested_keeper_id: 1,
        members: [
          album({ id: 1, track_count: 10, format: "FLAC", is_suggested_keeper: true }),
          album({
            id: 2,
            track_count: 9,
            format: "MP3",
            bitrate_kbps: 320,
            folder: "/music/Radiohead/In Rainbows (1)",
            is_suggested_keeper: false,
          }),
        ],
      },
    ],
  };
}

const RESOLVE_ALL_URL = `${window.location.origin}/api/duplicates/resolve-all`;

function reportWithTwoGroups(): DuplicatesReport {
  return {
    mode: "strict",
    group_count: 2,
    album_count: 4,
    groups: [
      reportWithOneGroup().groups[0], // Radiohead / In Rainbows (ids 1,2)
      {
        match_reason: "MusicBrainz album id",
        suggested_keeper_id: 3,
        members: [
          album({ id: 3, album_artist: "Daft Punk", title: "Discovery", track_count: 14, is_suggested_keeper: true, folder: "/music/Daft Punk/Discovery" }),
          album({ id: 4, album_artist: "Daft Punk", title: "Discovery", track_count: 12, is_suggested_keeper: false, format: "MP3", bitrate_kbps: 320, folder: "/music/Daft Punk/Discovery (1)" }),
        ],
      },
    ],
  };
}

function renderPage() {
  return renderWithProviders(<DuplicatesPage />, { route: "/duplicates", path: "/duplicates" });
}

describe("DuplicatesPage", () => {
  test("renders a duplicate group with its members", async () => {
    server.use(http.get(DUP_URL, () => HttpResponse.json(reportWithOneGroup())));
    renderPage();
    expect(await screen.findByText(/Matched on/i)).toBeInTheDocument();
    expect(screen.getByText("most complete")).toBeInTheDocument();
    expect(screen.getAllByText("In Rainbows").length).toBe(2);
  });

  test("empty report shows the clean-library state", async () => {
    server.use(
      http.get(DUP_URL, () => HttpResponse.json({ mode: "strict", group_count: 0, album_count: 0, groups: [] })),
    );
    renderPage();
    expect(await screen.findByText(/No duplicate albums found/i)).toBeInTheDocument();
  });

  test("toggling to fuzzy refetches with mode=fuzzy", async () => {
    const seen: string[] = [];
    server.use(
      http.get(DUP_URL, ({ request }) => {
        seen.push(new URL(request.url).searchParams.get("mode") ?? "");
        return HttpResponse.json({ mode: "fuzzy", group_count: 0, album_count: 0, groups: [] });
      }),
    );
    const user = userEvent.setup();
    renderPage();
    await screen.findByText(/No duplicate albums found/i);
    await user.click(screen.getByRole("button", { name: /fuzzy/i }));
    await waitFor(() => expect(seen).toContain("fuzzy"));
  });

  test("resolve confirm posts keep + remove ids and clears the group", async () => {
    let body: unknown = null;
    let getCalls = 0;
    server.use(
      http.get(DUP_URL, () => {
        getCalls += 1;
        return HttpResponse.json(
          getCalls === 1
            ? reportWithOneGroup()
            : { mode: "strict", group_count: 0, album_count: 0, groups: [] },
        );
      }),
      http.post(RESOLVE_URL, async ({ request }) => {
        body = await request.json();
        return HttpResponse.json({ kept_album_id: 1, moved: [{ id: 2, album_artist: "Radiohead", title: "In Rainbows", trash_path: "/t" }] });
      }),
    );
    const user = userEvent.setup();
    renderPage();
    await screen.findByText(/Matched on/i);

    await user.click(screen.getByRole("button", { name: /move 1 to trash/i }));
    const dialog = await screen.findByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: /move to trash/i }));

    await waitFor(() => expect(body).toEqual({ mode: "strict", keep_album_id: 1, remove_album_ids: [2] }));
    await waitFor(() => expect(screen.queryByText(/Matched on/i)).not.toBeInTheDocument());
  });

  test("an import-active 409 surfaces the 'import running' message", async () => {
    server.use(
      http.get(DUP_URL, () => HttpResponse.json(reportWithOneGroup())),
      http.post(RESOLVE_URL, () => HttpResponse.json({ detail: "Import in progress" }, { status: 409 })),
    );
    const user = userEvent.setup();
    renderPage();
    await screen.findByText(/Matched on/i);
    await user.click(screen.getByRole("button", { name: /move 1 to trash/i }));
    const dialog = await screen.findByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: /move to trash/i }));
    expect(
      await screen.findByText(/can't resolve while an import is running/i),
    ).toBeInTheDocument();
  });

  test("a stale-group 409 shows the 'group changed' message and refetches the report", async () => {
    let getCalls = 0;
    server.use(
      http.get(DUP_URL, () => {
        getCalls += 1;
        return HttpResponse.json(reportWithOneGroup());
      }),
      http.post(RESOLVE_URL, () =>
        HttpResponse.json({ detail: "duplicate group membership changed" }, { status: 409 }),
      ),
    );
    const user = userEvent.setup();
    renderPage();
    await screen.findByText(/Matched on/i);
    const callsBeforeResolve = getCalls;
    await user.click(screen.getByRole("button", { name: /move 1 to trash/i }));
    const dialog = await screen.findByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: /move to trash/i }));
    expect(await screen.findByText(/this group changed/i)).toBeInTheDocument();
    // 409 invalidates ["duplicates"] so the report self-heals: at least one
    // extra GET fires after the failed resolve.
    await waitFor(() => expect(getCalls).toBeGreaterThan(callsBeforeResolve));
  });

  test("the Resolve-all button shows the total copy count (only with 2+ groups)", async () => {
    server.use(http.get(DUP_URL, () => HttpResponse.json(reportWithTwoGroups())));
    renderPage();
    expect(
      await screen.findByRole("button", { name: /resolve all · 2 copies/i }),
    ).toBeInTheDocument();
  });

  test("no Resolve-all button with a single group", async () => {
    server.use(http.get(DUP_URL, () => HttpResponse.json(reportWithOneGroup())));
    renderPage();
    await screen.findByText(/Matched on/i);
    expect(screen.queryByRole("button", { name: /resolve all/i })).not.toBeInTheDocument();
  });

  test("Resolve all posts every group's decision (honoring an override) and clears them", async () => {
    let body: unknown = null;
    let getCalls = 0;
    server.use(
      http.get(DUP_URL, () => {
        getCalls += 1;
        return HttpResponse.json(
          getCalls === 1
            ? reportWithTwoGroups()
            : { mode: "strict", group_count: 0, album_count: 0, groups: [] },
        );
      }),
      http.post(RESOLVE_ALL_URL, async ({ request }) => {
        body = await request.json();
        return HttpResponse.json({
          resolved: [
            { kept_album_id: 1, moved: [{ id: 2, album_artist: "Radiohead", title: "In Rainbows", trash_path: "/t" }] },
            { kept_album_id: 4, moved: [{ id: 3, album_artist: "Daft Punk", title: "Discovery", trash_path: "/t" }] },
          ],
          skipped_stale: [],
          group_count: 2,
          moved_count: 2,
        });
      }),
    );
    const user = userEvent.setup();
    renderPage();
    // Two groups → two "Matched on" rows, so wait on findAllByText.
    await screen.findAllByText(/Matched on/i);

    // Override group 2's keeper: pick the 12-track Daft Punk copy (id 4).
    await user.click(screen.getByRole("radio", { name: /Keep Discovery \(12 tracks\)/i }));
    await user.click(screen.getByRole("button", { name: /resolve all · 2 copies/i }));
    const dialog = await screen.findByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: /move all to trash/i }));

    await waitFor(() =>
      expect(body).toEqual({
        mode: "strict",
        groups: [
          { keep_album_id: 1, remove_album_ids: [2] },
          { keep_album_id: 4, remove_album_ids: [3] },
        ],
      }),
    );
    await waitFor(() => expect(screen.queryByText(/Matched on/i)).not.toBeInTheDocument());
  });

  test("the summary flags groups the server skipped", async () => {
    server.use(
      http.get(DUP_URL, () => HttpResponse.json(reportWithTwoGroups())),
      http.post(RESOLVE_ALL_URL, () =>
        HttpResponse.json({
          resolved: [{ kept_album_id: 1, moved: [{ id: 2, album_artist: "Radiohead", title: "In Rainbows", trash_path: "/t" }] }],
          skipped_stale: [{ keep_album_id: 3, reason: "duplicate group membership changed" }],
          group_count: 1,
          moved_count: 1,
        }),
      ),
    );
    const user = userEvent.setup();
    renderPage();
    // Two groups → two "Matched on" rows, so wait on findAllByText.
    await screen.findAllByText(/Matched on/i);
    await user.click(screen.getByRole("button", { name: /resolve all/i }));
    const dialog = await screen.findByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: /move all to trash/i }));
    expect(await screen.findByText(/1 group changed and was skipped/i)).toBeInTheDocument();
  });

  test("an all-skipped batch leads with the skip notice, not 'moved 0'", async () => {
    // Every group drifted since the scan: a normal 200 with nothing moved. The
    // summary must surface the actionable skip, not read as a no-op success.
    server.use(
      http.get(DUP_URL, () => HttpResponse.json(reportWithTwoGroups())),
      http.post(RESOLVE_ALL_URL, () =>
        HttpResponse.json({
          resolved: [],
          skipped_stale: [
            { keep_album_id: 1, reason: "duplicate group membership changed" },
            { keep_album_id: 3, reason: "duplicate group membership changed" },
          ],
          group_count: 0,
          moved_count: 0,
        }),
      ),
    );
    const user = userEvent.setup();
    renderPage();
    await screen.findAllByText(/Matched on/i);
    await user.click(screen.getByRole("button", { name: /resolve all/i }));
    const dialog = await screen.findByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: /move all to trash/i }));
    expect(await screen.findByText(/nothing moved/i)).toBeInTheDocument();
    expect(screen.queryByText(/moved 0/i)).not.toBeInTheDocument();
  });

  test("renders the page h1 + live count meta via PageHeader", async () => {
    server.use(http.get(DUP_URL, () => HttpResponse.json(reportWithOneGroup())));
    renderPage();

    const h1 = await screen.findByRole("heading", { level: 1, name: "Duplicates" });
    expect(h1).toHaveAttribute("tabindex", "-1");
    expect(await screen.findByText("1 group · 2 albums")).toBeInTheDocument();
  });

  test("the mode control is the shared SegmentedControl (accent active, not inverted)", async () => {
    server.use(http.get(DUP_URL, () => HttpResponse.json(reportWithOneGroup())));
    renderPage();
    await screen.findByText(/Matched on/i);

    const strict = screen.getByRole("button", { name: /strict/i });
    expect(strict).toHaveAttribute("aria-pressed", "true");
    // The old ModeToggle's inverted bg-foreground active style is gone.
    expect(strict).not.toHaveClass("bg-foreground");
    expect(strict.closest('[data-slot="segmented-control"]')).not.toBeNull();
  });

  test("member rows render on AlbumRow with the cover thumb", async () => {
    server.use(http.get(DUP_URL, () => HttpResponse.json(reportWithOneGroup())));
    renderPage();
    await screen.findByText(/Matched on/i);

    // One cover per member, served from the album cover endpoint.
    const covers = document.querySelectorAll('img[data-slot="cover-art"]');
    expect(covers).toHaveLength(2);
    // ?size=thumb: AlbumRow renders these at size-10 (40 CSS px), so the
    // 320px WebP derivation is already 4x what the box needs at 2x DPI.
    expect(covers[0].getAttribute("src")).toBe("/api/albums/1/cover?size=thumb");
    // The quality/meta line folded into the row.
    expect(screen.getByText(/2007 · 10 tracks · FLAC · 900k/)).toBeInTheDocument();
  });

  test("a failed scan renders the shared inline ErrorState", async () => {
    server.use(http.get(DUP_URL, () => new HttpResponse(null, { status: 500 })));
    renderPage();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveAttribute("data-slot", "error-state");
    expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument();
  });

  test("the clean-library state renders the shared EmptyState", async () => {
    server.use(
      http.get(DUP_URL, () =>
        HttpResponse.json({ mode: "strict", group_count: 0, album_count: 0, groups: [] }),
      ),
    );
    renderPage();

    await screen.findByText(/No duplicate albums found/i);
    expect(document.querySelector('[data-slot="empty-state"]')).not.toBeNull();
  });
});
