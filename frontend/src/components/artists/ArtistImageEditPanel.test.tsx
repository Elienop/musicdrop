import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ArtistImageEditPanel } from "@/components/artists/ArtistImageEditPanel";

const uploadMutate = vi.fn();
const resetMutate = vi.fn();
const setFromUrlMutate = vi.fn();
const fetchMutate = vi.fn();
const fetchReset = vi.fn();

// Mutable so a test can flip a toggle without a second mock factory.
const imageSettings = { enabled: true };
const artSettings = { enabled: false };

const sourcesResult = {
  data: {
    sources: [
      {
        id: "fanarttv",
        label: "fanart.tv",
        available: false,
        reason: "No MusicBrainz ID for this artist, so fanart.tv cannot be searched",
      },
      { id: "spotify", label: "Spotify", available: true, reason: null },
      { id: "deezer", label: "Deezer", available: true, reason: null },
    ],
  },
  isPending: false,
  isError: false,
};
const noSourcesResult = { data: undefined, isPending: false, isError: false };
/** Every (name, enabled) pair the panel asked the sources query for. */
const sourcesCalls: Array<{ name: string; enabled: boolean }> = [];

// A factory REPLACES the module, so it must list every export the component
// imports — a missing one fails this whole file, not one test.
vi.mock("@/api/useArtistImage", () => ({
  useArtistImageSettings: () => ({ data: imageSettings }),
  useUploadArtistImageOverride: () => ({
    mutate: uploadMutate,
    isPending: false,
    isError: false,
    error: null,
  }),
  useResetArtistImage: () => ({
    mutate: resetMutate,
    isPending: false,
    isError: false,
    error: null,
  }),
  useSetArtistImageFromUrl: () => ({
    mutate: setFromUrlMutate,
    isPending: false,
    isError: false,
    error: null,
  }),
  useFetchArtistImage: () => ({
    mutate: fetchMutate,
    isPending: false,
    isError: false,
    error: null,
    reset: fetchReset,
  }),
  // Honours `enabled` like the real hook does, so "no source row when the
  // feature is off" cannot pass just because the list was handed over anyway.
  useArtistImageSources: (name: string, enabled = true) => {
    sourcesCalls.push({ name, enabled });
    return enabled ? sourcesResult : noSourcesResult;
  },
}));

// The panel composes the SAME pair the fetch route gates on (image toggle OR
// write-to-library toggle), so the art module is in the panel's import graph.
vi.mock("@/api/useArtistArt", () => ({
  useArtistArtSettings: () => ({ data: artSettings }),
}));

const RealURL = globalThis.URL;
let revoked: string[] = [];
let minted = 0;
beforeEach(() => {
  // jsdom lacks object-URL APIs.
  revoked = [];
  sourcesCalls.length = 0;
  minted = 0;
  // Unique per mint, so "the FIRST preview's URL was released" is a real claim
  // rather than one satisfied by any revoke at all.
  vi.stubGlobal("URL", {
    ...RealURL,
    createObjectURL: () => `blob:${++minted}`,
    revokeObjectURL: (url: string) => revoked.push(url),
  });
});
afterEach(() => {
  // Restore only URL: `vi.unstubAllGlobals()` would also drop test/setup.ts's
  // scrollTo/matchMedia/EventSource stubs for the rest of this file.
  vi.stubGlobal("URL", RealURL);
  // reset, not clear: clearAllMocks keeps per-test mockImplementations, which
  // would leak a fetch result into the next test in file order.
  vi.resetAllMocks();
  imageSettings.enabled = true;
  artSettings.enabled = false;
});

/** A fetch that answers with a portrait. The hook hands back BYTES only — the
 * panel mints the object URL — so the fixture carries no `objectUrl`. */
