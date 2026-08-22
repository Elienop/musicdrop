import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, expect, test } from "vitest";

import type { ExistingAlbum } from "@/api/useBank";

import { AlreadyInLibrary } from "@/components/import/AlreadyInLibrary";

function makeExisting(overrides: Partial<ExistingAlbum> = {}): ExistingAlbum {
  return {
    album_id: 1,
    album_artist: "10cc",
    album: "The Essential 10cc",
    year: 2016,
    track_count: 2,
    format: "FLAC",
    bitrate_kbps: 1000,
    folder: "/music/10cc",
    tracks: [],
    ...overrides,
  };
}

// The View link needs a router context; the panels use memory routing like
// the other tests that render Link (e.g. AlbumRow.test.tsx).
function renderSection(
  existing: ExistingAlbum[] = [makeExisting()],
  blurb = "These already live in your library.",
) {
  return render(
    <MemoryRouter initialEntries={["/import"]}>
      <AlreadyInLibrary existing={existing} blurb={blurb} />
    </MemoryRouter>,
  );
}

describe("cover requests", () => {
  // Regression: the up-front comparison is a thumbnail — every cover must be
  // requested at the thumb variant. A silent flip to full-size art bloats the
  // request and must fail this.
  test("requests each existing album's cover exactly at the thumb variant", () => {
    const { container } = renderSection([
      makeExisting({ album_id: 1 }),
      makeExisting({ album_id: 2, album: "Live at the Palladium" }),
    ]);
    const covers = container.querySelectorAll('img[src*="/cover"]');
    expect(covers).toHaveLength(2);
    for (const albumId of [1, 2]) {
      expect(
        container.querySelector(
          `img[src="/api/albums/${albumId}/cover?size=thumb"]`,
        ),
      ).not.toBeNull();
    }
    // No full-size (or size-less) cover may sneak in.
    for (const img of covers) {
      expect(img.getAttribute("src")).toMatch(
        /\/api\/albums\/\d+\/cover\?size=thumb$/,
      );
    }
  });
});

describe("blurb", () => {
  test("renders the flow-specific blurb beneath the section label", () => {
    renderSection([], "Your bank resolves these inline below.");
    expect(screen.getByText("Your bank resolves these inline below.")).toBeInTheDocument();
  });
});

describe("tracklist rows", () => {
  test("shows number · title · quality, with '-' and 'Untitled' fallbacks", () => {
    renderSection([
      makeExisting({
        tracks: [
          {
            track: 1,
            disc: null,
            title: "A Question of Live",
            format: "FLAC",
            bitrate_kbps: 1000,
          },
          {
            track: null,
            disc: null,
            title: null,
            format: "FLAC",
            bitrate_kbps: null,
          },
        ],
      }),
    ]);
    expect(screen.getByText("A Question of Live")).toBeInTheDocument();
    // A NULL track falls back to '-', NULL title to 'Untitled'.
    expect(screen.getByText("-")).toBeInTheDocument();
    expect(screen.getByText("Untitled")).toBeInTheDocument();
    // quality joins as 'FORMAT · N kbps'...
    expect(screen.getByText("FLAC · 1000 kbps")).toBeInTheDocument();
    // ...and the bitrate part is absent when NULL (the panel's joined meta
    // line is a different text node, so this match is the row's own).
    expect(screen.getByText("FLAC")).toBeInTheDocument();
  });

  test("an album with no tracks renders no tracklist at all", () => {
    const { container } = renderSection([makeExisting()]);
    expect(container.querySelector("ol")).toBeNull();
  });
});

describe("album links and panels", () => {
  test("each album gets a View link pointing at its album page", () => {
    renderSection([
      makeExisting({ album_id: 1 }),
      makeExisting({ album_id: 2, album: "Live at the Palladium" }),
    ]);
    const links = screen.getAllByRole("link", { name: "View" });
    expect(links.map((l) => l.getAttribute("href"))).toEqual([
      "/albums/1",
      "/albums/2",
    ]);
  });

  test("two albums render two panels", () => {
    renderSection([
      makeExisting({ album_id: 1 }),
      makeExisting({ album_id: 2, album: "Live at the Palladium" }),
    ]);
    expect(screen.getAllByText("Already in library")).toHaveLength(2);
  });
});
