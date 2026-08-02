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

const settings = { enabled: true };
vi.mock("@/api/useArtistImage", () => ({
  ARTIST_IMAGE_SETTINGS_KEY: ["artist-image", "settings"],
  useArtistImageSettings: () => ({ data: settings }),
  useUploadArtistImageOverride: () => ({
    mutate: vi.fn(),
    isPending: false,
    isError: false,
    error: null,
  }),
  useResetArtistImageOverride: () => ({
    mutate: vi.fn(),
    isPending: false,
    isError: false,
    error: null,
  }),
  useSetArtistImageFromUrl: () => ({
    mutate: vi.fn(),
    isPending: false,
    isError: false,
    error: null,
  }),
}));

// The reorganize header control runs a live status useQuery; stub it so this
// raw render (no QueryClientProvider) doesn't crash. Idle status + no-op
// mutations keep the control inert and out of the way of this suite.
vi.mock("@/api/useReorganize", () => ({
  useReorganizeStatus: () => ({ data: { phase: "idle", scope: null, artist: null, album_id: null } }),
  usePreviewReorganize: () => ({ mutate: vi.fn(), isPending: false }),
  useStartReorganize: () => ({ mutate: vi.fn(), isPending: false }),
  useStopReorganize: () => ({ mutate: vi.fn(), isPending: false }),
  useDismissReorganize: () => ({ mutate: vi.fn(), isPending: false }),
}));

// This suite covers the artist-image edit flow; keep the orthogonal artist-art
// write toggle OFF so its header action doesn't render here.
vi.mock("@/api/useArtistArt", () => ({
  useArtistArtSettings: () => ({ data: { enabled: false } }),
  useArtistArtBackfillStatus: () => ({ data: { phase: "idle", artist: null } }),
  useStartArtistArtApply: () => ({
    mutate: vi.fn(),
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
  settings.enabled = true;
});

describe("ArtistAlbumsPage artist-image edit", () => {
  it("shows the Edit button and opens the panel when images are enabled", () => {
    renderAt("ABBA");
    const btn = screen.getByRole("button", { name: /edit artist image/i });
    fireEvent.click(btn);
    // Assert the panel's distinctive copy (both the button AND the panel section
    // carry aria-label "Edit artist image", like CoverEditPanel — so target text).
    expect(screen.getByText(/upload a custom portrait/i)).toBeInTheDocument();
  });

  it("hides the Edit button when images are disabled", () => {
    settings.enabled = false;
    renderAt("ABBA");
    expect(
      screen.queryByRole("button", { name: /edit artist image/i }),
    ).toBeNull();
  });

  it("closing the panel from inside (Cancel) returns focus to the toggle", () => {
    renderAt("ABBA");
    const btn = screen.getByRole("button", { name: /edit artist image/i });
    fireEvent.click(btn);
    expect(screen.getByText(/upload a custom portrait/i)).toBeInTheDocument();
    // The in-panel Cancel unmounts the focused button — focus must come back
    // to the disclosure toggle instead of dropping to <body>.
    fireEvent.click(screen.getByRole("button", { name: /cancel/i }));
    expect(screen.queryByText(/upload a custom portrait/i)).toBeNull();
    expect(btn).toHaveFocus();
  });
});
