import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { MergePreview } from "@/api/useImport";
import { MergePreviewTable } from "@/components/import/MergePreviewTable";

const preview: MergePreview = {
  total: 4,
  in_library_count: 1,
  added_count: 2,
  upgrade_count: 1,
  missing_count: 1,
  rows: [
    {
      position: 1,
      disc: 1,
      title: "Shared Song",
      state: "upgrade",
      library_format: "MP3",
      library_bitrate_kbps: 320,
      import_format: "FLAC",
      import_bitrate_kbps: 1000,
    },
    {
      position: 2,
      disc: 1,
      title: "Gap Song",
      state: "missing",
      library_format: null,
      library_bitrate_kbps: null,
      import_format: null,
      import_bitrate_kbps: null,
    },
    {
      position: 3,
      disc: 2,
      title: "New Song One",
      state: "added",
      library_format: null,
      library_bitrate_kbps: null,
      import_format: "FLAC",
      import_bitrate_kbps: 1000,
    },
    {
      position: 4,
      disc: 2,
      title: "New Song Two",
      state: "added",
      library_format: null,
      library_bitrate_kbps: null,
      import_format: "FLAC",
      import_bitrate_kbps: 1000,
    },
  ],
};

function rowOf(title: string): HTMLElement {
  return screen.getByText(title).closest("tr") as HTMLElement;
}

describe("MergePreviewTable", () => {
  it("labels the two sides and summarises the counts", () => {
    render(<MergePreviewTable preview={preview} />);
    expect(screen.getByText("In your library")).toBeInTheDocument();
    expect(screen.getByText("This import")).toBeInTheDocument();
    expect(screen.getByText(/2 added/i)).toBeInTheDocument();
    expect(screen.getByText(/1 still missing/i)).toBeInTheDocument();
  });

  it("flags an added track the import folds in", () => {
    render(<MergePreviewTable preview={preview} />);
    expect(within(rowOf("New Song One")).getByText(/adds/i)).toBeInTheDocument();
  });

  it("shows the per-track quality on an upgrade row", () => {
    render(<MergePreviewTable preview={preview} />);
    const row = rowOf("Shared Song");
    expect(within(row).getByText(/upgrade/i)).toBeInTheDocument();
    expect(within(row).getByText(/MP3/)).toBeInTheDocument();
    expect(within(row).getByText(/FLAC/)).toBeInTheDocument();
  });

  it("marks a track that stays missing after merge", () => {
    render(<MergePreviewTable preview={preview} />);
    expect(within(rowOf("Gap Song")).getByText(/missing/i)).toBeInTheDocument();
  });

  it("groups multi-disc tracks under disc headers", () => {
    render(<MergePreviewTable preview={preview} />);
    expect(screen.getByText(/disc 2/i)).toBeInTheDocument();
  });
});
