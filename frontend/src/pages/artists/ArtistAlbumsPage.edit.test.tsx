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
  useUploadArtistImageOverride: () => ({ mutate: vi.fn(), isPending: false, isError: false, error: null }),
  useResetArtistImageOverride: () => ({ mutate: vi.fn(), isPending: false, isError: false, error: null }),
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
    expect(screen.queryByRole("button", { name: /edit artist image/i })).toBeNull();
  });
});
