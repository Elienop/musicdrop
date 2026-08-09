import { render } from "@testing-library/react";
import { describe, expect, test } from "vitest";

import { PlaylistCover } from "@/components/playlists/PlaylistCover";

const ID = "a".repeat(32);

/** Minimal cover-relevant slice of a Playlist (the component only reads these). */
function cover(over: {
  artwork_hash?: string | null;
  cover_album_ids?: number[];
}) {
  return {
    id: ID,
    artwork_hash: over.artwork_hash ?? null,
    cover_album_ids: over.cover_album_ids ?? [],
  };
}

describe("PlaylistCover", () => {
  test("artwork_hash → one square image, cache-busted by the hash", () => {
    const { container } = render(
      <PlaylistCover playlist={cover({ artwork_hash: "deadbeef" })} />,
    );
    const imgs = container.querySelectorAll("img");
    expect(imgs).toHaveLength(1);
    expect(imgs[0]).toHaveAttribute(
      "src",
      `/api/playlists/${ID}/artwork?v=deadbeef`,
    );
    // Decorative — the surrounding row/header carries the name.
    expect(imgs[0]).toHaveAttribute("alt", "");
  });

  test("no artwork, 4 cover ids → a 2×2 collage of album covers", () => {
    const { container } = render(
      <PlaylistCover playlist={cover({ cover_album_ids: [1, 2, 3, 4] })} />,
    );
    const imgs = container.querySelectorAll("img");
    expect(imgs).toHaveLength(4);
    expect([...imgs].map((i) => i.getAttribute("src"))).toEqual([
      "/api/albums/1/cover?size=thumb",
      "/api/albums/2/cover?size=thumb",
      "/api/albums/3/cover?size=thumb",
      "/api/albums/4/cover?size=thumb",
    ]);
  });

  test("more than 4 cover ids are capped at 4 tiles", () => {
    const { container } = render(
      <PlaylistCover
        playlist={cover({ cover_album_ids: [1, 2, 3, 4, 5, 6] })}
      />,
    );
    expect(container.querySelectorAll("img")).toHaveLength(4);
  });

  test("3 cover ids → 3 tiles with the first spanning to fill the grid", () => {
    const { container } = render(
      <PlaylistCover playlist={cover({ cover_album_ids: [7, 8, 9] })} />,
    );
    const imgs = container.querySelectorAll("img");
    expect(imgs).toHaveLength(3);
    expect(imgs[0].className).toContain("col-span-2");
  });

  test("1 cover id → a single tile filling the whole square", () => {
    const { container } = render(
      <PlaylistCover playlist={cover({ cover_album_ids: [42] })} />,
    );
    const imgs = container.querySelectorAll("img");
    expect(imgs).toHaveLength(1);
    expect(imgs[0]).toHaveAttribute("src", "/api/albums/42/cover?size=thumb");
    expect(imgs[0].className).toContain("col-span-2");
    expect(imgs[0].className).toContain("row-span-2");
  });

  test("no artwork and no cover ids → the Playlists icon placeholder, no image", () => {
    const { container } = render(<PlaylistCover playlist={cover({})} />);
    expect(container.querySelectorAll("img")).toHaveLength(0);
    expect(
      container.querySelector('[data-slot="playlist-cover-fallback"]'),
    ).not.toBeNull();
  });

  test("forwards className to the rendered box for caller sizing", () => {
    const { container } = render(
      <PlaylistCover playlist={cover({})} className="size-12 rounded-lg" />,
    );
    const box = container.querySelector(
      '[data-slot="playlist-cover-fallback"]',
    );
    expect(box?.className).toContain("size-12");
    expect(box?.className).toContain("rounded-lg");
  });
});
