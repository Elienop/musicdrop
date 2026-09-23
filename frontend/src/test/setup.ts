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

// jsdom has no ResizeObserver. Radix's Checkbox measures the control with one
// (`react-use-size`) to size the hidden bubble input it renders for native form
// participation — and it renders that input only INSIDE a <form>, which is why
// the six older Checkbox call sites (five files) never needed this and the one
// in ReleaseSearchRow's search form does. A no-op class is enough: nothing in
// the app reads an entry, and nothing in the app's own source branches on the
// global being present. A dependency does: @floating-ui/dom's `autoUpdate`
// defaults `elementResize` to `typeof ResizeObserver === "function"`, so every
// Radix popper now constructs this and calls observe on two elements. With a
// no-op that never fires, those calls are the whole difference.
class NoopResizeObserver {
  observe(): void {
    /* no layout engine to observe */
  }
  unobserve(): void {
    /* no layout engine to observe */
  }
  disconnect(): void {
    /* no layout engine to observe */
  }
}
vi.stubGlobal("ResizeObserver", NoopResizeObserver);

// jsdom has no EventSource. The app's useEventStream opens one at the shell, so
// every App-rendering test needs a stand-in. A no-op class is enough here; the
// dedicated useEventStream test installs its own capturing mock.
class NoopEventSource {
  /** The platform's CLOSED constant. useEventStream reads
   * `EventSource.CLOSED` off whatever class is global, so a stub without this
   * makes the comparison `!== undefined` — permanently true, silently
   * disabling the hook's give-up branch. */
  static readonly CLOSED = 2;
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
// A file's last test can leave 0 ms timers queued: cleanup() unmounting a
// Radix overlay queues FocusScope's focus restore, and a query settling at the
// end queues TanStack's notify flush. vitest tears jsdom down when the file
// ends without waiting for them, so one can run after `window` is deleted and
// Node's own CustomEvent is back, and the run exits 1 with every test passed
// ("Failed to execute 'dispatchEvent' on 'EventTarget'", "window is not
// defined"). One macrotask first lets them run while jsdom is still installed:
// timers with the same delay fire in the order they were queued.
afterAll(async () => {
  await new Promise((resolve) => setTimeout(resolve, 0));
  server.close();
});
