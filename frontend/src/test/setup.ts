import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterAll, afterEach, beforeAll, vi } from "vitest";

import { server } from "./msw-server";

// jsdom has no layout engine, so window.scrollTo is unimplemented and throws a
// "Not implemented" error when components call it (e.g. scroll-to-top on page
// change). Stub it so those code paths run cleanly under tests.
vi.stubGlobal("scrollTo", vi.fn());

// jsdom has no matchMedia. Hooks that branch on a media query call it inside
// their effect (e.g. useRailMaxHeight's md+ check), so provide a stand-in.
// Tests that assert on the result spy on window.matchMedia and return their own
// MediaQueryList; this default reports "no match" with no-op listeners.
vi.stubGlobal(
  "matchMedia",
  (query: string) =>
    ({
      matches: false,
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }) as MediaQueryList,
);

// jsdom has no EventSource. The app's useEventStream opens one at the shell, so
// every App-rendering test needs a stand-in. A no-op class is enough here; the
// dedicated useEventStream test installs its own capturing mock.
class NoopEventSource {
  url: string | URL;
  /** OPEN. The hook's onerror branches on CLOSED (2); a stand-in that never
   * errors stays open, and the dedicated test drives this itself. */
  readyState = 1;
  onopen: ((e: Event) => void) | null = null;
  onmessage: ((e: MessageEvent) => void) | null = null;
  onerror: ((e: Event) => void) | null = null;
  constructor(url: string | URL) {
    this.url = url;
  }
  close(): void {
    /* jsdom lacks this; the test never observes it */
  }
  addEventListener(): void {
    /* jsdom lacks this; the test never observes it */
  }
  removeEventListener(): void {
    /* jsdom lacks this; the test never observes it */
  }
}
vi.stubGlobal("EventSource", NoopEventSource);

// Node >=22 defines an experimental `localStorage` getter on globalThis that
// returns undefined unless --localstorage-file is set; under vitest's
// populateGlobal it shadows jsdom's real localStorage. Replace it with an
// in-memory Storage so components can persist (e.g. the sidebar collapse).
if (globalThis.localStorage === undefined) {
  const store = new Map<string, string>();
  const memoryStorage: Storage = {
    get length() {
      return store.size;
    },
    clear: () => {
      store.clear();
    },
    getItem: (key: string) => store.get(key) ?? null,
    key: (index: number) => [...store.keys()][index] ?? null,
    removeItem: (key: string) => {
      store.delete(key);
    },
    setItem: (key: string, value: string) => {
      store.set(key, String(value));
    },
  };
  Object.defineProperty(globalThis, "localStorage", {
    value: memoryStorage,
    writable: true,
    configurable: true,
  });
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  cleanup();
  server.resetHandlers();
});
afterAll(() => server.close());
