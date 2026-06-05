import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
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
  useReorganizeStatus: () => ({ data: { phase: "idle", scope: null, artist: null, album_id: null } }),
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
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[`/artists/${name}`]}>
        <Routes>
          <Route path="/artists/:artistName" element={<ArtistAlbumsPage />} />
          <Route path="/" element={<div>home</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
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
  it("shows the Write artist art button and starts the job when write is enabled", () => {
    renderAt("ABBA");
    const btn = screen.getByRole("button", { name: /write artist art/i });
    fireEvent.click(btn);
    expect(applyMutate).toHaveBeenCalledTimes(1);
  });

  it("hides the Write artist art button when write is disabled", () => {
    writeSettings.enabled = false;
    renderAt("ABBA");
    expect(
      screen.queryByRole("button", { name: /write artist art/i }),
    ).toBeNull();
  });

  it("disables the button while this artist's job runs (progress is in the app banner)", () => {
    backfillStatus.phase = "running";
    backfillStatus.artist = "ABBA";
    backfillStatus.processed = 1;
    backfillStatus.total = 3;
    renderAt("ABBA");
    expect(screen.getByRole("button", { name: /write artist art/i })).toBeDisabled();
    // No inline progress — it must not duplicate the top app banner.
    expect(screen.queryByText(/1 \/ 3/)).toBeNull();
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
