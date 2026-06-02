import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { CoverEditPanel } from "@/pages/albums/CoverEditPanel";

function renderPanel(onInstalled = vi.fn()) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <CoverEditPanel albumId={7} onInstalled={onInstalled} onClose={() => {}} />
    </QueryClientProvider>,
  );
  return onInstalled;
}

describe("CoverEditPanel", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    // jsdom lacks createObjectURL
    vi.stubGlobal("URL", { ...URL, createObjectURL: () => "blob:preview", revokeObjectURL: () => {} });
  });

  it("fetches, previews, then installs on approve", async () => {
    const png = new Blob([new Uint8Array([137, 80, 78, 71])], { type: "image/png" });
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(
        new Response(png, { status: 200, headers: { "X-Art-Source": "Cover Art Archive" } }),
      )
      .mockResolvedValueOnce(new Response(JSON.stringify({ ok: true, embedded: false }), { status: 200 }));

    const onInstalled = renderPanel();
    fireEvent.click(screen.getByRole("button", { name: /fetch from sources/i }));
    await waitFor(() => expect(screen.getByText(/Cover Art Archive/i)).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: /use this cover/i }));
    await waitFor(() => expect(onInstalled).toHaveBeenCalled());
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("shows 'no cover found' on 404", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(null, { status: 404 }));
    renderPanel();
    fireEvent.click(screen.getByRole("button", { name: /fetch from sources/i }));
    await waitFor(() => expect(screen.getByText(/no cover found/i)).toBeInTheDocument());
  });
});
