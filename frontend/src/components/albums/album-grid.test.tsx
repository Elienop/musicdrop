import { screen } from "@testing-library/react";
import { describe, expect, test } from "vitest";

import type { Album } from "@/api/useAlbums";
import {
  AlbumCard,
  AlbumsGridSkeleton,
  GRID_CLASS,
} from "@/components/albums/album-grid";
import { renderWithProviders } from "@/test/render";

const ALBUM: Album = {
  id: 7,
  album_artist: "Radiohead",
  title: "OK Computer",
  year: 1997,
  track_count: 12,
  genre: "Alternative Rock",
  mb_albumid: null,
};

describe("AlbumCard (borderless)", () => {
  test("whole card is a focus-ring link with no Card chrome", () => {
    renderWithProviders(<AlbumCard album={ALBUM} />);

    const link = screen.getByRole("link", { name: /OK Computer/ });
    expect(link).toHaveAttribute("href", "/albums/7");
    // Phase-3 focus dialect on the link itself; the old hand-rolled
    // ring-2/ring-offset-2 variant is gone.
    expect(link).toHaveClass("focus-ring", "rounded-lg");
    expect(link.className).not.toMatch(/focus-visible:ring-2/);
    // The bordered Card shell is dead — the art IS the card.
    expect(link.querySelector('[data-slot="card"]')).toBeNull();
  });

  test("row anatomy: square thumb beside centered info, surface-tint hover", () => {
    renderWithProviders(<AlbumCard album={ALBUM} />);

    const img = screen.getByAltText("OK Computer cover");
    // Square thumb (Koito "Albums featuring" scale), never squashed.
    expect(img).toHaveClass("size-32", "shrink-0", "rounded-lg");
    // The whole link is the row: thumb + info vertically centered, hover is
    // the ONE app hover dialect: the neutral surface fill.
    const link = img.closest("a");
    expect(link).toHaveClass(
      "flex",
      "items-center",
      "hover:bg-surface-hover",
    );
    expect(
      document.querySelector('div[aria-hidden="true"].fade-bottom-to-base'),
    ).toBeNull();
  });

  test("info column: title, artist, one compact fact line", () => {
    renderWithProviders(<AlbumCard album={ALBUM} />);

    expect(screen.getByText("OK Computer")).toBeInTheDocument();
    expect(screen.getByText("Radiohead")).toBeInTheDocument();
    // Year · count · genre collapse to ONE compact truncating line (no
    // badge chrome).
    expect(
      screen.getByText(
        (_, el) =>
          el?.tagName === "SPAN" &&
          el.textContent === "1997 · 12 tracks · Alternative Rock",
      ),
    ).toBeInTheDocument();
  });
});

describe("GRID_CLASS", () => {
  test("row-card grid: auto-fill columns at the derived minimum, no breakpoint counts", () => {
    // Column count falls out of the space (auto-fill at --card-row-min);
    // min(...,100%) guards narrow phones.
    expect(GRID_CLASS).toContain(
      "grid-cols-[repeat(auto-fill,minmax(min(var(--card-row-min),100%),1fr))]",
    );
    expect(GRID_CLASS).toContain("gap-x-6");
    expect(GRID_CLASS).toContain("gap-y-3");
    expect(GRID_CLASS).not.toMatch(/grid-cols-\d/);
  });
});

describe("AlbumsGridSkeleton (borderless)", () => {
  test("mirrors the row-card anatomy: square thumb + text lines, no Card chrome", () => {
    const { container } = renderWithProviders(<AlbumsGridSkeleton count={3} />);

    const list = container.querySelector("ul");
    expect(list).toHaveAttribute("aria-hidden", "true");
    expect(container.querySelectorAll("li")).toHaveLength(3);
    expect(container.querySelector('[data-slot="card"]')).toBeNull();
    // One square thumb placeholder per row.
    expect(container.querySelectorAll(".size-32")).toHaveLength(3);
  });
});
