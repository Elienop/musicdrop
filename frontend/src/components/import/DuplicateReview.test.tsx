import { render, screen } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";

import type { DuplicatePrompt } from "@/api/useImport";
import {
  DuplicateActionRow,
  DuplicateActions,
  DuplicateComparison,
} from "@/components/import/DuplicateReview";

function makePrompt(overrides: Partial<DuplicatePrompt> = {}): DuplicatePrompt {
  return {
    album_index: 0,
    incoming: {
      album_artist: "10cc",
      album: "The Essential 10cc",
      year: 2016,
      track_count: 35,
      format: "FLAC",
      bitrate_kbps: 1000,
      folder: "/downloads/10cc",
      has_current_art: false,
    },
    existing: [
      {
        album_id: 1,
        album_artist: "10cc",
        album: "The Essential 10cc",
        year: 2016,
        track_count: 18,
        format: "FLAC",
        bitrate_kbps: 1000,
        folder: "/music/10cc",
        tracks: [],
      },
    ],
    merge_preview: null,
    ...overrides,
  };
}

describe("DuplicateComparison", () => {
  test("renders the track comparison table when a merge preview is present", () => {
    const prompt = makePrompt({
      merge_preview: {
        total: 2,
        in_library_count: 1,
        added_count: 1,
        upgrade_count: 0,
        missing_count: 0,
        rows: [
          {
            position: 1,
            disc: 1,
            title: "Kept",
            state: "library_only",
            library_format: "FLAC",
            library_bitrate_kbps: 1000,
            import_format: null,
            import_bitrate_kbps: null,
          },
          {
            position: 2,
            disc: 1,
            title: "Folded In",
            state: "added",
            library_format: null,
            library_bitrate_kbps: null,
            import_format: "FLAC",
            import_bitrate_kbps: 1000,
          },
        ],
      },
    });
    render(<DuplicateComparison prompt={prompt} incomingCoverUrl={null} />);
    expect(screen.getByText("Track comparison")).toBeInTheDocument();
    expect(screen.getByText("Folded In")).toBeInTheDocument();
  });

  test("omits the table for an as-is duplicate (no merge preview)", () => {
    render(<DuplicateComparison prompt={makePrompt()} incomingCoverUrl={null} />);
    expect(screen.queryByText("Track comparison")).not.toBeInTheDocument();
  });

  test("shows each side's release identity so 'same release?' is answerable", () => {
    const base = makePrompt();
    const prompt = {
      ...base,
      incoming: {
        ...base.incoming,
        release: {
          data_source: "Deezer",
          label: null,
          country: null,
          media: null,
          disambiguation: "2016 reissue",
          release_url: "https://www.deezer.com/album/9",
        },
      },
      existing: [
        {
          ...base.existing[0],
          release: {
            data_source: "MusicBrainz",
            label: "Warner Bros.",
            country: "US",
            media: '12" Vinyl',
            disambiguation: null,
            release_url: "https://musicbrainz.org/release/m1",
          },
        },
      ],
    };
    render(<DuplicateComparison prompt={prompt} incomingCoverUrl={null} />);
    // incoming = a different release than the existing copy
    expect(screen.getByText("Deezer")).toBeInTheDocument();
    expect(screen.getByText("2016 reissue")).toBeInTheDocument();
    // existing = the library copy's MusicBrainz edition
    expect(screen.getByText('MusicBrainz · 12" Vinyl · US')).toBeInTheDocument();
    expect(screen.getByText("Warner Bros.")).toBeInTheDocument();
    // both link out to their release page
    expect(screen.getAllByRole("link", { name: /view release/i })).toHaveLength(2);
  });
});

describe("DuplicateActions", () => {
  test("the four actions are peer-weighted — none carries the primary accent", () => {
    render(<DuplicateActions pending={null} busy={false} onDecide={vi.fn()} />);
    for (const name of [/skip new/i, /keep both/i, /replace old/i, /merge/i]) {
      const button = screen.getByRole("button", { name });
      // shadcn's Button reflects its variant onto data-variant; none of the
      // four is preferred, so all use the neutral outline variant (Replace old
      // keeps only its amber caution tint on top of outline).
      expect(button).toHaveAttribute("data-variant", "outline");
    }
    // The accent (default) variant paints bg-primary — Merge must not.
    expect(screen.getByRole("button", { name: /merge/i })).not.toHaveClass(
      "bg-primary",
    );
  });

  test("the live footnote promises Merge reappears as a normal review", () => {
    render(<DuplicateActions pending={null} busy={false} onDecide={vi.fn()} />);
    expect(
      screen.getByText(/reappears as a normal review/i),
    ).toBeInTheDocument();
  });

  test("the bank footnote drops the re-review promise (merge is terminal there)", () => {
    render(
      <DuplicateActions
        pending={null}
        busy={false}
        onDecide={vi.fn()}
        context="bank"
      />,
    );
    expect(
      screen.queryByText(/reappears as a normal review/i),
    ).not.toBeInTheDocument();
    expect(
      screen.getByText(/combines them into your library/i),
    ).toBeInTheDocument();
  });
});

describe("DuplicateActionRow", () => {
  test("DuplicateActionRow points aria-describedby at the given id", () => {
    render(
      <DuplicateActionRow pending={null} busy={false} onDecide={vi.fn()} describedBy="x1" />,
    );
    expect(screen.getByRole("button", { name: /skip new/i })).toHaveAttribute(
      "aria-describedby",
      "x1",
    );
  });
});
