import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ArtistImagesPanel } from "@/pages/settings/ArtistImagesPanel";

const setEnabled = vi.fn();

vi.mock("@/api/useArtistImage", () => ({
  useArtistImageSettings: () => ({ data: { enabled: false }, isPending: false }),
  useSetArtistImageSettings: () => ({
    mutate: setEnabled,
    isPending: false,
    isError: false,
    error: null,
  }),
}));

afterEach(() => vi.clearAllMocks());

describe("ArtistImagesPanel", () => {
  it("renders the current state and toggles", () => {
    render(<ArtistImagesPanel />);
    expect(screen.getByRole("heading", { name: "Artist images" })).toBeInTheDocument();
    const toggle = screen.getByRole("switch", { name: /enable artist images/i });
    expect(toggle).not.toBeChecked();
    fireEvent.click(toggle);
    expect(setEnabled).toHaveBeenCalledWith(true);
  });
});
