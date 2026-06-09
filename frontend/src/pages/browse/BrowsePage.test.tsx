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
});
