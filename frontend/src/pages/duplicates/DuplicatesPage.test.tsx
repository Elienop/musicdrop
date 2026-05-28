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

  test("a 409 surfaces the inline can't-resolve message", async () => {
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
    expect(await screen.findByText(/can't resolve right now/i)).toBeInTheDocument();
  });
});
