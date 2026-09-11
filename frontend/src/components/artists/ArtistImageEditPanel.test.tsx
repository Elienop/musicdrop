import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ArtistImageEditPanel } from "@/components/artists/ArtistImageEditPanel";

const uploadMutate = vi.fn();
const resetMutate = vi.fn();
const setFromUrlMutate = vi.fn();
const fetchMutate = vi.fn();
const fetchReset = vi.fn();
const resetReset = vi.fn();
/** Mutable for the same reason `fetchState` is: the reset now reports from
 * inside its confirm, so a test has to put the mutation in flight or in error
 * while the dialog is open. `resetReset` really clears it, so "no stale error
 * on reopen" is a claim about the component and not about the spy. */
const resetState = { isPending: false, isError: false, error: null as Error | null };
/** Mutable so a test can put the fetch mutation in its error state — and so
 * `reset()` can actually clear it, the way the real hook does. A `reset` spy
 * that changed nothing would let "the alert is gone" pass on a component that
 * never cleared it. */
const fetchState = { isPending: false, isError: false, error: null as Error | null };

// Mutable so a test can flip a toggle without a second mock factory.
const imageSettings = { enabled: true };
const artSettings = { enabled: false };

// The three shapes the REAL hook produces, measured against TanStack v5 and
// pinned by `a disabled sources query is pending but NOT loading` in
// useArtistImage.test.tsx. The disabled one matters most: `isPending` is TRUE
// while a query is disabled, so a mock reporting `isPending: false` there
// models a state that cannot happen — and every assertion downstream of it
// then passes for the wrong reason.
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
  isLoading: false,
  isError: false,
};
/** `enabled: false` — pending forever, never fetching. */
const disabledSourcesResult = {
  data: undefined,
  isPending: true,
  isLoading: false,
  isError: false,
};
/** Enabled, request in the air. */
const loadingSourcesResult = {
  data: undefined,
  isPending: true,
  isLoading: true,
  isError: false,
};
let sourcesLoading = false;
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
    isPending: resetState.isPending,
    isError: resetState.isError,
    error: resetState.error,
    reset: resetReset,
  }),
  useSetArtistImageFromUrl: () => ({
    mutate: setFromUrlMutate,
    isPending: false,
    isError: false,
    error: null,
  }),
  useFetchArtistImage: () => ({
    mutate: fetchMutate,
    isPending: fetchState.isPending,
    isError: fetchState.isError,
    error: fetchState.error,
    reset: fetchReset,
  }),
  // Honours `enabled` like the real hook does, so "no source row when the
  // feature is off" cannot pass just because the list was handed over anyway.
  useArtistImageSources: (name: string, enabled = true) => {
    sourcesCalls.push({ name, enabled });
    if (!enabled) return disabledSourcesResult;
    return sourcesLoading ? loadingSourcesResult : sourcesResult;
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
  fetchState.isPending = false;
  fetchState.isError = false;
  fetchState.error = null;
  resetState.isPending = false;
  resetState.isError = false;
  resetState.error = null;
  // Re-armed every test: afterEach uses resetAllMocks, which drops it.
  fetchReset.mockImplementation(() => {
    fetchState.isError = false;
    fetchState.error = null;
  });
  resetReset.mockImplementation(() => {
    resetState.isError = false;
    resetState.error = null;
  });
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
  sourcesLoading = false;
});

/** A fetch that answers with a portrait. The hook hands back BYTES only — the
 * panel mints the object URL — so the fixture carries no `objectUrl`. */
function fetchReturnsPortrait(blob: Blob, source: string | null = "Deezer") {
  fetchMutate.mockImplementation(
    (_source: string, opts: { onSuccess: (r: unknown) => void }) =>
      opts.onSuccess({ found: true, blob, source }),
  );
}

/** Every reset goes through the confirm now. Returns the open dialog so a test
 * can scope its own assertions to it. */
