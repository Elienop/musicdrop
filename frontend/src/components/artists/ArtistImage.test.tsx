import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import type { ReactElement } from "react";
import { describe, expect, it } from "vitest";

import { ArtistImage } from "@/components/artists/ArtistImage";
import { ARTIST_ART_SETTINGS_KEY } from "@/api/useArtistArt";
import { ARTIST_IMAGE_SETTINGS_KEY } from "@/api/useArtistImage";

function renderImage(
  ui: ReactElement,
  { enabled, artEnabled }: { enabled?: boolean; artEnabled?: boolean } = {},
) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  if (enabled !== undefined) qc.setQueryData(ARTIST_IMAGE_SETTINGS_KEY, { enabled });
  // Default the write toggle to OFF so the "disabled" cases (image off) actually
  // short-circuit; tests that exercise the OR pass artEnabled explicitly.
  qc.setQueryData(ARTIST_ART_SETTINGS_KEY, { enabled: artEnabled ?? false });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

describe("ArtistImage", () => {
  it("requests the portrait when enabled", () => {
    const { container } = renderImage(<ArtistImage name="ABBA" />, { enabled: true });
    const img = container.querySelector("img");
    expect(img).not.toBeNull();
    expect(img?.getAttribute("src")).toContain("/api/artists/image?name=ABBA");
  });

  it("shows the monogram and makes NO request when disabled", () => {
    const { container } = renderImage(<ArtistImage name="ABBA" />, { enabled: false });
    expect(container.querySelector("img")).toBeNull();
    expect(screen.getByText("A")).toBeInTheDocument();
  });

  it("falls back to the monogram on image error", () => {
    const { container } = renderImage(<ArtistImage name="Beyoncé" />, { enabled: true });
    const img = container.querySelector("img");
    expect(img).not.toBeNull();
    fireEvent.error(img as HTMLImageElement);
    expect(screen.getByText("B")).toBeInTheDocument();
  });

  it("appends &v= when a version is given", () => {
    const { container } = renderImage(<ArtistImage name="ABBA" version={3} />, { enabled: true });
    expect(container.querySelector("img")?.getAttribute("src")).toContain("&v=3");
  });

  it("requests the portrait when ONLY the write toggle is on (the OR)", () => {
    const { container } = renderImage(<ArtistImage name="ABBA" />, {
      enabled: false,
      artEnabled: true,
    });
    expect(container.querySelector("img")).not.toBeNull();
  });
});
