import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, test } from "vitest";

import { ArtistImage } from "@/components/artists/ArtistImage";

describe("ArtistImage", () => {
  test("renders a lazy <img> pointing at the artist-image endpoint", () => {
    render(<ArtistImage name="Radiohead" />);

    const img = screen.getByAltText("Radiohead");
    expect(img).toHaveAttribute(
      "src",
      "/api/artists/image?name=Radiohead",
    );
    expect(img).toHaveAttribute("loading", "lazy");
  });

  test("encodes the artist name in the src", () => {
    render(<ArtistImage name="Sigur Rós" />);

    const img = screen.getByAltText("Sigur Rós");
    expect(img).toHaveAttribute(
      "src",
      `/api/artists/image?name=${encodeURIComponent("Sigur Rós")}`,
    );
  });

  test("falls back to a labelled glyph box when the image errors", () => {
    render(<ArtistImage name="Radiohead" />);

    const img = screen.getByAltText("Radiohead");
    // Simulate the 404 / broken-image path (feature off or no match).
    fireEvent.error(img);

    // The <img> is gone; a labelled placeholder takes its place.
    expect(screen.queryByAltText("Radiohead")).not.toBeInTheDocument();
    expect(
      screen.getByLabelText("Radiohead unavailable"),
    ).toBeInTheDocument();
  });

  test("applies the caller's className to both the image and the fallback", () => {
    const { rerender } = render(
      <ArtistImage name="Radiohead" className="size-40 rounded-xl" />,
    );

    const img = screen.getByAltText("Radiohead");
    expect(img).toHaveClass("size-40", "rounded-xl");

    fireEvent.error(img);
    // Same sizing on the fallback so there's no layout shift.
    const fallback = screen.getByLabelText("Radiohead unavailable");
    expect(fallback).toHaveClass("size-40", "rounded-xl");
    rerender(<ArtistImage name="Radiohead" className="size-40 rounded-xl" />);
  });
});