async function confirmReset() {
  fireEvent.click(screen.getByRole("button", { name: /reset to auto/i }));
  const dialog = await screen.findByRole("alertdialog");
  fireEvent.click(within(dialog).getByRole("button", { name: /^reset$/i }));
  return dialog;
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

  // The upload route never reads the declared content type — it sniffs magic
  // bytes (`sniff_image_mime`, backend/app/artwork/images.py). An
  // extensionless file reports `type: ""`, which says nothing about the bytes,
  // so refusing it here blocked a portrait the server would have installed.
  it("previews and uploads a PNG whose declared type came through blank", async () => {
    render(<ArtistImageEditPanel name="ABBA" onSaved={() => {}} onClose={() => {}} />);
    const input = screen.getByLabelText(/upload artist image/i);
    // Real PNG magic bytes, and no extension for the OS to guess from — the
    // exact pair the old `ACCEPTED_TYPES.has(file.type)` gate refused.
    const file = new File([new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10])], "portrait", {
      type: "",
    });
    expect(file.type).toBe("");
    fireEvent.change(input, { target: { files: [file] } });

    expect(await screen.findByAltText(/pending artist portrait/i)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /use this image/i }));
    expect(uploadMutate).toHaveBeenCalledWith(file, expect.anything());
  });

  it("resets to auto once the confirm is accepted", async () => {
    render(<ArtistImageEditPanel name="ABBA" onSaved={() => {}} onClose={() => {}} />);
    await confirmReset();
    expect(resetMutate).toHaveBeenCalledTimes(1);
    expect(resetMutate.mock.calls[0]?.[0]).toBeUndefined();
  });

  it("opens a confirm on Reset and sends nothing yet", async () => {
    // The reset moves an uploaded portrait to Trash and forgets the cached
    // automatic one; no endpoint says which of those applies beforehand, so the
    // dialog is shown on every click and the copy covers both cases.
    render(<ArtistImageEditPanel name="ABBA" onSaved={() => {}} onClose={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: /reset to auto/i }));

    const dialog = await screen.findByRole("alertdialog");
    expect(within(dialog).getByText("Reset to auto?")).toBeInTheDocument();
    // Verbatim (the apostrophe is U+2019): the second sentence is the only
    // warning that an uploaded image is about to move, so a paraphrase that
    // drops it is the failure this pins.
    expect(
      within(dialog).getByText(
        "Forgets this artist\u2019s portrait so it is looked up again. An image you" +
          " uploaded or linked moves to Trash first.",
      ),
    ).toBeInTheDocument();
    expect(resetMutate).not.toHaveBeenCalled();
  });

  it("keeps the confirm open, and Cancel locked, while the reset is in flight", async () => {
    // The route answers 503 when the Trash store cannot be used — nothing is
    // reset — so the dialog has to survive until the request settles. Cancel is
    // disabled, which is why Escape must be swallowed as well: a dialog Escape
    // could close leaves that sentence nowhere to report.
    resetMutate.mockImplementation(() => {
      resetState.isPending = true;
    });
    const panel = () => (
      <ArtistImageEditPanel name="ABBA" onSaved={() => {}} onClose={() => {}} />
    );
    const view = render(panel());
    await confirmReset();
    view.rerender(panel());

    const dialog = screen.getByRole("alertdialog");
    expect(within(dialog).getByRole("button", { name: /^cancel$/i })).toBeDisabled();
    expect(within(dialog).getByRole("button", { name: "Resetting…" })).toBeDisabled();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.getByRole("alertdialog")).toBeInTheDocument();
  });

  it("closes the confirm on Escape when nothing is in flight", async () => {
    // Positive control for the assertion above: without it "still open while
    // pending" also passes on a dialog Escape never reaches in jsdom.
    render(<ArtistImageEditPanel name="ABBA" onSaved={() => {}} onClose={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: /reset to auto/i }));
    await screen.findByRole("alertdialog");

    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(resetMutate).not.toHaveBeenCalled();
  });

  it("reports a failed reset inside the confirm and re-enables Cancel", async () => {
    resetMutate.mockImplementation(() => {
      resetState.isError = true;
      resetState.error = new Error(
        "The uploaded image could not be moved to Trash, so nothing was reset",
      );
    });
    const panel = () => (
      <ArtistImageEditPanel name="ABBA" onSaved={() => {}} onClose={() => {}} />
    );
    const view = render(panel());
    await confirmReset();
    view.rerender(panel());

    const dialog = screen.getByRole("alertdialog");
    const alert = within(dialog).getByRole("alert");
    expect(alert).toHaveTextContent(/nothing was reset/i);
    expect(within(dialog).getByRole("button", { name: /^cancel$/i })).toBeEnabled();
  });

  it("reopens the confirm without the last failure on it", async () => {
    // The mutation outlives the dialog, so its error state is still set at the
    // next open — the Save-art and delete confirms clear it the same way.
    resetMutate.mockImplementation(() => {
      resetState.isError = true;
      resetState.error = new Error("Trash is unusable");
    });
    const panel = () => (
      <ArtistImageEditPanel name="ABBA" onSaved={() => {}} onClose={() => {}} />
    );
    const view = render(panel());
    await confirmReset();
    view.rerender(panel());
    expect(within(screen.getByRole("alertdialog")).getByRole("alert")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /^cancel$/i }));
    view.rerender(panel());
    fireEvent.click(screen.getByRole("button", { name: /reset to auto/i }));
    expect(resetReset).toHaveBeenCalled();
    view.rerender(panel());
    const reopened = await screen.findByRole("alertdialog");
    expect(within(reopened).queryByRole("alert")).toBeNull();
  });

  it("closes the confirm when the reset succeeds", async () => {
    resetMutate.mockImplementation(
      (_v: undefined, opts: { onSuccess: (r: unknown) => void }) =>
        opts.onSuccess({ ok: true, cleared_override: true, cleared_auto: false }),
    );
    render(<ArtistImageEditPanel name="ABBA" onSaved={() => {}} onClose={() => {}} />);
    await confirmReset();
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(screen.getByRole("status")).toHaveTextContent(/looked up again/i);
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
    expect(screen.queryByAltText(/pending artist portrait/i)).toBeNull();
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
    expect(screen.getByAltText(/pending artist portrait/i).closest("[tabindex]")).toHaveFocus();

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
    expect(screen.getByAltText(/pending artist portrait/i)).toBeInTheDocument();

    view.rerender(<ArtistImageEditPanel name="Blondie" onSaved={() => {}} onClose={() => {}} />);
    expect(screen.getByText(/choose the portrait for blondie/i)).toBeInTheDocument();
    expect(screen.queryByAltText(/pending artist portrait/i)).toBeNull();
    expect(screen.queryByRole("button", { name: /use this image/i })).toBeNull();
    // ...and the abandoned candidate's URL goes with it.
    expect(revoked).toContain("blob:1");
  });

  it("reports what the reset actually cleared", async () => {
    resetMutate.mockImplementation(
      (_v: undefined, opts: { onSuccess: (r: unknown) => void }) =>
        opts.onSuccess({ ok: true, cleared_override: true, cleared_auto: true }),
    );
    const onSaved = vi.fn();
    render(<ArtistImageEditPanel name="ABBA" onSaved={onSaved} onClose={() => {}} />);
    await confirmReset();
    expect(onSaved).toHaveBeenCalled();
    expect(screen.getByRole("status")).toHaveTextContent(/looked up again/i);
    // Nothing is left to abandon, so the closing button stops saying "Cancel".
    expect(screen.getByRole("button", { name: /^done$/i })).toBeInTheDocument();
  });

  it("clears a failed fetch's alert when another action starts", async () => {
    // A red "Spotify did not answer" sat beside the reset's success line,
    // because clearNotices() cleared the panel's own notices but not the
    // mutation's error state. Two entry points, because each one used to
    // remember its own subset of what to clear.
    const failed = () => {
      fetchState.isError = true;
      fetchState.error = new Error("Spotify did not answer - try again in a moment");
    };

    // The real hook re-renders on reset(); this mock has no subscription to do
    // that, so the rerender stands in for it. A FRESH element each time — React
    // bails out of re-rendering when handed the referentially identical one, so
    // a stored `const panel` would silently render nothing. Same key, so the
    // panel is re-rendered rather than remounted: its state survives, as in the
    // app.
    const panel = () => <ArtistImageEditPanel name="ABBA" onSaved={() => {}} onClose={() => {}} />;

    failed();
    const first = render(panel());
    expect(screen.getByRole("alert")).toHaveTextContent(/did not answer/i);
    await confirmReset();
    expect(fetchReset).toHaveBeenCalled();
    first.rerender(panel());
    expect(screen.queryByRole("alert")).toBeNull();
    first.unmount();

    fetchReset.mockClear();
    failed();
    const second = render(panel());
    expect(screen.getByRole("alert")).toHaveTextContent(/did not answer/i);
    fireEvent.change(screen.getByLabelText(/image url/i), {
      target: { value: "https://example.test/a.jpg" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^set$/i }));
    expect(fetchReset).toHaveBeenCalled();
    second.rerender(panel());
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("clears a stale outcome when another action starts", async () => {
    resetMutate.mockImplementation(
      (_v: undefined, opts: { onSuccess: (r: unknown) => void }) =>
        opts.onSuccess({ ok: true, cleared_override: true, cleared_auto: true }),
    );
    render(<ArtistImageEditPanel name="ABBA" onSaved={() => {}} onClose={() => {}} />);
    await confirmReset();
    expect(screen.getByRole("status")).toHaveTextContent(/looked up again/i);
    // Submitting the pasted link must not leave the reset's outcome standing
    // over it as if it described what just happened.
    fireEvent.change(screen.getByLabelText(/image url/i), {
      target: { value: "https://example.test/a.jpg" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^set$/i }));
    expect(screen.getByRole("status")).toHaveTextContent("");
  });

  it("a both-false reset promises the lookup WITHOUT claiming anything was cleared", async () => {
    // An unwritable cache dir swallows the unlink while the in-memory entry is
    // dropped, so `false, false` can still mean what is served changed — the
    // copy may not read as "already clear". The overclaim production can
    // actually emit is the SIBLING branch of the same ternary, which prefixes
    // "Cleared." — so this pins the both-false sentence verbatim (the
    // apostrophe is U+2019) rather than a wording blocklist, and any leak of
    // the cleared branch fails on both the equality and the prefix.
    resetMutate.mockImplementation(
      (_v: undefined, opts: { onSuccess: (r: unknown) => void }) =>
        opts.onSuccess({ ok: true, cleared_override: false, cleared_auto: false }),
    );
    render(<ArtistImageEditPanel name="ABBA" onSaved={() => {}} onClose={() => {}} />);
    await confirmReset();
    const note = screen.getByRole("status");
    expect(note.textContent).toBe("This artist’s portrait will be looked up again.");
    expect(note.textContent).not.toMatch(/^Cleared\./);
  });

  it("offers no fetch at all when both artist-image toggles are off", () => {
    // The route gates on image-toggle OR write-toggle; showing Fetch with both
    // off ships a button whose only possible answer is a 403.
    imageSettings.enabled = false;
    artSettings.enabled = false;
    const { container } = render(
      <ArtistImageEditPanel name="ABBA" onSaved={() => {}} onClose={() => {}} />,
    );
    // The HEADING is what this test lives on. The control and the group are
    // hidden by `available.length > 0` whenever the query has no data, so
    // asserting only on those passed with the gate deleted — measured: the
    // gate's own mutant survived all 1029 tests. The heading renders on the
    // gate alone, and with the gate gone a disabled query also paints a
    // skeleton that never resolves, so both are pinned here.
    expect(screen.queryByRole("heading", { name: /fetch from a source/i })).toBeNull();
    expect(container.querySelectorAll('[data-slot="skeleton"]')).toHaveLength(0);
    expect(screen.queryByRole("group", { name: /image source/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /^fetch$/i })).toBeNull();
    // ...and the upload/reset half still works, heading and all.
    expect(screen.getByRole("heading", { name: /use your own image/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /reset to auto/i })).toBeInTheDocument();
  });

  it("shows a skeleton only while the sources request is actually in the air", () => {
    // The skeleton branch had no test at all, which is how `isPending` (true
    // for a DISABLED query too) survived there as the predicate.
    sourcesLoading = true;
    const { container } = render(
      <ArtistImageEditPanel name="ABBA" onSaved={() => {}} onClose={() => {}} />,
    );
    expect(container.querySelectorAll('[data-slot="skeleton"]')).toHaveLength(1);
    expect(screen.queryByRole("group", { name: /image source/i })).toBeNull();
    // The heading is up, so the user knows what is loading.
    expect(screen.getByRole("heading", { name: /fetch from a source/i })).toBeInTheDocument();
  });

  it("shows no skeleton once the sources have arrived", () => {
    // Positive control for the test above: without it, "no skeleton" in the
    // gate test would hold for a component that never renders one.
    const { container } = render(
      <ArtistImageEditPanel name="ABBA" onSaved={() => {}} onClose={() => {}} />,
    );
    expect(container.querySelectorAll('[data-slot="skeleton"]')).toHaveLength(0);
    expect(screen.getByRole("group", { name: /image source/i })).toBeInTheDocument();
  });

  it("asks the sources endpoint nothing about an empty artist name", () => {
    // The server declares `min_length=1` and the hook has no guard, so an empty
    // name must not become a request at all.
    const { container } = render(
      <ArtistImageEditPanel name="" onSaved={() => {}} onClose={() => {}} />,
    );
    expect(sourcesCalls.at(-1)).toEqual({ name: "", enabled: false });
    expect(screen.queryByRole("group", { name: /image source/i })).toBeNull();
    // The ONE state where the query is disabled while this subtree still
    // renders: the toggles are on, so the gate lets it through, but the empty
    // name disables the query. A disabled query is pending forever, so
    // branching on `isPending` here paints a skeleton that never resolves —
    // and this is the only assertion in the suite that can tell the two
    // predicates apart.
    expect(container.querySelectorAll('[data-slot="skeleton"]')).toHaveLength(0);
  });

  it("still offers fetch when only the write-to-library toggle is on", () => {
    imageSettings.enabled = false;
    artSettings.enabled = true;
    render(<ArtistImageEditPanel name="ABBA" onSaved={() => {}} onClose={() => {}} />);
    expect(screen.getByRole("button", { name: /^fetch$/i })).toBeInTheDocument();
  });
});
