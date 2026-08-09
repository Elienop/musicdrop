import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, test } from "vitest";

import { BrowsePage } from "@/pages/browse/BrowsePage";
import { renderWithProviders } from "@/test/render";
import { server } from "@/test/msw-server";

const O = window.location.origin;
const FACETS = `${O}/api/browse/facets`;
const ALBUMS = `${O}/api/browse/albums`;

let lastQuery: URLSearchParams = new URLSearchParams();

function album(id: number, title: string) {
  return {
    id,
    album_artist: "An Artist",
    title,
    year: 2015,
    track_count: 10,
    genre: "Rock",
    mb_albumid: null,
  };
}

const FACETS_BODY = {
  genres: [
    { value: "Rock", count: 2 },
    { value: "Pop", count: 1 },
  ],
  decades: [{ value: "2010s", count: 3 }],
  formats: [{ value: "FLAC", count: 3 }],
  album_types: [
    { value: "album", count: 2 },
    { value: "single", count: 1 },
  ],
  sources: [{ value: "MusicBrainz", count: 3 }],
  media: [{ value: "CD", count: 3 }],
  countries: [{ value: "US", count: 3 }],
  lyrics: [{ value: "Missing", count: 3 }],
  tracks: [{ value: "Incomplete", count: 2 }],
};

