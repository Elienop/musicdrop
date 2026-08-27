import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { CoverEditPanel } from "@/pages/albums/CoverEditPanel";

function renderPanel(onInstalled = vi.fn(), onClose = vi.fn()) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const utils = render(
    <QueryClientProvider client={qc}>
      <CoverEditPanel albumId={7} onInstalled={onInstalled} onClose={onClose} />
    </QueryClientProvider>,
  );
  return { onInstalled, onClose, ...utils };
}

/** A tiny valid-ish image File of a given type/size for the client-side guard. */
function makeFile(name: string, type: string, bytes = 4): File {
  return new File([new Uint8Array(bytes)], name, { type });
}

/** The PNG signature `sniff_image_mime` matches on, spelled out so a file with
 * an untypeable name is still genuinely the thing the server would accept. */
const PNG_MAGIC = new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10]);

// Hand-rolled Response-likes: a real `new Response(jsdomBlob)` calls `.stream()`
// on the body when consumed, which the jsdom Blob lacks on Node 22 (undici) ->
// "object.stream is not a function". These expose exactly what the hooks read.
function imageResponse(blob: Blob, source: string): Response {
  return {
    ok: true,
    status: 200,
    headers: {
      get: (h: string) => (h.toLowerCase() === "x-art-source" ? source : null),
    },
    blob: async () => blob,
  } as unknown as Response;
}
function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => null },
    json: async () => body,
  } as unknown as Response;
}
function emptyResponse(status: number): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => null },
  } as unknown as Response;
}

