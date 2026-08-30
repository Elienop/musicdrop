import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import * as assetVersion from "@/api/assetVersion";
import { LIBRARY_CONTENT_KEY_COUNT, useEventStream } from "@/api/useEventStream";

class CapturingEventSource {
  static last: CapturingEventSource | null = null;
  url: string | URL;
  /** OPEN. The hook's onerror only acts on CLOSED (2), so a test that wants
   * the permanent-close branch sets this itself before firing onerror. */
  readyState = 1;
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

  it("a continuous stream flushes exactly at the max-wait boundary (1700ms = 2000ms max-wait - 300ms flush-after)", () => {
    vi.useFakeTimers();
    const { spy, es } = setup();
    const emit = () =>
      es().onmessage?.(new MessageEvent("message", { data: '{"type":"library:changed"}' }));
    emit(); // t=0: pins firstQueuedAt and arms the 300ms trailing debounce
    // Keep re-arming the debounce every 100ms (< 300ms) so it never gets a
    // quiet gap to fire on its own — this is the "continuous stream" case.
    for (let t = 100; t < 1700; t += 100) {
      vi.advanceTimersByTime(100);
      emit();
      expect(spy).not.toHaveBeenCalled(); // still short of the max-wait threshold
    }
    // now === 1600ms here. One more 100ms tick crosses the threshold
    // (now - firstQueuedAt >= MAX_WAIT_MS - FLUSH_AFTER_MS, i.e. >= 1700).
    vi.advanceTimersByTime(100); // now === 1700ms; nothing fires on time alone
    expect(spy).not.toHaveBeenCalled();
    emit(); // the message AT the threshold is what triggers the immediate flush
    expect(spy).toHaveBeenCalledTimes(LIBRARY_CONTENT_KEY_COUNT); // exactly one round
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

  it("cancels a pending debounce timer on reconnect (no double invalidation)", () => {
    vi.useFakeTimers();
    const { spy, es } = setup();
    es().onopen?.(new Event("open")); // first connect: nothing to catch up on
    // A message arrives and arms the 300ms trailing debounce...
    es().onmessage?.(new MessageEvent("message", { data: '{"type":"library:changed"}' }));
    vi.advanceTimersByTime(100); // ...then the connection drops mid-window (timer still pending)
    es().onopen?.(new Event("open")); // reconnect: immediate catch-up round
    // Advance well past where the stale queued timer would have fired (it
    // was armed at +300ms from the message, i.e. long since elapsed) — the
    // reconnect's catch-up must have cancelled it, not just run alongside it.
    vi.advanceTimersByTime(2000);
    expect(spy).toHaveBeenCalledTimes(LIBRARY_CONTENT_KEY_COUNT); // exactly ONE round, not two
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
    expect(spy.mock.calls).toHaveLength(0);
    // Re-connect (after a drop/sleep): invalidate to catch up on what was missed.
    es().onopen?.(new Event("open"));
    expect(spy.mock.calls.length).toBeGreaterThan(0);
  });

  it("flushes a queued round when the stream closes for good", () => {
    // An HTTP error on the INITIAL response closes an EventSource
    // permanently — the browser does not retry one — and the session gate's
    // 401 on an expired cookie is exactly that. Whatever was queued is the
    // last thing this stream will ever say, and the quiet gap the debounce
    // waits for has already begun, so sitting on it just delays a refresh
    // that is already owed.
    vi.useFakeTimers();
    const { spy, es } = setup();
    es().onmessage?.(new MessageEvent("message", { data: '{"type":"library:changed"}' }));
    expect(spy).not.toHaveBeenCalled(); // still inside the trailing debounce

    es().readyState = 2; // CLOSED
    es().onerror?.(new Event("error"));

    expect(spy).toHaveBeenCalledTimes(LIBRARY_CONTENT_KEY_COUNT); // one round, now
    // And the cancelled timer does not fire a second one behind it.
    vi.advanceTimersByTime(2000);
    expect(spy).toHaveBeenCalledTimes(LIBRARY_CONTENT_KEY_COUNT);
  });

  it("leaves a TRANSIENT drop alone — the browser is still retrying", () => {
    // readyState CONNECTING means a reconnect is already in flight, and
    // onopen's catch-up branch covers the gap. Flushing here would spend a
    // full invalidation round on a stream that is about to come back.
    vi.useFakeTimers();
    const { spy, es } = setup();
    es().onmessage?.(new MessageEvent("message", { data: '{"type":"library:changed"}' }));

    es().readyState = 0; // CONNECTING
    es().onerror?.(new Event("error"));

    expect(spy).not.toHaveBeenCalled();
    // The debounce it left alone still fires on its own schedule.
    vi.advanceTimersByTime(300);
    expect(spy).toHaveBeenCalledTimes(LIBRARY_CONTENT_KEY_COUNT);
  });

  it("a permanent close with nothing queued invalidates nothing", () => {
    // The common shape of this branch: the 401 lands on the stream's very
    // first response, so no message ever arrived to queue anything.
    vi.useFakeTimers();
    const { spy, es } = setup();

    es().readyState = 2; // CLOSED
    es().onerror?.(new Event("error"));

    expect(spy).not.toHaveBeenCalled();
  });

  it("closes the stream on unmount", () => {
    const { view, es } = setup();
    const source = es();
    view.unmount();
    expect(source.closed).toBe(true);
  });
});
