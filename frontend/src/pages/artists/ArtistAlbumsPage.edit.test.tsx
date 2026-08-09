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
  useResetArtistImage: () => ({
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
  // The panel imports these too, and a factory REPLACES the module — a missing
  // export fails this whole file, not just the panel.
  useFetchArtistImage: () => ({
    mutate: vi.fn(),
    isPending: false,
    isError: false,
    error: null,
    reset: vi.fn(),
  }),
  useArtistImageSources: () => ({
    data: { sources: [{ id: "deezer", label: "Deezer", available: true, reason: null }] },
    isPending: false,
    isError: false,
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

// This suite covers the artist-image edit flow; the orthogonal artist-art write
// toggle defaults OFF so its header action doesn't render here — but it is
// mutable, because it is half of the predicate that decides whether the edit
// button exists at all.
const artSettings = { enabled: false };
vi.mock("@/api/useArtistArt", () => ({
  useArtistArtSettings: () => ({ data: artSettings }),
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
  artSettings.enabled = false;
});

describe("ArtistAlbumsPage artist-image edit", () => {
  it("shows the Edit button and opens the panel when images are enabled", () => {
    renderAt("ABBA");
    const btn = screen.getByRole("button", { name: /edit artist image/i });
    fireEvent.click(btn);
    // Assert the panel's distinctive copy (both the button AND the panel section
    // carry aria-label "Edit artist image", like CoverEditPanel — so target text).
    expect(screen.getByText(/choose the portrait for/i)).toBeInTheDocument();
  });

  it("hides the Edit button only when BOTH toggles are off", () => {
    settings.enabled = false;
    artSettings.enabled = false;
    renderAt("ABBA");
    expect(
      screen.queryByRole("button", { name: /edit artist image/i }),
    ).toBeNull();
  });

  it("still offers the Edit button when only the write-to-library toggle is on", () => {
    // The backend accepts a fetch on image-toggle OR write-toggle. Gating the
    // way in on the image toggle alone left this combination with a painted
    // portrait, a route that accepts, and no way to reach either.
    settings.enabled = false;
    artSettings.enabled = true;
    renderAt("ABBA");
    expect(screen.getByRole("button", { name: /edit artist image/i })).toBeInTheDocument();
  });

  it("closing the panel from inside (Cancel) returns focus to the toggle", () => {
    renderAt("ABBA");
    const btn = screen.getByRole("button", { name: /edit artist image/i });
    fireEvent.click(btn);
    expect(screen.getByText(/choose the portrait for/i)).toBeInTheDocument();
    // The in-panel Cancel unmounts the focused button — focus must come back
    // to the disclosure toggle instead of dropping to <body>.
    fireEvent.click(screen.getByRole("button", { name: /cancel/i }));
    expect(screen.queryByText(/choose the portrait for/i)).toBeNull();
    expect(btn).toHaveFocus();
  });
});
