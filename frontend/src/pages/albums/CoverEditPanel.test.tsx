import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
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

describe("CoverEditPanel", () => {
  let revokeSpy: ReturnType<typeof vi.fn>;
  let createSpy: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    vi.restoreAllMocks();
    revokeSpy = vi.fn();
    createSpy = vi.fn(() => "blob:preview");
    // jsdom lacks createObjectURL/revokeObjectURL
    vi.stubGlobal("URL", { ...URL, createObjectURL: createSpy, revokeObjectURL: revokeSpy });
  });

  it("fetches, previews, then installs on approve", async () => {
    const png = new Blob([new Uint8Array([137, 80, 78, 71])], { type: "image/png" });
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(
        new Response(png, { status: 200, headers: { "X-Art-Source": "Cover Art Archive" } }),
      )
      .mockResolvedValueOnce(new Response(JSON.stringify({ ok: true, embedded: false }), { status: 200 }));

    const { onInstalled } = renderPanel();
    fireEvent.click(screen.getByRole("button", { name: /fetch from sources/i }));
    await waitFor(() => expect(screen.getByText(/Cover Art Archive/i)).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: /use this cover/i }));
    // Surfaces an inline success note instead of auto-closing.
    await waitFor(() => expect(screen.getByText(/cover updated/i)).toBeInTheDocument());
    expect(onInstalled).toHaveBeenCalled();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("shows 'no cover found' on 404", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(null, { status: 404 }));
    renderPanel();
    fireEvent.click(screen.getByRole("button", { name: /fetch from sources/i }));
    await waitFor(() => expect(screen.getByText(/no cover found/i)).toBeInTheDocument());
  });

  it("surfaces an embed-skip detail and keeps the panel open with a Done button", async () => {
    const png = new Blob([new Uint8Array([137, 80, 78, 71])], { type: "image/png" });
    vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(
        new Response(png, { status: 200, headers: { "X-Art-Source": "Cover Art Archive" } }),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            ok: true,
            embedded: false,
            embed_detail: "embedart on but image is webp",
          }),
          { status: 200 },
        ),
      );

    const { onInstalled, onClose } = renderPanel();
    fireEvent.click(screen.getByRole("button", { name: /fetch from sources/i }));
    await waitFor(() => expect(screen.getByText(/Cover Art Archive/i)).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: /use this cover/i }));

    await waitFor(() => expect(screen.getByText(/cover updated/i)).toBeInTheDocument());
    expect(screen.getByText(/embedart on but image is webp/i)).toBeInTheDocument();
    expect(onInstalled).toHaveBeenCalled();
    // Did not auto-close; closes via Done.
    expect(onClose).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: /done/i }));
    expect(onClose).toHaveBeenCalled();
  });

  it("surfaces the server's reason on a 422 install error", async () => {
    const png = new Blob([new Uint8Array([137, 80, 78, 71])], { type: "image/png" });
    vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(
        new Response(png, { status: 200, headers: { "X-Art-Source": "Cover Art Archive" } }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ detail: "Image is too large (max 10 MB)." }), { status: 422 }),
      );

    renderPanel();
    fireEvent.click(screen.getByRole("button", { name: /fetch from sources/i }));
    await waitFor(() => expect(screen.getByText(/Cover Art Archive/i)).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: /use this cover/i }));
    await waitFor(() =>
      expect(screen.getByText(/image is too large \(max 10 mb\)/i)).toBeInTheDocument(),
    );
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

    expect(await screen.findByText(/png, jpeg, gif, or webp/i)).toBeInTheDocument();
    expect(screen.queryByAltText(/cover preview/i)).not.toBeInTheDocument();
  });

  it("previews an accepted file pick", async () => {
    renderPanel();
    const input = screen.getByLabelText(/upload cover image/i);
    fireEvent.change(input, { target: { files: [makeFile("art.png", "image/png")] } });
    expect(await screen.findByAltText(/cover preview/i)).toBeInTheDocument();
    expect(screen.getByText(/your file/i)).toBeInTheDocument();
  });

  it("creates an object URL for a fetched preview", async () => {
    const png = new Blob([new Uint8Array([137, 80, 78, 71])], { type: "image/png" });
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      new Response(png, { status: 200, headers: { "X-Art-Source": "Cover Art Archive" } }),
    );
    renderPanel();
    fireEvent.click(screen.getByRole("button", { name: /fetch from sources/i }));
    await screen.findByAltText(/cover preview/i);
    expect(createSpy).toHaveBeenCalled();
  });

  it("revokes the pending preview URL on unmount", async () => {
    const { unmount } = renderPanel();
    const input = screen.getByLabelText(/upload cover image/i);
    fireEvent.change(input, { target: { files: [makeFile("a.png", "image/png")] } });
    await screen.findByAltText(/cover preview/i);

    revokeSpy.mockClear();
    unmount();
    expect(revokeSpy).toHaveBeenCalledWith("blob:preview");
  });

  it("clears a prior fetch error when picking a file", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(new Response(null, { status: 500 }));
    renderPanel();
    fireEvent.click(screen.getByRole("button", { name: /fetch from sources/i }));
    await waitFor(() => expect(screen.getByText(/couldn’t fetch a cover/i)).toBeInTheDocument());

    const input = screen.getByLabelText(/upload cover image/i);
    fireEvent.change(input, { target: { files: [makeFile("a.png", "image/png")] } });
    await screen.findByAltText(/cover preview/i);
    expect(screen.queryByText(/couldn’t fetch a cover/i)).not.toBeInTheDocument();
  });
});