describe("CoverEditPanel", () => {
  let revokeSpy: ReturnType<typeof vi.fn>;
  let createSpy: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    vi.restoreAllMocks();
    revokeSpy = vi.fn();
    createSpy = vi.fn(() => "blob:preview");
    // jsdom lacks createObjectURL/revokeObjectURL
    vi.stubGlobal("URL", {
      ...URL,
      createObjectURL: createSpy,
      revokeObjectURL: revokeSpy,
    });
  });

  it("fetches, previews, then installs on approve", async () => {
    const png = new Blob([new Uint8Array([137, 80, 78, 71])], {
      type: "image/png",
    });
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(imageResponse(png, "Cover Art Archive"))
      .mockResolvedValueOnce(jsonResponse({ ok: true, embedded: false }, 200));

    const { onInstalled } = renderPanel();
    fireEvent.click(
      screen.getByRole("button", { name: /fetch from online sources/i }),
    );
    expect(await screen.findByText(/Cover Art Archive/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /use this cover/i }));
    // Surfaces an inline success note instead of auto-closing.
    expect(await screen.findByText(/cover updated/i)).toBeInTheDocument();
    expect(onInstalled).toHaveBeenCalled();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("shows 'no cover found' on 404", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(emptyResponse(404));
    renderPanel();
    fireEvent.click(
      screen.getByRole("button", { name: /fetch from online sources/i }),
    );
    expect(await screen.findByText(/no cover found/i)).toBeInTheDocument();
  });

  it("surfaces an embed-skip detail and keeps the panel open with a Done button", async () => {
    const png = new Blob([new Uint8Array([137, 80, 78, 71])], {
      type: "image/png",
    });
    vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(imageResponse(png, "Cover Art Archive"))
      .mockResolvedValueOnce(
        jsonResponse(
          {
            ok: true,
            embedded: false,
            embed_detail: "embedart on but image is webp",
          },
          200,
        ),
      );

    const { onInstalled, onClose } = renderPanel();
    fireEvent.click(
      screen.getByRole("button", { name: /fetch from online sources/i }),
    );
    expect(await screen.findByText(/Cover Art Archive/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /use this cover/i }));

    expect(await screen.findByText(/cover updated/i)).toBeInTheDocument();
    expect(
      screen.getByText(/embedart on but image is webp/i),
    ).toBeInTheDocument();
    expect(onInstalled).toHaveBeenCalled();
    // Did not auto-close; closes via Done.
    expect(onClose).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: /done/i }));
    expect(onClose).toHaveBeenCalled();
  });

  it("surfaces the server's reason on a 413 oversize install error", async () => {
    const png = new Blob([new Uint8Array([137, 80, 78, 71])], {
      type: "image/png",
    });
    vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(imageResponse(png, "Cover Art Archive"))
      .mockResolvedValueOnce(
        // 413, not 422: an oversize cover is Payload Too Large, matching the
        // artist-portrait and playlist-artwork uploads.
        jsonResponse({ detail: "Image is too large (max 10 MB)." }, 413),
      );

    renderPanel();
    fireEvent.click(
      screen.getByRole("button", { name: /fetch from online sources/i }),
    );
    expect(await screen.findByText(/Cover Art Archive/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /use this cover/i }));
    expect(
      await screen.findByText(/image is too large \(max 10 mb\)/i),
    ).toBeInTheDocument();
  });

  it("surfaces the message from a structured 500 install error", async () => {
    // Cover install guard raises {detail: {message, recovery}} on 500 — the
    // message carries the real cause (missing folder vs permission denied).
    const png = new Blob([new Uint8Array([137, 80, 78, 71])], {
      type: "image/png",
    });
    vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(imageResponse(png, "Cover Art Archive"))
      .mockResolvedValueOnce(
        jsonResponse(
          {
            detail: {
              message: "Cover install failed: [Errno 13] Permission denied",
              recovery: "Check folder permissions and retry.",
            },
          },
          500,
        ),
      );

    renderPanel();
    fireEvent.click(
      screen.getByRole("button", { name: /fetch from online sources/i }),
    );
    expect(await screen.findByText(/Cover Art Archive/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /use this cover/i }));
    expect(
      await screen.findByText(/errno 13\] permission denied/i),
    ).toBeInTheDocument();
  });

  it("rejects an oversize file before previewing", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch");
    renderPanel();
    const input = screen.getByLabelText(/upload cover image/i);
    const big = makeFile("huge.png", "image/png", 10 * 1024 * 1024 + 1);
    fireEvent.change(input, { target: { files: [big] } });

    expect(await screen.findByText(/10 MB/i)).toBeInTheDocument();
    expect(screen.queryByAltText(/cover preview/i)).not.toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("rejects an unsupported file type before previewing", async () => {
    renderPanel();
    const input = screen.getByLabelText(/upload cover image/i);
    const bmp = makeFile("art.bmp", "image/bmp");
    fireEvent.change(input, { target: { files: [bmp] } });

    expect(
      await screen.findByText(/png, jpeg, gif, or webp/i),
    ).toBeInTheDocument();
    expect(screen.queryByAltText(/cover preview/i)).not.toBeInTheDocument();
  });

  // The route never reads the declared content type — it sniffs magic bytes
  // (`sniff_image_mime`, backend/app/artwork/images.py). A file the browser
  // could not type therefore has to reach the server, which would accept it.
  // Refusing it here left the user with no way to proceed at all.
  it("previews a PNG whose declared type came through blank", async () => {
    renderPanel();
    const input = screen.getByLabelText(/upload cover image/i);
    // Real PNG magic bytes, and no extension for the OS to guess from — the
    // exact pair the old `ACCEPTED_TYPES.has(file.type)` gate refused.
    const png = new File([PNG_MAGIC], "artwork", { type: "" });
    expect(png.type).toBe("");
    fireEvent.change(input, { target: { files: [png] } });

    expect(await screen.findByAltText(/cover preview/i)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("previews a PNG declared as application/octet-stream", async () => {
    renderPanel();
    const input = screen.getByLabelText(/upload cover image/i);
    const png = new File([PNG_MAGIC], "artwork.bin", {
      type: "application/octet-stream",
    });
    fireEvent.change(input, { target: { files: [png] } });

    expect(await screen.findByAltText(/cover preview/i)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  // Leniency about the TYPE must not leak into the SIZE guard, which mirrors
  // `len(data) > cap` exactly and stays unconditional.
  it("still rejects an oversize file whose declared type is blank", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch");
    renderPanel();
    const input = screen.getByLabelText(/upload cover image/i);
    const big = new File([new Uint8Array(10 * 1024 * 1024 + 1)], "artwork", {
      type: "",
    });
    fireEvent.change(input, { target: { files: [big] } });

    expect(await screen.findByText(/10 MB/i)).toBeInTheDocument();
    expect(screen.queryByAltText(/cover preview/i)).not.toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("opens the native file picker when 'Upload an image…' is clicked", () => {
    renderPanel();
    const input = screen.getByLabelText(/upload cover image/i);
    const clickSpy = vi.spyOn(input, "click");
    fireEvent.click(screen.getByRole("button", { name: /upload an image/i }));
    expect(clickSpy).toHaveBeenCalled();
  });

  it("previews an accepted file pick", async () => {
    renderPanel();
    const input = screen.getByLabelText(/upload cover image/i);
    fireEvent.change(input, {
      target: { files: [makeFile("art.png", "image/png")] },
    });
    expect(await screen.findByAltText(/cover preview/i)).toBeInTheDocument();
    expect(screen.getByText(/your file/i)).toBeInTheDocument();
  });

  it("creates an object URL for a fetched preview", async () => {
    const png = new Blob([new Uint8Array([137, 80, 78, 71])], {
      type: "image/png",
    });
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      imageResponse(png, "Cover Art Archive"),
    );
    renderPanel();
    fireEvent.click(
      screen.getByRole("button", { name: /fetch from online sources/i }),
    );
    await screen.findByAltText(/cover preview/i);
    expect(createSpy).toHaveBeenCalled();
  });

  it("does not carry one album's pending cover onto another", async () => {
    // `/albums/:albumId` is an unkeyed route element, so moving between two
    // albums re-renders this SAME instance with a new id. A candidate that
    // survived that would be installed against whichever album is now on
    // screen — bytes fetched for a different record.
    const png = new Blob([new Uint8Array([137, 80, 78, 71])], {
      type: "image/png",
    });
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      imageResponse(png, "Cover Art Archive"),
    );
    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const view = render(
      <QueryClientProvider client={qc}>
        <CoverEditPanel albumId={7} onInstalled={vi.fn()} onClose={vi.fn()} />
      </QueryClientProvider>,
    );
    fireEvent.click(
      screen.getByRole("button", { name: /fetch from online sources/i }),
    );
    await screen.findByAltText(/cover preview/i);
    revokeSpy.mockClear();

    // The provider has to be re-rendered too, or the panel loses its client.
    view.rerender(
      <QueryClientProvider client={qc}>
        <CoverEditPanel albumId={8} onInstalled={vi.fn()} onClose={vi.fn()} />
      </QueryClientProvider>,
    );
    expect(screen.queryByAltText(/cover preview/i)).toBeNull();
    expect(
      screen.queryByRole("button", { name: /use this cover/i }),
    ).toBeNull();
    // ...and the abandoned candidate's URL goes with it.
    expect(revokeSpy).toHaveBeenCalledWith("blob:preview");
  });

  it("revokes the pending preview URL on unmount", async () => {
    const { unmount } = renderPanel();
    const input = screen.getByLabelText(/upload cover image/i);
    fireEvent.change(input, {
      target: { files: [makeFile("a.png", "image/png")] },
    });
    await screen.findByAltText(/cover preview/i);

    revokeSpy.mockClear();
    unmount();
    expect(revokeSpy).toHaveBeenCalledWith("blob:preview");
  });

  it("clears a prior fetch error when picking a file", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(emptyResponse(500));
    renderPanel();
    fireEvent.click(
      screen.getByRole("button", { name: /fetch from online sources/i }),
    );
    expect(
      await screen.findByText(/couldn’t fetch a cover/i),
    ).toBeInTheDocument();

    const input = screen.getByLabelText(/upload cover image/i);
    fireEvent.change(input, {
      target: { files: [makeFile("a.png", "image/png")] },
    });
    await screen.findByAltText(/cover preview/i);
    expect(
      screen.queryByText(/couldn’t fetch a cover/i),
    ).not.toBeInTheDocument();
  });
});
