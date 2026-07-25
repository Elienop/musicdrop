// frontend/src/components/system/CoverArt.test.tsx
import { act, fireEvent, render, screen } from "@testing-library/react";
import { expect, test } from "vitest";

import { bumpAssetVersion } from "@/api/assetVersion";
import { CoverArt } from "@/components/system/CoverArt";

test("renders the image with src, alt, and caller-supplied sizing", () => {
  render(
    <CoverArt
      src="/api/albums/7/cover"
      alt="OK Computer cover"
      className="size-40 rounded-xl"
    />,
  );
  const img = screen.getByRole("img", { name: "OK Computer cover" });
  expect(img).toHaveAttribute("src", "/api/albums/7/cover");
  expect(img).toHaveAttribute("loading", "lazy");
  expect(img).toHaveClass("size-40", "rounded-xl", "aspect-square", "object-cover");
});

test("null src renders the fallback immediately (no img, no request)", () => {
  const { container } = render(<CoverArt src={null} alt="OK Computer cover" />);
  expect(container.querySelector("img")).toBeNull();
  const fallback = screen.getByRole("img", { name: "OK Computer cover unavailable" });
  expect(fallback).toHaveClass("bg-muted", "aspect-square");
  expect(fallback.querySelector("svg")).not.toBeNull();
});

test("decode/404 error flips to the fallback", () => {
  const { container } = render(
    <CoverArt src="/api/albums/7/cover" alt="OK Computer cover" />,
  );
  fireEvent.error(container.querySelector("img") as HTMLImageElement);
  expect(container.querySelector("img")).toBeNull();
  expect(
    screen.getByRole("img", { name: "OK Computer cover unavailable" }),
  ).toBeInTheDocument();
});

test("decorative cover (default alt) hides the fallback from AT", () => {
  const { container } = render(<CoverArt src={null} />);
  expect(screen.queryByRole("img")).toBeNull();
  const fallback = container.querySelector('[data-slot="cover-art-fallback"]');
  expect(fallback).toHaveAttribute("aria-hidden", "true");
});

// The remount key isn't readable off the DOM, so these assert its observable
// consequence: remounting resets the failed state and brings the <img> back.
test("an art event for ANOTHER album leaves a scoped cover alone", () => {
  const { container } = render(
    <CoverArt src="/api/albums/7/cover" assetKey="album:7" />,
  );
  fireEvent.error(container.querySelector("img") as HTMLImageElement);
  expect(container.querySelector("img")).toBeNull();
  // Someone installed a cover on album 8 in another tab — album 7 must not
  // remount (no flash, no conditional GET).
  act(() => bumpAssetVersion("album:8"));
  expect(container.querySelector("img")).toBeNull();
});

test("an art event for THIS album remounts the cover", () => {
  const { container } = render(
    <CoverArt src="/api/albums/7/cover" assetKey="album:7" />,
  );
  fireEvent.error(container.querySelector("img") as HTMLImageElement);
  expect(container.querySelector("img")).toBeNull();
  act(() => bumpAssetVersion("album:7"));
  expect(container.querySelector("img")).not.toBeNull();
});

test("an UNSCOPED art event still remounts a scoped cover", () => {
  const { container } = render(
    <CoverArt src="/api/albums/7/cover" assetKey="album:7" />,
  );
  fireEvent.error(container.querySelector("img") as HTMLImageElement);
  act(() => bumpAssetVersion());
  expect(container.querySelector("img")).not.toBeNull();
});

test("a new src resets a previous failure (cache-busted reload works)", () => {
  const { container, rerender } = render(<CoverArt src="/api/albums/7/cover" />);
  fireEvent.error(container.querySelector("img") as HTMLImageElement);
  expect(container.querySelector("img")).toBeNull();
  rerender(<CoverArt src="/api/albums/7/cover?v=2" />);
  expect(container.querySelector("img")).toHaveAttribute(
    "src",
    "/api/albums/7/cover?v=2",
  );
});
