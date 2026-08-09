import { screen } from "@testing-library/react";
import { describe, expect, test } from "vitest";

import type { Artist } from "@/api/useArtists";
import { ArtistCard } from "@/components/artists/ArtistCard";
import { renderWithProviders } from "@/test/render";

const ARTIST: Artist = { name: "Sigur Rós", album_count: 7 };

describe("ArtistCard (borderless)", () => {
  test("whole card is a focus-ring link with no Card chrome", () => {
    renderWithProviders(<ArtistCard artist={ARTIST} />);

    const link = screen.getByRole("link", { name: /Sigur Rós/ });
    expect(link).toHaveAttribute(
      "href",
      `/artists/${encodeURIComponent("Sigur Rós")}`,
    );
    expect(link).toHaveClass("focus-ring", "rounded-lg");
    expect(link.className).not.toMatch(/focus-visible:ring-2/);
    expect(link.querySelector('[data-slot="card"]')).toBeNull();
  });

  test("row anatomy: square portrait beside centered info, surface-tint hover", () => {
    renderWithProviders(<ArtistCard artist={ARTIST} />);

    const img = screen.getByAltText("Sigur Rós portrait");
    // Square thumb (Koito "Albums featuring" scale), never squashed.
    expect(img).toHaveClass("size-32", "shrink-0", "rounded-lg");
    // The whole link is the row: thumb + info vertically centered, hover is
    // the ONE app hover dialect: the neutral surface fill.
    expect(img.closest("a")).toHaveClass(
      "flex",
      "items-center",
      "hover:bg-surface-hover",
    );
  });

  test("portrait requests the thumb variant", () => {
    renderWithProviders(<ArtistCard artist={ARTIST} />);

    const img = screen.getByAltText("Sigur Rós portrait");
    expect(img).toHaveAttribute(
      "src",
      expect.stringContaining("&size=thumb"),
    );
  });

  test("name + album count render below the art", () => {
    renderWithProviders(<ArtistCard artist={ARTIST} />);

    expect(screen.getByText("Sigur Rós")).toBeInTheDocument();
    expect(screen.getByText("7 albums")).toBeInTheDocument();
  });
});
