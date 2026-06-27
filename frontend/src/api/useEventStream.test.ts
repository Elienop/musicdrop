import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

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
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const spy = vi.spyOn(qc, "invalidateQueries");
  const wrapper = ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client: qc }, children);
  const view = renderHook(() => useEventStream(), { wrapper });
  return { spy, view, es: () => CapturingEventSource.last! };
}

afterEach(() => vi.unstubAllGlobals());

describe("useEventStream", () => {
  it("invalidates the library family on a message", () => {
    const { spy, es } = setup();
    es().onmessage?.(new MessageEvent("message", { data: '{"type":"library:changed"}' }));
    const keys = spy.mock.calls.map(([f]) => f?.queryKey);
    expect(keys).toEqual(
      expect.arrayContaining([["albums"], ["artists"], ["browse"], ["search"], ["stats"], ["album"]]),
    );
  });

  it("catches up on (re)connect via onopen", () => {
    const { spy, es } = setup();
    spy.mockClear();
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