function fetchReturnsPortrait(blob: Blob, source: string | null = "Deezer") {
  fetchMutate.mockImplementation(
    (_source: string, opts: { onSuccess: (r: unknown) => void }) =>
      opts.onSuccess({ found: true, blob, source }),
  );
}

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

  it("offers only the sources that can answer for this artist", () => {
    render(<ArtistImageEditPanel name="ABBA" onSaved={() => {}} onClose={() => {}} />);
    const group = screen.getByRole("group", { name: /image source/i });
    expect(within(group).getByRole("button", { name: "Deezer" })).toBeInTheDocument();
    expect(within(group).getByRole("button", { name: "Spotify" })).toBeInTheDocument();
    expect(within(group).queryByRole("button", { name: "fanart.tv" })).toBeNull();
  });

  it("explains a configured source that cannot answer instead of hiding it", () => {
    render(<ArtistImageEditPanel name="ABBA" onSaved={() => {}} onClose={() => {}} />);
    expect(screen.getByText(/no musicbrainz id for this artist/i)).toBeInTheDocument();
  });

  it("fetches from the picked source", () => {
    render(<ArtistImageEditPanel name="ABBA" onSaved={() => {}} onClose={() => {}} />);
    // Deezer, NOT Spotify: Spotify is already the default, so picking it would
    // prove nothing about the pick actually being read.
    fireEvent.click(screen.getByRole("button", { name: "Deezer" }));
    expect(screen.getByRole("button", { name: "Deezer" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    fireEvent.click(screen.getByRole("button", { name: /^fetch$/i }));
    expect(fetchMutate).toHaveBeenCalledWith("deezer", expect.anything());
  });

  it("defaults to the first source the chain would try", () => {
    render(<ArtistImageEditPanel name="ABBA" onSaved={() => {}} onClose={() => {}} />);
    expect(screen.getByRole("button", { name: "Spotify" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    fireEvent.click(screen.getByRole("button", { name: /^fetch$/i }));
    expect(fetchMutate).toHaveBeenCalledWith("spotify", expect.anything());
  });

  it("drops one source's answer when another is picked", () => {
    fetchMutate.mockImplementation(
      (_source: string, opts: { onSuccess: (r: unknown) => void }) =>
        opts.onSuccess({ found: false, reason: "Spotify has no portrait for ABBA" }),
    );
    render(<ArtistImageEditPanel name="ABBA" onSaved={() => {}} onClose={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: /^fetch$/i }));
    expect(screen.getByText(/spotify has no portrait/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Deezer" }));
    expect(screen.queryByText(/spotify has no portrait/i)).toBeNull();
  });

  it("previews a fetched portrait and installs THOSE bytes", () => {
    const blob = new Blob([new Uint8Array([1, 2, 3])], { type: "image/jpeg" });
    fetchReturnsPortrait(blob);
    render(<ArtistImageEditPanel name="ABBA" onSaved={() => {}} onClose={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: /^fetch$/i }));
    // Anchored: the live region announces "Found a portrait from Deezer." at the
    // same moment, and an unanchored /from deezer/ matches both.
    expect(screen.getByText(/^from deezer$/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /use this image/i }));
    // The very blob that was previewed — never a re-fetch that could differ.
    // `toBe`, NOT `toHaveBeenCalledWith(blob)`: jsdom hides all Blob state
    // behind one non-enumerated `Symbol(impl)`, so two jsdom Blobs deep-equal
    // each other and vitest matches `new Blob([])` against these three bytes —
    // measured, and it let an "install different bytes" mutant survive. Plain
    // Node Blobs carry own symbols (`kHandle`/`kLength`/`kType`) and do NOT
    // deep-equal, so a probe run outside jsdom wrongly reassures.
    expect(uploadMutate.mock.calls[0]?.[0]).toBe(blob);
    expect(uploadMutate.mock.calls).toHaveLength(1);
  });

  it("says a source had nothing rather than showing an empty preview", () => {
    fetchMutate.mockImplementation(
      (_source: string, opts: { onSuccess: (r: unknown) => void }) =>
        opts.onSuccess({ found: false, reason: "Deezer has no portrait for ABBA" }),
    );
    render(<ArtistImageEditPanel name="ABBA" onSaved={() => {}} onClose={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: /^fetch$/i }));
    expect(screen.getByText(/deezer has no portrait for abba/i)).toBeInTheDocument();
    expect(screen.queryByAltText(/artist image preview/i)).toBeNull();
  });

  it("releases a rejected preview's object URL instead of leaking it", () => {
    fetchReturnsPortrait(new Blob([new Uint8Array([1])], { type: "image/jpeg" }));
    render(<ArtistImageEditPanel name="ABBA" onSaved={() => {}} onClose={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: /^fetch$/i }));
    expect(revoked).not.toContain("blob:1");
    // Discarding the preview is the common path — it must not strand the URL.
    fireEvent.click(screen.getByRole("button", { name: /discard/i }));
    expect(revoked).toContain("blob:1");
  });

  it("releases the live preview's object URL when the panel unmounts", () => {
    fetchReturnsPortrait(new Blob([new Uint8Array([1])], { type: "image/jpeg" }));
    const view = render(
      <ArtistImageEditPanel name="ABBA" onSaved={() => {}} onClose={() => {}} />,
    );
    fireEvent.click(screen.getByRole("button", { name: /^fetch$/i }));
    view.unmount();
    expect(revoked).toContain("blob:1");
  });

  it("keeps its live region mounted before there is anything to announce", () => {
    // A region inserted in the same commit as its text is not reliably
    // announced, so "the element exists after the event" is not the invariant —
    // "the element existed BEFORE the event" is.
    render(<ArtistImageEditPanel name="ABBA" onSaved={() => {}} onClose={() => {}} />);
    const region = screen.getByRole("status");
    expect(region).toHaveTextContent("");

    fetchReturnsPortrait(new Blob([new Uint8Array([1])], { type: "image/jpeg" }));
    fireEvent.click(screen.getByRole("button", { name: /^fetch$/i }));
    // The very same node now carries the text — not a replacement node.
    expect(screen.getByRole("status")).toBe(region);
    expect(region).toHaveTextContent(/from deezer/i);
  });

  it("moves focus onto the arrived portrait, and back when it is discarded", () => {
    fetchReturnsPortrait(new Blob([new Uint8Array([1])], { type: "image/jpeg" }));
    render(<ArtistImageEditPanel name="ABBA" onSaved={() => {}} onClose={() => {}} />);
    const fetchButton = screen.getByRole("button", { name: /^fetch$/i });
    fetchButton.focus();
    fireEvent.click(fetchButton);
    // Fetch unmounts the button that was just pressed; focus must not fall to
    // <body>, where the next Tab restarts from the top of the document.
    expect(document.body).not.toHaveFocus();
    expect(screen.getByAltText(/artist image preview/i).closest("[tabindex]")).toHaveFocus();

    fireEvent.click(screen.getByRole("button", { name: /discard/i }));
    expect(screen.getByRole("button", { name: /^fetch$/i })).toHaveFocus();
  });

  it("names the source it asked when the server cannot expose the header", () => {
    // X-Art-Source is unreadable cross-origin, so `source` arrives null on any
    // deployment that does not proxy /api. The panel chose the source, so it
    // can always say which one.
    fetchReturnsPortrait(new Blob([new Uint8Array([1])], { type: "image/jpeg" }), null);
    render(<ArtistImageEditPanel name="ABBA" onSaved={() => {}} onClose={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: "Deezer" }));
    fireEvent.click(screen.getByRole("button", { name: /^fetch$/i }));
    expect(screen.getByText(/^from deezer$/i)).toBeInTheDocument();
  });

  it("does not carry one artist's preview onto another", () => {
    // The route is `/artists/:artistName` with no key, so navigating between
    // two artists (or pressing Back) re-renders this SAME instance with a new
    // name. A preview that survives that would be installed against the artist
    // now on screen — bytes the user never saw for that artist.
    const blob = new Blob([new Uint8Array([1])], { type: "image/jpeg" });
    fetchReturnsPortrait(blob);
    const view = render(<ArtistImageEditPanel name="ABBA" onSaved={() => {}} onClose={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: /^fetch$/i }));
    expect(screen.getByAltText(/artist image preview/i)).toBeInTheDocument();

    view.rerender(<ArtistImageEditPanel name="Blondie" onSaved={() => {}} onClose={() => {}} />);
    expect(screen.getByText(/choose the portrait for blondie/i)).toBeInTheDocument();
    expect(screen.queryByAltText(/artist image preview/i)).toBeNull();
    expect(screen.queryByRole("button", { name: /use this image/i })).toBeNull();
    // ...and the abandoned candidate's URL goes with it.
    expect(revoked).toContain("blob:1");
  });

  it("reports what the reset actually cleared", () => {
    resetMutate.mockImplementation(
      (_v: undefined, opts: { onSuccess: (r: unknown) => void }) =>
        opts.onSuccess({ ok: true, cleared_override: true, cleared_auto: true }),
    );
    const onSaved = vi.fn();
    render(<ArtistImageEditPanel name="ABBA" onSaved={onSaved} onClose={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: /reset to auto/i }));
    expect(onSaved).toHaveBeenCalled();
    expect(screen.getByRole("status")).toHaveTextContent(/looked up again/i);
    // Nothing is left to abandon, so the closing button stops saying "Cancel".
    expect(screen.getByRole("button", { name: /^done$/i })).toBeInTheDocument();
  });

  it("clears a stale outcome when another action starts", () => {
    resetMutate.mockImplementation(
      (_v: undefined, opts: { onSuccess: (r: unknown) => void }) =>
        opts.onSuccess({ ok: true, cleared_override: true, cleared_auto: true }),
    );
    render(<ArtistImageEditPanel name="ABBA" onSaved={() => {}} onClose={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: /reset to auto/i }));
    expect(screen.getByRole("status")).toHaveTextContent(/looked up again/i);
    // Submitting the pasted link must not leave the reset's outcome standing
    // over it as if it described what just happened.
    fireEvent.change(screen.getByLabelText(/image url/i), {
      target: { value: "https://example.test/a.jpg" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^set$/i }));
    expect(screen.getByRole("status")).toHaveTextContent("");
  });

  it("does not call a both-false reset a no-op", () => {
    // An unwritable cache dir swallows the unlink while the in-memory entry is
    // dropped, so `false, false` can still mean what is served changed. The
    // copy must not read as "already clear".
    resetMutate.mockImplementation(
      (_v: undefined, opts: { onSuccess: (r: unknown) => void }) =>
        opts.onSuccess({ ok: true, cleared_override: false, cleared_auto: false }),
    );
    render(<ArtistImageEditPanel name="ABBA" onSaved={() => {}} onClose={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: /reset to auto/i }));
    const note = screen.getByRole("status");
    expect(note).toHaveTextContent(/looked up again/i);
    expect(note.textContent).not.toMatch(/nothing|already|no-op/i);
  });

  it("offers no fetch at all when both artist-image toggles are off", () => {
    // The route gates on image-toggle OR write-toggle; showing Fetch with both
    // off ships a button whose only possible answer is a 403.
    imageSettings.enabled = false;
    artSettings.enabled = false;
    render(<ArtistImageEditPanel name="ABBA" onSaved={() => {}} onClose={() => {}} />);
    expect(screen.queryByRole("group", { name: /image source/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /^fetch$/i })).toBeNull();
    // ...and the upload/reset half still works.
    expect(screen.getByRole("button", { name: /reset to auto/i })).toBeInTheDocument();
  });

  it("asks the sources endpoint nothing about an empty artist name", () => {
    // The server declares `min_length=1` and the hook has no guard, so an empty
    // name must not become a request at all.
    render(<ArtistImageEditPanel name="" onSaved={() => {}} onClose={() => {}} />);
    expect(sourcesCalls.at(-1)).toEqual({ name: "", enabled: false });
    expect(screen.queryByRole("group", { name: /image source/i })).toBeNull();
  });

  it("still offers fetch when only the write-to-library toggle is on", () => {
    imageSettings.enabled = false;
    artSettings.enabled = true;
    render(<ArtistImageEditPanel name="ABBA" onSaved={() => {}} onClose={() => {}} />);
    expect(screen.getByRole("button", { name: /^fetch$/i })).toBeInTheDocument();
  });
});
