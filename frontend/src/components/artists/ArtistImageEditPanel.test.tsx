import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ArtistImageEditPanel } from "@/components/artists/ArtistImageEditPanel";

const uploadMutate = vi.fn();
const resetMutate = vi.fn();
const setFromUrlMutate = vi.fn();

vi.mock("@/api/useArtistImage", () => ({
  useUploadArtistImageOverride: () => ({ mutate: uploadMutate, isPending: false, isError: false, error: null }),
  useResetArtistImage: () => ({ mutate: resetMutate, isPending: false, isError: false, error: null }),
  useSetArtistImageFromUrl: () => ({ mutate: setFromUrlMutate, isPending: false, isError: false, error: null }),
}));

beforeEach(() => {
  // jsdom lacks object-URL APIs.
  vi.stubGlobal("URL", { ...URL, createObjectURL: () => "blob:x", revokeObjectURL: () => {} });
});
afterEach(() => {
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

describe("ArtistImageEditPanel", () => {
  it("uploads a picked image", async () => {
    render(<ArtistImageEditPanel name="ABBA" onSaved={() => {}} onClose={() => {}} />);
    const input = screen.getByLabelText(/upload artist image/i);
    const file = new File([new Uint8Array([1, 2, 3])], "p.png", { type: "image/png" });
    fireEvent.change(input, { target: { files: [file] } });
    fireEvent.click(await screen.findByRole("button", { name: /use this image/i }));
    expect(uploadMutate).toHaveBeenCalled();
  });

  it("rejects a non-image file inline", () => {
    render(<ArtistImageEditPanel name="ABBA" onSaved={() => {}} onClose={() => {}} />);
    const input = screen.getByLabelText(/upload artist image/i);
    const file = new File(["x"], "x.txt", { type: "text/plain" });
    fireEvent.change(input, { target: { files: [file] } });
    expect(screen.getByRole("alert")).toHaveTextContent(/png, jpeg, gif, or webp/i);
    expect(uploadMutate).not.toHaveBeenCalled();
  });

  it("resets to auto", () => {
    render(<ArtistImageEditPanel name="ABBA" onSaved={() => {}} onClose={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: /reset to auto/i }));
    expect(resetMutate).toHaveBeenCalled();
  });

  it("sets the image from a pasted URL", () => {
    render(<ArtistImageEditPanel name="ABBA" onSaved={() => {}} onClose={() => {}} />);
    fireEvent.change(screen.getByLabelText(/image url/i), {
      target: { value: "https://example.test/a.jpg" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^set$/i }));
    expect(setFromUrlMutate).toHaveBeenCalledWith("https://example.test/a.jpg", expect.anything());
  });
});
