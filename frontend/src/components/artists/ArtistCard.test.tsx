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
    expect(link).toHaveClass("focus-ring", "group", "block", "rounded-lg");
    expect(link.className).not.toMatch(/focus-visible:ring-2/);
    expect(link.querySelector('[data-slot="card"]')).toBeNull();
  });

  test("hover mechanism: ring + clipping on the wrapper, scale on the portrait", () => {
    renderWithProviders(<ArtistCard artist={ARTIST} />);

    const img = screen.getByAltText("Sigur Rós portrait");
    expect(img).toHaveClass(
      "aspect-square",
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

  test("name + album count render below the art", () => {
    renderWithProviders(<ArtistCard artist={ARTIST} />);

    expect(screen.getByText("Sigur Rós")).toBeInTheDocument();
    expect(screen.getByText("7 albums")).toBeInTheDocument();
  });
});
