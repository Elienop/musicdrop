import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, test } from "vitest";

import { ArtistImage } from "@/components/artists/ArtistImage";

describe("ArtistImage", () => {
  test("renders a lazy <img> pointing at the artist-image endpoint", () => {
    render(<ArtistImage name="Radiohead" />);

    const img = screen.getByRole("img", { name: "Radiohead portrait" });
    expect(img).toHaveAttribute(
      "src",
      "/api/artists/image?name=Radiohead",
    );
    expect(img).toHaveAttribute("loading", "lazy");
  });

  test("uses a descriptive '{name} portrait' alt by default", () => {
    render(<ArtistImage name="Radiohead" />);

    expect(screen.getByAltText("Radiohead portrait")).toBeInTheDocument();
  });

  test("is decorative (empty alt) when `decorative`", () => {
    const { container } = render(
      <ArtistImage name="Radiohead" decorative />,
    );

    const img = container.querySelector("img");
    expect(img).not.toBeNull();
    expect(img).toHaveAttribute("alt", "");
    // No accessible name for assistive tech (the adjacent heading names it).
    expect(
      screen.queryByRole("img", { name: /radiohead/i }),
    ).not.toBeInTheDocument();
  });

  test("encodes the artist name in the src", () => {
    render(<ArtistImage name="Sigur Rós" />);

    const img = screen.getByAltText("Sigur Rós portrait");
    expect(img).toHaveAttribute(
      "src",
      `/api/artists/image?name=${encodeURIComponent("Sigur Rós")}`,
    );
  });

  test("falls back to an initials monogram (not a person glyph) on error", () => {
    render(<ArtistImage name="ABBA" />);

    const img = screen.getByAltText("ABBA portrait");
    fireEvent.error(img);

    // The <img> is gone; a labelled monogram box takes its place.
    expect(screen.queryByAltText("ABBA portrait")).not.toBeInTheDocument();
    const fallback = screen.getByLabelText("ABBA");
    expect(fallback).toBeInTheDocument();
    // The first alphanumeric character, uppercased.
    expect(fallback).toHaveTextContent("A");
  });

  test("monogram uppercases the first character", () => {
    render(<ArtistImage name="aphex twin" />);

    fireEvent.error(screen.getByAltText("aphex twin portrait"));

    expect(screen.getByLabelText("aphex twin")).toHaveTextContent("A");
  });

  test("monogram uses the first alphanumeric character for leading symbols/digits", () => {
    render(<ArtistImage name="2 Unlimited" />);

    fireEvent.error(screen.getByAltText("2 Unlimited portrait"));

    // Digit is alphanumeric, so "2".
    expect(screen.getByLabelText("2 Unlimited")).toHaveTextContent("2");
  });

  test("decorative fallback drops the aria-label and is aria-hidden", () => {
    const { container } = render(<ArtistImage name="Radiohead" decorative />);

    fireEvent.error(container.querySelector("img") as HTMLImageElement);

    // No accessible name; hidden from assistive tech.
    expect(
      screen.queryByLabelText(/radiohead/i),
    ).not.toBeInTheDocument();
    const fallback = container.querySelector('[aria-hidden="true"]');
    expect(fallback).not.toBeNull();
    expect(fallback).toHaveTextContent("R");
  });

  test("applies the caller's className to both the image and the fallback", () => {
    render(<ArtistImage name="Radiohead" className="size-40 rounded-xl" />);

    const img = screen.getByAltText("Radiohead portrait");
    expect(img).toHaveClass("size-40", "rounded-xl");

    fireEvent.error(img);
    // Same sizing on the fallback so there's no layout shift.
    const fallback = screen.getByLabelText("Radiohead");
    expect(fallback).toHaveClass("size-40", "rounded-xl");
  });
});
