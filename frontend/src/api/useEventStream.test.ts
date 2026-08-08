import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import * as assetVersion from "@/api/assetVersion";
import { LIBRARY_CONTENT_KEY_COUNT, useEventStream } from "@/api/useEventStream";

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
  // Some tests opt into fake timers to drive the debounce/max-wait math
  // deterministically; always leave real timers behind for the next test.
  vi.useRealTimers();
});

describe("useEventStream", () => {
  it("invalidates the library family (incl. playlists) on a library:changed message", () => {
    vi.useFakeTimers();
    const { spy, bumpSpy, es } = setup();
    es().onmessage?.(new MessageEvent("message", { data: '{"type":"library:changed"}' }));
    // Coalesced: nothing fires until the trailing debounce elapses.
    vi.advanceTimersByTime(300);
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
    vi.useFakeTimers();
    const { spy, bumpSpy, es } = setup();
    es().onmessage?.(
      // The REAL unscoped wire bytes: the broker dumps with exclude_none, so a
      // library-wide art event carries no `scope` key at all (not `null`).
      new MessageEvent("message", { data: '{"type":"art:changed"}' }),
    );
    // The asset-version bump is immediate (asserted below); the library
    // invalidation it also triggers is coalesced like any other message.
    vi.advanceTimersByTime(300);
    expect(spy.mock.calls.length).toBeGreaterThan(0);
    // No scope = library-wide: bump the global counter (undefined, not null).
    expect(bumpSpy).toHaveBeenCalledWith(undefined);
  });

  it("coalesces an event burst into one invalidation round", () => {
    vi.useFakeTimers();
    const { spy, es } = setup();
    es().onmessage?.(new MessageEvent("message", { data: '{"type":"library:changed"}' }));
    es().onmessage?.(new MessageEvent("message", { data: '{"type":"library:changed"}' }));
    es().onmessage?.(new MessageEvent("message", { data: '{"type":"library:changed"}' }));
    expect(spy).not.toHaveBeenCalled(); // trailing debounce — nothing yet
    vi.advanceTimersByTime(300);
    expect(spy).toHaveBeenCalledTimes(LIBRARY_CONTENT_KEY_COUNT); // one round, not three
  });

  it("a continuous stream still flushes every ~2s (max-wait)", () => {
    vi.useFakeTimers();
    const { spy, es } = setup();
    for (let t = 0; t < 2100; t += 100) {
      es().onmessage?.(new MessageEvent("message", { data: '{"type":"library:changed"}' }));
      vi.advanceTimersByTime(100);
    }
    // A continuous drain keeps re-arming the 300ms trailing debounce, which
    // would otherwise never fire; the max-wait clause forces a flush anyway.
    expect(spy.mock.calls.length).toBeGreaterThan(0);
  });

  it("art:changed bumps the asset version immediately, before any flush", () => {
    vi.useFakeTimers();
    const { bumpSpy, es } = setup();
    es().onmessage?.(
      new MessageEvent("message", { data: '{"type":"art:changed","scope":"album:7"}' }),
    );
    // No timer advance: the bump must already have happened.
    expect(bumpSpy).toHaveBeenCalledWith("album:7");
  });

  it("unmount clears a pending debounce timer (no invalidation fires after)", () => {
    vi.useFakeTimers();
    const { spy, view, es } = setup();
    es().onmessage?.(new MessageEvent("message", { data: '{"type":"library:changed"}' }));
    view.unmount();
    vi.advanceTimersByTime(2000);
    expect(spy).not.toHaveBeenCalled();
  });

  it("bumps ONLY the named scope when art:changed carries one", () => {
    const { bumpSpy, es } = setup();
    es().onmessage?.(
      new MessageEvent("message", { data: '{"type":"art:changed","scope":"album:7"}' }),
    );
    expect(bumpSpy).toHaveBeenCalledTimes(1);
    expect(bumpSpy).toHaveBeenCalledWith("album:7");
  });

  it("re-connect catch-up bumps GLOBALLY (unknown what was missed)", () => {
    const { bumpSpy, es } = setup();
    es().onopen?.(new Event("open")); // first connect: nothing to catch up on
    expect(bumpSpy).not.toHaveBeenCalled();
    es().onopen?.(new Event("open")); // re-connect
    expect(bumpSpy).toHaveBeenCalledTimes(1);
    expect(bumpSpy.mock.calls[0][0]).toBeUndefined(); // no scope = global bump
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
