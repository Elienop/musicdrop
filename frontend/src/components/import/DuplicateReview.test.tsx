import { render, screen } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";

import type { DuplicatePrompt } from "@/api/useImport";
import { DuplicateActions, DuplicateComparison } from "@/components/import/DuplicateReview";

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
