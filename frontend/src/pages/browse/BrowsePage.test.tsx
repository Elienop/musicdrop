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
};

describe("BrowsePage", () => {
  beforeEach(() => {
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
    await userEvent.click(await screen.findByRole("button", { name: /next/i }));
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

    await userEvent.click(await screen.findByRole("button", { name: /next/i }));

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
});
