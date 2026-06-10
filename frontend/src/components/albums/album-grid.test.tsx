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
    expect(link).toHaveClass("focus-ring", "group", "block", "rounded-lg");
    expect(link.className).not.toMatch(/focus-visible:ring-2/);
    // The bordered Card shell is dead — the art IS the card.
    expect(link.querySelector('[data-slot="card"]')).toBeNull();
  });

  test("hover mechanism: ring + clipping on the wrapper, scale on the image", () => {
    renderWithProviders(<AlbumCard album={ALBUM} />);

    const img = screen.getByAltText("OK Computer cover");
    expect(img).toHaveClass(
      "transition-transform",
      "motion-safe:group-hover:scale-[1.02]",
    );
    expect(img.parentElement).toHaveClass(
      "overflow-hidden",
      "rounded-lg",
      "ring-1",
      "ring-transparent",
      "group-hover:ring-primary/50",
    );
  });

  test("meta renders below the art: title, artist, year badge, count, genre", () => {
    renderWithProviders(<AlbumCard album={ALBUM} />);

    expect(screen.getByText("OK Computer")).toBeInTheDocument();
    expect(screen.getByText("Radiohead")).toBeInTheDocument();
    expect(screen.getByText("1997")).toBeInTheDocument();
    expect(screen.getByText("12 tracks")).toBeInTheDocument();
    expect(screen.getByText("Alternative Rock")).toBeInTheDocument();
  });
});

describe("GRID_CLASS", () => {
  test("keeps every column breakpoint and widens row gaps", () => {
    for (const cls of [
      "grid-cols-1",
      "sm:grid-cols-2",
      "lg:grid-cols-3",
      "xl:grid-cols-4",
      "2xl:grid-cols-5",
      "gap-x-4",
      "gap-y-6",
    ]) {
      expect(GRID_CLASS).toContain(cls);
    }
  });
});

describe("AlbumsGridSkeleton (borderless)", () => {
  test("mirrors the card anatomy: square art + two text lines, no Card chrome", () => {
    const { container } = renderWithProviders(<AlbumsGridSkeleton count={3} />);

    const list = container.querySelector("ul");
    expect(list).toHaveAttribute("aria-hidden", "true");
    expect(container.querySelectorAll("li")).toHaveLength(3);
    expect(container.querySelector('[data-slot="card"]')).toBeNull();
    // One square placeholder per item.
    expect(container.querySelectorAll(".aspect-square")).toHaveLength(3);
  });
});
