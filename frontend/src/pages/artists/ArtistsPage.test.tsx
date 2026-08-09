import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, test } from "vitest";

import type { components } from "@/api/schema";
import { ArtistsPage } from "@/pages/artists/ArtistsPage";
import { renderWithProviders } from "@/test/render";
import { server } from "@/test/msw-server";

type Artist = components["schemas"]["Artist"];

const ARTISTS_URL = `${window.location.origin}/api/artists`;

const ROSTER: Artist[] = [
  { name: "Radiohead", album_count: 9 },
  { name: "Aphex Twin", album_count: 1 },
  { name: "Sigur Rós", album_count: 7 },
];

describe("ArtistsPage", () => {
  test("renders the roster with names and album counts", async () => {
    server.use(http.get(ARTISTS_URL, () => HttpResponse.json(ROSTER)));

    renderWithProviders(<ArtistsPage />);

    expect(await screen.findByText("Radiohead")).toBeInTheDocument();
    expect(screen.getByText("Aphex Twin")).toBeInTheDocument();
    expect(screen.getByText("Sigur Rós")).toBeInTheDocument();

    // Pluralization: "9 albums", singular "1 album".
    expect(screen.getByText("9 albums")).toBeInTheDocument();
    expect(screen.getByText("1 album")).toBeInTheDocument();
    expect(screen.getByText("7 albums")).toBeInTheDocument();
  });

  test("links each artist to that artist's albums page", async () => {
    server.use(http.get(ARTISTS_URL, () => HttpResponse.json(ROSTER)));

    renderWithProviders(<ArtistsPage />);

    await screen.findByText("Radiohead");

    const radiohead = screen.getByRole("link", { name: /Radiohead/i });
    expect(radiohead).toHaveAttribute("href", "/artists/Radiohead");

    // Names with spaces / non-ASCII must be percent-encoded into the path.
    const sigur = screen.getByRole("link", { name: /Sigur Rós/i });
    expect(sigur).toHaveAttribute(
      "href",
      `/artists/${encodeURIComponent("Sigur Rós")}`,
    );
  });

  test("each card shows a poster image pointing at the artist-image endpoint", async () => {
    server.use(http.get(ARTISTS_URL, () => HttpResponse.json(ROSTER)));

    renderWithProviders(<ArtistsPage />);

    const poster = await screen.findByAltText("Radiohead portrait");
    expect(poster).toHaveAttribute(
      "src",
      "/api/artists/image?name=Radiohead&size=thumb",
    );
    expect(poster).toHaveAttribute("loading", "lazy");

    // Encoded for non-ASCII names.
    expect(screen.getByAltText("Sigur Rós portrait")).toHaveAttribute(
      "src",
      `/api/artists/image?name=${encodeURIComponent("Sigur Rós")}&size=thumb`,
    );
  });

  test("shows a loading skeleton before data arrives", () => {
    // Never-resolving handler so the page stays in its pending state.
    server.use(
      http.get(
        ARTISTS_URL,
        () => new Promise<HttpResponse<Artist[]>>(() => {}),
      ),
    );

    const { container } = renderWithProviders(<ArtistsPage />);

    expect(
      container.querySelector('[aria-hidden="true"]'),
    ).toBeInTheDocument();
  });

  test("announces a loading status to screen readers while pending", () => {
    server.use(
      http.get(
        ARTISTS_URL,
        () => new Promise<HttpResponse<Artist[]>>(() => {}),
      ),
    );

    renderWithProviders(<ArtistsPage />);

    const status = screen.getByRole("status");
    expect(status).toHaveTextContent(/loading artists/i);
  });

  test("shows the empty state when the roster is empty", async () => {
    server.use(http.get(ARTISTS_URL, () => HttpResponse.json([])));

    renderWithProviders(<ArtistsPage />);

    expect(await screen.findByText(/no artists/i)).toBeInTheDocument();
  });

  test("shows an error state with a retry on a 500", async () => {
    server.use(
      http.get(ARTISTS_URL, () => new HttpResponse(null, { status: 500 })),
    );

    renderWithProviders(<ArtistsPage />);

    expect(
      await screen.findByText(/couldn.t load artists/i),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument();
  });

  test("retry refetches after a transient error", async () => {
    let calls = 0;
    server.use(
      http.get(ARTISTS_URL, () => {
        calls += 1;
        if (calls === 1) {
          return new HttpResponse(null, { status: 500 });
        }
        return HttpResponse.json(ROSTER);
      }),
    );

    renderWithProviders(<ArtistsPage />);

    const retry = await screen.findByRole("button", { name: /retry/i });
    await userEvent.click(retry);

    expect(await screen.findByText("Radiohead")).toBeInTheDocument();
  });

  test("renders the Artists h1 with the roster count in the meta line", async () => {
    server.use(http.get(ARTISTS_URL, () => HttpResponse.json(ROSTER)));

    renderWithProviders(<ArtistsPage />);

    expect(
      await screen.findByRole("heading", { level: 1, name: "Artists" }),
    ).toBeInTheDocument();
    const count = await screen.findByText("3 artists");
    expect(count.closest("[aria-live]")).toHaveAttribute(
      "aria-live",
      "polite",
    );
  });

  test("the empty roster offers the import CTA", async () => {
    server.use(http.get(ARTISTS_URL, () => HttpResponse.json([])));

    renderWithProviders(<ArtistsPage />);

    const cta = await screen.findByRole("link", {
      name: /add music from a folder/i,
    });
    expect(cta).toHaveAttribute("href", "/import");
  });

  test("paginates a roster larger than one page", async () => {
    // 60 artists > PAGE_SIZE (48): the page must render one slice, not all of
    // them (the 10k-library render-storm this fixes).
    const big: Artist[] = Array.from({ length: 60 }, (_, i) => ({
      name: `Artist ${String(i + 1).padStart(2, "0")}`,
      album_count: 1,
    }));
    server.use(http.get(ARTISTS_URL, () => HttpResponse.json(big)));

    renderWithProviders(<ArtistsPage />);

    // First page: exactly PAGE_SIZE cards, and the last artist isn't on it.
    await screen.findByText("Artist 01");
    expect(screen.getAllByRole("listitem")).toHaveLength(48);
    expect(screen.queryByText("Artist 60")).not.toBeInTheDocument();
    // The meta line still reports the FULL count, not the page size.
    expect(screen.getByText("60 artists")).toBeInTheDocument();

    // Advancing to the next page renders the remaining slice (12 artists).
    // Exact "Next" targets the bottom pager only — the compact top pager's
    // button is named "Next page", which /next/i would also match.
    await userEvent.click(screen.getByRole("button", { name: "Next" }));
    await screen.findByText("Artist 60");
    expect(screen.getAllByRole("listitem")).toHaveLength(12);
    expect(screen.queryByText("Artist 01")).not.toBeInTheDocument();
  });

  test("a big roster gets a top toolbar: size selector + compact pager", async () => {
    localStorage.clear();
    const big: Artist[] = Array.from({ length: 60 }, (_, i) => ({
      name: `Artist ${String(i).padStart(2, "0")}`,
      album_count: 1,
    }));
    server.use(http.get(ARTISTS_URL, () => HttpResponse.json(big)));

    renderWithProviders(<ArtistsPage />);

    await screen.findByText("Artist 00");
    // Default page size 48: the 49th artist is on page 2.
    expect(screen.queryByText("Artist 48")).not.toBeInTheDocument();
    expect(
      screen.getByRole("navigation", { name: "Pagination (top)" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("navigation", { name: "Pagination" }),
    ).toBeInTheDocument();

    // Switching to 96 per page shows the whole roster and drops the pagers
    // (60 < 96) while the selector stays (60 > 48).
    await userEvent.selectOptions(
      screen.getByRole("combobox", { name: "Results per page" }),
      "96",
    );
    expect(await screen.findByText("Artist 59")).toBeInTheDocument();
    expect(screen.queryByRole("navigation")).not.toBeInTheDocument();
    expect(
      screen.getByRole("combobox", { name: "Results per page" }),
    ).toBeInTheDocument();
  });

  test("the letter index sits in the toolbar row, not on a row of its own", async () => {
    localStorage.clear();
    const big: Artist[] = Array.from({ length: 60 }, (_, i) => ({
      name: `Artist ${String(i).padStart(2, "0")}`,
      album_count: 1,
    }));
    server.use(http.get(ARTISTS_URL, () => HttpResponse.json(big)));

    renderWithProviders(<ArtistsPage />);
    await screen.findByText("Artist 00");

    const index = screen.getByRole("navigation", {
      name: "Jump to artists by letter",
    });
    const topPager = screen.getByRole("navigation", {
      name: "Pagination (top)",
    });
    const sizeSelect = screen.getByRole("combobox", {
      name: "Results per page",
    });
    // ONE horizontal band: the letters, the size select and the pager are
    // siblings in a single flex row, with the letters taking the empty space
    // to the left of the controls. A second row for the index is exactly the
    // vertical space this reclaims.
    const band = index.parentElement;
    expect(topPager.parentElement).toBe(band);
    // PageSizeSelect wraps its <select> in a positioning span.
    expect(sizeSelect.parentElement?.parentElement).toBe(band);
    expect(band?.classList.contains("flex")).toBe(true);
    expect(band?.classList.contains("flex-col")).toBe(false);
  });

  test("a small roster gets no toolbar at all", async () => {
    localStorage.clear();
    server.use(http.get(ARTISTS_URL, () => HttpResponse.json(ROSTER)));
    renderWithProviders(<ArtistsPage />);
    await screen.findByText("Radiohead");
    expect(
      screen.queryByRole("combobox", { name: "Results per page" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByRole("navigation")).not.toBeInTheDocument();
  });
});
