import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import * as assetVersion from "@/api/assetVersion";
import { useEventStream } from "@/api/useEventStream";

class CapturingEventSource {
  static last: CapturingEventSource | null = null;
  url: string | URL;
  onopen: ((e: Event) => void) | null = null;
  onmessage: ((e: MessageEvent) => void) | null = null;
  onerror: ((e: Event) => void) | null = null;
  closed = false;
  constructor(url: string | URL) {
    this.url = url;
    CapturingEventSource.last = this;
  }
  close(): void {
    this.closed = true;
  }
}

function setup() {
  vi.stubGlobal("EventSource", CapturingEventSource);
  // No-op the real bump so the module-level counter stays put across tests; we
  // only assert whether it was CALLED, not the absolute value.
  const bumpSpy = vi.spyOn(assetVersion, "bumpAssetVersion").mockImplementation(() => {});
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const spy = vi.spyOn(qc, "invalidateQueries");
  const wrapper = ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client: qc }, children);
  const view = renderHook(() => useEventStream(), { wrapper });
  return { spy, bumpSpy, view, es: () => CapturingEventSource.last! };
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("useEventStream", () => {
  it("invalidates the library family (incl. playlists) on a library:changed message", () => {
    const { spy, bumpSpy, es } = setup();
    es().onmessage?.(new MessageEvent("message", { data: '{"type":"library:changed"}' }));
    const keys = spy.mock.calls.map(([f]) => f?.queryKey);
    expect(keys).toEqual(
      expect.arrayContaining([
        ["albums"],
        ["artists"],
        ["browse"],
        ["search"],
        ["stats"],
        ["album"],
        ["playlists"],
        ["playlist"],
      ]),
    );
    // A routine data change must NOT remount images (no roster flicker).
    expect(bumpSpy).not.toHaveBeenCalled();
  });

  it("invalidates AND bumps the asset version on an art:changed message", () => {
    const { spy, bumpSpy, es } = setup();
    es().onmessage?.(new MessageEvent("message", { data: '{"type":"art:changed"}' }));
    expect(spy.mock.calls.length).toBeGreaterThan(0);
    expect(bumpSpy).toHaveBeenCalledTimes(1);
  });

  it("skips the first onopen but catches up on re-connect", () => {
    const { spy, es } = setup();
    // First connect: queries just loaded — nothing to catch up on.
    es().onopen?.(new Event("open"));
    expect(spy.mock.calls.length).toBe(0);
    // Re-connect (after a drop/sleep): invalidate to catch up on what was missed.
    es().onopen?.(new Event("open"));
    expect(spy.mock.calls.length).toBeGreaterThan(0);
  });

  it("closes the stream on unmount", () => {
    const { view, es } = setup();
    const source = es();
    view.unmount();
    expect(source.closed).toBe(true);
  });
});
