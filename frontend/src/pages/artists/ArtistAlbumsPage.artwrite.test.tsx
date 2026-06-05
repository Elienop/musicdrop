import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ArtistAlbumsPage } from "@/pages/artists/ArtistAlbumsPage";

vi.mock("@/api/useAlbums", () => ({
  useAlbums: () => ({
    data: { items: [], total: 0 },
    isPending: false,
    isError: false,
    isFetching: false,
    refetch: vi.fn(),
  }),
}));

// Artist-image edit lives behind its own toggle; keep it OFF so the only header
// button under test is "Apply to library".
vi.mock("@/api/useArtistImage", () => ({
  ARTIST_IMAGE_SETTINGS_KEY: ["artist-image", "settings"],
  useArtistImageSettings: () => ({ data: { enabled: false } }),
}));

// The reorganize header control runs a live status useQuery; stub it so this
// raw render (no QueryClientProvider) doesn't crash. Idle status + no-op
// mutations keep the control inert and out of the way of this suite.
vi.mock("@/api/useReorganize", () => ({
  useReorganizeStatus: () => ({ data: { phase: "idle", artist: null, album_id: null } }),
  usePreviewReorganize: () => ({ mutate: vi.fn(), isPending: false }),
  useStartReorganize: () => ({ mutate: vi.fn(), isPending: false }),
  useStopReorganize: () => ({ mutate: vi.fn(), isPending: false }),
}));

const writeSettings = { enabled: true };
const applyMutate = vi.fn();
const backfillStatus: {
  phase: string;
  artist: string | null;
  processed: number;
  total: number;
  written: number;
  skipped: number;
  failed: number;
} = {
  phase: "idle",
  artist: null,
  processed: 0,
  total: 0,
  written: 0,
  skipped: 0,
  failed: 0,
};

vi.mock("@/api/useArtistArt", () => ({
  useArtistArtSettings: () => ({ data: writeSettings }),
  useArtistArtBackfillStatus: () => ({ data: backfillStatus }),
  useStartArtistArtApply: () => ({
    mutate: applyMutate,
    isPending: false,
    isError: false,
    error: null,
  }),
}));

function renderAt(name: string) {
  return render(
    <MemoryRouter initialEntries={[`/artists/${name}`]}>
      <Routes>
        <Route path="/artists/:artistName" element={<ArtistAlbumsPage />} />
        <Route path="/" element={<div>home</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

afterEach(() => {
  writeSettings.enabled = true;
  applyMutate.mockClear();
  backfillStatus.phase = "idle";
  backfillStatus.artist = null;
  backfillStatus.processed = 0;
  backfillStatus.total = 0;
  backfillStatus.written = 0;
  backfillStatus.skipped = 0;
  backfillStatus.failed = 0;
});

describe("ArtistAlbumsPage artist-art apply", () => {
  it("shows the Apply to library button and starts the job when write is enabled", () => {
    renderAt("ABBA");
    const btn = screen.getByRole("button", { name: /apply to library/i });
    fireEvent.click(btn);
    expect(applyMutate).toHaveBeenCalledTimes(1);
  });

  it("hides the Apply to library button when write is disabled", () => {
    writeSettings.enabled = false;
    renderAt("ABBA");
    expect(
      screen.queryByRole("button", { name: /apply to library/i }),
    ).toBeNull();
  });

  it("shows marching progress while this artist owns the running job", () => {
    backfillStatus.phase = "running";
    backfillStatus.artist = "ABBA";
    backfillStatus.processed = 1;
    backfillStatus.total = 3;
    renderAt("ABBA");
    expect(screen.getByText(/writing artist art/i)).toBeInTheDocument();
    expect(screen.getByText(/1 \/ 3/)).toBeInTheDocument();
  });

  it("shows a terminal tally when this artist's job finishes", () => {
    backfillStatus.phase = "done";
    backfillStatus.artist = "ABBA";
    backfillStatus.processed = 1;
    backfillStatus.total = 1;
    backfillStatus.written = 1;
    renderAt("ABBA");
    expect(screen.getByText(/1 written/i)).toBeInTheDocument();
  });
});