describe("BrowsePage", () => {
  beforeEach(() => {
    localStorage.clear();
    lastQuery = new URLSearchParams();
    server.use(
      http.get(FACETS, () => HttpResponse.json(FACETS_BODY)),
      http.get(ALBUMS, ({ request }) => {
        lastQuery = new URL(request.url).searchParams;
        return HttpResponse.json({
          items: [album(1, "First"), album(2, "Second")],
          total: 2,
          limit: 48,
          offset: 0,
        });
      }),
    );
  });

  test("renders facet groups with values + counts", async () => {
    renderWithProviders(<BrowsePage />, { route: "/browse" });
    // Fieldsets render once the facets load.
    const genre = await screen.findByRole("group", { name: "Genre" });
    expect(within(genre).getByText("Rock")).toBeInTheDocument();
    expect(within(genre).getByText("2")).toBeInTheDocument(); // Rock count
    expect(screen.getByRole("group", { name: "Decade" })).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "Format" })).toBeInTheDocument();
  });

  test("filter rail is its own always-in-viewport scroll box under the sticky topbar", async () => {
    renderWithProviders(<BrowsePage />, { route: "/browse" });
    const rail = await screen.findByRole("complementary", { name: /filters/i });
    // jsdom has no real scroll geometry, so lock the class contract instead:
    // the rail must be an internal scroller (overflow-y-auto) pinned BELOW the
    // 4.5rem sticky topbar (top-24 = 6rem = topbar + main py-6) and sized to
    // always fit the viewport (100vh - 6rem top - 1.5rem bottom gap).
    expect(rail.className).toContain("overflow-y-auto");
    expect(rail.className).toContain("md:sticky");
    expect(rail.className).toContain("md:top-24");
    expect(rail.className).toContain("md:max-h-[calc(100vh-7.5rem)]");
    // A real right gutter — content, then empty space, then a slim themed
    // bar — so the scrollbar never crowds the facet counts.
    expect(rail.className).toContain("pr-4");
    expect(rail.className).toContain("thin-scrollbar");
    expect(rail.className).toContain("md:w-60");
  });

  test("toggling a genre puts it in the query and refetches", async () => {
    renderWithProviders(<BrowsePage />, { route: "/browse" });
    const rock = await screen.findByRole("checkbox", { name: /rock/i });
    await userEvent.click(rock);
    await waitFor(() => expect(lastQuery.getAll("genre")).toEqual(["Rock"]));
  });

  test("an active filter chip removes its value", async () => {
    renderWithProviders(<BrowsePage />, { route: "/browse?genre=Rock" });
    // The chip is a button labelled with the value + a remove hint.
    const chip = await screen.findByRole("button", { name: /remove rock filter/i });
    await userEvent.click(chip);
    await waitFor(() => expect(lastQuery.getAll("genre")).toEqual([]));
  });

  test("Clear all drops every filter", async () => {
    renderWithProviders(<BrowsePage />, { route: "/browse?genre=Rock&decade=2010s" });
    await userEvent.click(await screen.findByRole("button", { name: /clear all/i }));
    await waitFor(() => {
      expect(lastQuery.getAll("genre")).toEqual([]);
      expect(lastQuery.getAll("decade")).toEqual([]);
    });
  });

  test("shows an empty state when no albums match", async () => {
    server.use(
      http.get(ALBUMS, () =>
        HttpResponse.json({ items: [], total: 0, limit: 48, offset: 0 }),
      ),
    );
    renderWithProviders(<BrowsePage />, { route: "/browse?genre=Disco" });
    expect(
      await screen.findByText(/no albums match these filters/i),
    ).toBeInTheDocument();
  });

  test("shows an error state with retry when albums fail to load", async () => {
    server.use(
      http.get(ALBUMS, () => new HttpResponse(null, { status: 500 })),
    );
    renderWithProviders(<BrowsePage />, { route: "/browse" });
    expect(await screen.findByText(/couldn.t load albums/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument();
  });

  test("offers a way back when the offset is past the end (total > 0, empty page)", async () => {
    server.use(
      http.get(ALBUMS, () =>
        HttpResponse.json({ items: [], total: 60, limit: 48, offset: 480 }),
      ),
    );
    renderWithProviders(<BrowsePage />, { route: "/browse?offset=480" });
    expect(await screen.findByText(/this page is empty/i)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /back to first page/i }));
    await waitFor(() => expect(lastQuery.get("offset")).toBeNull());
  });

  test("paginates when the total exceeds a page", async () => {
    server.use(
      http.get(ALBUMS, ({ request }) => {
        lastQuery = new URL(request.url).searchParams;
        return HttpResponse.json({
          items: [album(1, "First")],
          total: 60,
          limit: 48,
          offset: Number(new URL(request.url).searchParams.get("offset")) || 0,
        });
      }),
    );
    renderWithProviders(<BrowsePage />, { route: "/browse" });
    await userEvent.click(await screen.findByRole("button", { name: "Next" }));
    await waitFor(() => expect(lastQuery.get("offset")).toBe("48"));
  });

  test("renders the Browse h1 with the count meta line", async () => {
    renderWithProviders(<BrowsePage />, { route: "/browse" });

    expect(
      await screen.findByRole("heading", { level: 1, name: "Browse" }),
    ).toBeInTheDocument();
    const count = await screen.findByText(/2 albums in your library\./);
    expect(count.closest("[aria-live]")).toHaveAttribute(
      "aria-live",
      "polite",
    );
  });

  test("page change scrolls plainly and focuses the count line", async () => {
    server.use(
      http.get(ALBUMS, ({ request }) => {
        lastQuery = new URL(request.url).searchParams;
        return HttpResponse.json({
          items: [album(1, "First")],
          total: 60,
          limit: 48,
          offset: Number(new URL(request.url).searchParams.get("offset")) || 0,
        });
      }),
    );
    renderWithProviders(<BrowsePage />, { route: "/browse" });

    await userEvent.click(await screen.findByRole("button", { name: "Next" }));

    await waitFor(() => expect(lastQuery.get("offset")).toBe("48"));
    expect(screen.getByText(/60 albums in your library\./)).toHaveFocus();
    expect(window.scrollTo).toHaveBeenCalledWith({ top: 0 });
  });

  test("the unfiltered empty library offers the import CTA", async () => {
    server.use(
      http.get(ALBUMS, () =>
        HttpResponse.json({ items: [], total: 0, limit: 48, offset: 0 }),
      ),
    );
    renderWithProviders(<BrowsePage />, { route: "/browse" });

    expect(
      await screen.findByText(/no albums in the library yet/i),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: /add music from a folder/i }),
    ).toHaveAttribute("href", "/import");
  });

  test("renders all eight facet groups with their own values", async () => {
    renderWithProviders(<BrowsePage />, { route: "/browse" });
    // Value-pin each group: a facetKey swap between two rows renders the wrong
    // values under a heading, which the label-only check can't see (and the
    // same-type swap is invisible to the typechecker too).
    const expected = [
      ["Genre", "Rock"],
      ["Decade", "2010s"],
      ["Format", "FLAC"],
      ["Type", "single"],
      ["Media", "CD"],
      ["Country", "US"],
      ["Source", "MusicBrainz"],
      ["Lyrics", "Missing"],
    ] as const;
    for (const [name, value] of expected) {
      const group = await screen.findByRole("group", { name });
      expect(within(group).getByText(value)).toBeInTheDocument();
    }
  });

  test("toggling a type checkbox puts album_type in the query", async () => {
    renderWithProviders(<BrowsePage />, { route: "/browse" });
    await userEvent.click(await screen.findByRole("checkbox", { name: /single/i }));
    await waitFor(() =>
      expect(lastQuery.getAll("album_type")).toEqual(["single"]),
    );
  });

  test("renders the Tracks facet group and filters by it", async () => {
    renderWithProviders(<BrowsePage />, { route: "/browse" });
    const group = await screen.findByRole("group", { name: "Tracks" });
    await userEvent.click(
      within(group).getByRole("checkbox", { name: /incomplete/i }),
    );
    await waitFor(() =>
      expect(lastQuery.getAll("tracks")).toEqual(["Incomplete"]),
    );
  });

  test("a long facet group collapses to 8 with a Show all toggle", async () => {
    const many = Array.from({ length: 10 }, (_, i) => ({
      value: `G${i}`,
      count: 1,
    }));
    server.use(
      http.get(FACETS, () =>
        HttpResponse.json({ ...FACETS_BODY, genres: many }),
      ),
    );
    renderWithProviders(<BrowsePage />, { route: "/browse" });
    const genre = await screen.findByRole("group", { name: "Genre" });
    expect(within(genre).getByText("G7")).toBeInTheDocument();
    expect(within(genre).queryByText("G8")).not.toBeInTheDocument();
    await userEvent.click(
      within(genre).getByRole("button", { name: /show all \(10\)/i }),
    );
    expect(within(genre).getByText("G9")).toBeInTheDocument();
    await userEvent.click(
      within(genre).getByRole("button", { name: /show less/i }),
    );
    expect(within(genre).queryByText("G9")).not.toBeInTheDocument();
  });

  test("the sort select drives the sort param and resets the offset", async () => {
    renderWithProviders(<BrowsePage />, { route: "/browse?offset=48" });
    const select = await screen.findByRole("combobox", { name: /sort albums/i });
    await userEvent.selectOptions(select, "added");
    await waitFor(() => {
      expect(lastQuery.get("sort")).toBe("added");
      // useBrowseAlbums always threads offset; the reset lands as "0" (page 1),
      // not an omitted param — openapi-fetch serializes the numeric zero.
      expect(lastQuery.get("offset")).toBe("0");
    });
  });

  test("Clear all keeps the sort", async () => {
    renderWithProviders(<BrowsePage />, {
      route: "/browse?genre=Rock&sort=added",
    });
    await userEvent.click(
      await screen.findByRole("button", { name: /clear all/i }),
    );
    await waitFor(() => {
      expect(lastQuery.getAll("genre")).toEqual([]);
      expect(lastQuery.get("sort")).toBe("added");
    });
  });

  test("size selector and top pager appear when the library outgrows a page", async () => {
    server.use(
      http.get(ALBUMS, ({ request }) => {
        lastQuery = new URL(request.url).searchParams;
        return HttpResponse.json({
          items: [album(1, "First"), album(2, "Second")],
          total: 300,
          limit: 48,
          offset: 0,
        });
      }),
    );
    renderWithProviders(<BrowsePage />, { route: "/browse" });
    await screen.findByText("First");
    // Two pagers: the compact one in the toolbar plus the one under the grid.
    expect(
      screen.getByRole("navigation", { name: "Pagination (top)" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("navigation", { name: "Pagination" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("combobox", { name: "Results per page" }),
    ).toBeInTheDocument();
    // The per-page choice lives in the page header beside the h1 (same place
    // as Artists); the toolbar keeps only the sort select and the pager.
    const sizeSelect = screen.getByRole("combobox", {
      name: "Results per page",
    });
    const header = sizeSelect.closest("header");
    expect(header).not.toBeNull();
    expect(
      within(header as HTMLElement).getByRole("heading", { level: 1 }),
    ).toHaveTextContent("Browse");
    expect(header).not.toContainElement(
      screen.getByRole("combobox", { name: "Sort albums" }),
    );
  });

  test("choosing a page size drives the albums query and the URL", async () => {
    server.use(
      http.get(ALBUMS, ({ request }) => {
        lastQuery = new URL(request.url).searchParams;
        return HttpResponse.json({
          items: [album(1, "First")],
          total: 300,
          limit: 48,
          offset: 0,
        });
      }),
    );
    renderWithProviders(<BrowsePage />, { route: "/browse" });
    await screen.findByText("First");
    await userEvent.selectOptions(
      screen.getByRole("combobox", { name: "Results per page" }),
      "96",
    );
    await waitFor(() => expect(lastQuery.get("limit")).toBe("96"));
  });

  test("neither pager nor size selector renders for a one-page library", async () => {
    renderWithProviders(<BrowsePage />, { route: "/browse" });
    await screen.findByText("First"); // default handler: total 2
    expect(screen.queryByRole("navigation")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("combobox", { name: "Results per page" }),
    ).not.toBeInTheDocument();
  });
});
